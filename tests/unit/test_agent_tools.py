"""Unit tests for the tool registry: gating, sandbox plumbing, redaction."""

from __future__ import annotations

import asyncio
import base64
import subprocess
import sys

import pytest

from girder.agent.context import Scratchpad
from girder.agent.tools import (
    _MATCH_CAP,
    TOOL_SCHEMAS,
    CommandDenied,
    ToolRegistry,
    _summarize_junit,
    screen_command,
)
from girder.config import LimitsConfig
from girder.db import repo
from girder.db.engine import Database
from girder.db.models import Task, TaskStatus, TaskType
from girder.guard.redact import Redactor
from girder.guard.scope import TaskScopes
from girder.sandbox.engine import ContainerSpec, ExecResult, SandboxEngine
from girder.stacks import STACK_REGISTRY, StackPlugin


class FakeSandbox(SandboxEngine):
    """Scripted exec results; records every call (no podman)."""

    def __init__(
        self,
        results: list[ExecResult] | None = None,
        side_effects: list[object] | None = None,
    ) -> None:
        self.execs: list[tuple[str, list[str], float]] = []
        self.killed: list[str] = []
        self._results = list(results or [])
        self._side_effects = list(side_effects or [])

    async def start(self, spec: ContainerSpec) -> str:
        return "ctr"

    async def exec(
        self, name: str, cmd: list[str], *, timeout_s: float = 120.0, user: str | None = None
    ) -> ExecResult:
        self.execs.append((name, cmd, timeout_s))
        fx = self._side_effects.pop(0) if self._side_effects else None
        if fx is not None:
            return fx(cmd)  # type: ignore[operator]
        return self._results.pop(0) if self._results else ExecResult(0, "", "")

    async def kill(self, name: str) -> None:
        self.killed.append(name)

    async def exists(self, name: str) -> bool:
        return True


TASK = Task(
    id="t1",
    wave_id="w0",
    seq=1,
    title="t",
    task_type=TaskType.CODE_CHANGE,
    status=TaskStatus.PENDING,
    scope_globs=["src/**"],
)
SCOPES = TaskScopes(write_globs=["src/**"], protected_globs=[".github/**"])
LIMITS = LimitsConfig()


@pytest.fixture
async def seeded(db: Database) -> tuple[Database, str, str]:
    project = await repo.create_project(db, "p", "/repo")
    run = await repo.create_run(db, project.id, "intent", "run/abc1", 5.0)
    wave = await repo.get_or_create_wave0(db, run.id)
    task_row = await repo.create_task(db, wave.id, 1, "t", TaskType.CODE_CHANGE)
    attempt = await repo.create_attempt(db, task_row.id, "abc123")
    return db, run.id, attempt.id


def _registry(
    sandbox: FakeSandbox,
    db: Database,
    run_id: str,
    attempt_id: str,
    scratchpad: Scratchpad | None = None,
    stack: StackPlugin | None = None,
) -> ToolRegistry:
    return ToolRegistry(
        sandbox=sandbox,
        container="ctr",
        scopes=SCOPES,
        limits=LIMITS,
        redactor=Redactor(),
        db=db,
        attempt_id=attempt_id,
        run_id=run_id,
        task=TASK,
        stack=stack or STACK_REGISTRY["python-3.12"],
        scratchpad=scratchpad,
    )


async def test_read_file_executes_and_logs(seeded: tuple[Database, str, str]) -> None:
    db, run_id, attempt_id = seeded
    sandbox = FakeSandbox(results=[ExecResult(0, "hello\n", "")])
    result = await _registry(sandbox, db, run_id, attempt_id).execute(
        "read_file", {"path": "src/a.py"}
    )
    assert result.ok
    assert result.output == "hello\n"
    assert sandbox.execs == [("ctr", ["sed", "-n", "1,$p", "src/a.py"], 120.0)]
    rows = await repo.list_tool_calls_for_attempt(db, attempt_id)
    assert len(rows) == 1
    assert rows[0]["tool_name"] == "read_file"
    assert not rows[0]["held"]


async def test_write_outside_scope_is_held_not_executed(
    seeded: tuple[Database, str, str],
) -> None:
    db, run_id, attempt_id = seeded
    sandbox = FakeSandbox()
    result = await _registry(sandbox, db, run_id, attempt_id).execute(
        "write_file", {"path": "tests/test_x.py", "content": "boom"}
    )
    assert not result.ok and result.held and result.scope_violation
    assert sandbox.execs == []  # never executed
    rows = await repo.list_tool_calls_for_attempt(db, attempt_id)
    assert rows[0]["scope_violation"] and rows[0]["held"]
    violations = await db.fetchall("SELECT kind FROM integrity_violations")
    assert [v["kind"] for v in violations] == ["out_of_scope_write"]


async def test_protected_read_is_violation(seeded: tuple[Database, str, str]) -> None:
    db, run_id, attempt_id = seeded
    sandbox = FakeSandbox()
    result = await _registry(sandbox, db, run_id, attempt_id).execute(
        "read_file", {"path": ".github/workflows/ci.yml"}
    )
    assert result.held and sandbox.execs == []
    violations = await db.fetchall("SELECT kind FROM integrity_violations")
    assert [v["kind"] for v in violations] == ["protected_read"]


def test_screen_command_denylist() -> None:
    for bad in ("curl http://x", "wget x", "ssh host", "git push origin main", "sudo rm x"):
        with pytest.raises(CommandDenied):
            screen_command(bad)
    with pytest.raises(CommandDenied):
        screen_command("chmod +x tests/test_a.py")  # chmod on a test-signal path
    screen_command("python -m pytest -q")  # benign
    screen_command("chmod +x run.py")  # chmod on a non-test path


def test_screen_command_resists_lexical_evasion() -> None:
    """§6.6: quote-concat and backslash tricks must not defeat the denylist."""
    for bad in (
        "c'u'r'l http://x",  # quote-concat inside the binary name
        'c"u"r"l http://x',
        "cur\\l http://x",  # backslash-split name
        "git -C /repo push origin main",  # push with intervening flags
        "git --git-dir=/r push origin main",
        "chmod 000 tests",  # bare test dir, no slash/dot
        "chmod +x tests",
    ):
        with pytest.raises(CommandDenied):
            screen_command(bad)


def test_screen_command_allows_benign_commands() -> None:
    for ok in (
        "pytest -q",
        "python -m pytest tests/",
        "git add tests/test_x.py",
        "ls tests",
        "cat README.md",
        "rg foo src/",
        "chmod +x run.sh",
        "chmod 755 build/out.sh",
    ):
        screen_command(ok)  # must not raise


async def test_run_command_denied_vs_benign(seeded: tuple[Database, str, str]) -> None:
    db, run_id, attempt_id = seeded
    sandbox = FakeSandbox(results=[ExecResult(0, "ok\n", "")])
    registry = _registry(sandbox, db, run_id, attempt_id)

    denied = await registry.execute("run_command", {"cmd": "curl http://evil.example"})
    assert not denied.ok and denied.held
    assert "held" in denied.output or "denied" in denied.output

    benign = await registry.execute("run_command", {"cmd": "python -m pytest -q"})
    assert benign.ok
    assert ("ctr", ["sh", "-c", "python -m pytest -q"]) in [(n, c) for n, c, _ in sandbox.execs]
    rows = await repo.list_tool_calls_for_attempt(db, attempt_id)
    held_rows = [r for r in rows if r["held"]]
    assert len(held_rows) == 1
    violations = await db.fetchall("SELECT kind FROM integrity_violations")
    assert [v["kind"] for v in violations] == ["scope_violation"]


async def test_run_command_redirect_out_of_scope_held_not_executed(
    seeded: tuple[Database, str, str],
) -> None:
    db, run_id, attempt_id = seeded
    sandbox = FakeSandbox()
    result = await _registry(sandbox, db, run_id, attempt_id).execute(
        "run_command", {"cmd": "echo x > tests/test_new.py"}
    )
    assert not result.ok and result.held and result.scope_violation
    assert sandbox.execs == []  # held at the scope gate — never reached sh -c
    rows = await repo.list_tool_calls_for_attempt(db, attempt_id)
    assert rows[0]["scope_violation"] and rows[0]["held"]
    violations = await db.fetchall("SELECT kind FROM integrity_violations")
    assert [v["kind"] for v in violations] == ["scope_violation"]


async def test_run_command_tee_in_scope_and_reads_execute(
    seeded: tuple[Database, str, str],
) -> None:
    db, run_id, attempt_id = seeded
    sandbox = FakeSandbox(results=[ExecResult(0, "ok\n", ""), ExecResult(0, "body\n", "")])
    registry = _registry(sandbox, db, run_id, attempt_id)

    tee = await registry.execute("run_command", {"cmd": "tee src/allowed.py"})
    assert tee.ok

    # reads are NOT write-gated (R2): existing in-scope path passes the gate
    cat = await registry.execute("run_command", {"cmd": "cat src/app.py"})
    assert cat.ok
    held = [r for r in await repo.list_tool_calls_for_attempt(db, attempt_id) if r["held"]]
    assert held == []


async def test_run_command_protected_redirect_held(
    seeded: tuple[Database, str, str],
) -> None:
    db, run_id, attempt_id = seeded
    sandbox = FakeSandbox()
    result = await _registry(sandbox, db, run_id, attempt_id).execute(
        "run_command", {"cmd": "echo x > .github/x.yml"}
    )
    assert result.held and sandbox.execs == []


async def test_output_redacted_and_truncated(seeded: tuple[Database, str, str]) -> None:
    db, run_id, attempt_id = seeded
    leaky = "\n".join(["key AKIAIOSFODNN7EXAMPLE here", *(f"line {i}" for i in range(25))])
    sandbox = FakeSandbox(results=[ExecResult(0, leaky, "")])
    limits = LimitsConfig(tool_output_max_lines=5)
    registry = ToolRegistry(
        sandbox=sandbox,
        container="ctr",
        scopes=SCOPES,
        limits=limits,
        redactor=Redactor(),
        db=db,
        attempt_id=attempt_id,
        run_id=run_id,
        task=TASK,
        stack=STACK_REGISTRY["python-3.12"],
    )
    result = await registry.execute("read_file", {"path": "secrets.txt"})
    assert "AKIAIOSFODNN7EXAMPLE" not in result.output
    assert "REDACTED" in result.output
    assert "[truncated:" in result.output
    assert result.output.count("\n") <= limits.tool_output_max_lines
    rows = await repo.list_tool_calls_for_attempt(db, attempt_id)
    assert "AKIA" not in (rows[0]["output_blob_redacted"] or "")


async def test_write_file_uses_b64_argv_plumbing(seeded: tuple[Database, str, str]) -> None:
    db, run_id, attempt_id = seeded
    sandbox = FakeSandbox(results=[ExecResult(0, "", ""), ExecResult(0, "", "")])
    result = await _registry(sandbox, db, run_id, attempt_id).execute(
        "write_file", {"path": "src/pkg/a.py", "content": 'x = "héllo"\n'}
    )
    assert result.ok
    assert len(sandbox.execs) == 2
    mkdir_cmd = sandbox.execs[0][1]
    assert mkdir_cmd[:2] == ["mkdir", "-p"]
    assert mkdir_cmd[2] == "src/pkg"
    py_cmd = sandbox.execs[1][1]
    assert py_cmd[0:2] == ["python3", "-c"]
    assert "base64.b64decode" in py_cmd[2]
    assert base64.b64decode(py_cmd[4]).decode() == 'x = "héllo"\n'


async def test_apply_patch_stages_diff_in_tmp(seeded: tuple[Database, str, str]) -> None:
    db, run_id, attempt_id = seeded
    sandbox = FakeSandbox(results=[ExecResult(0, "", ""), ExecResult(0, "", "")])
    diff = "--- a/src/a.py\n+++ b/src/a.py\n"
    result = await _registry(sandbox, db, run_id, attempt_id).execute(
        "apply_patch", {"path": "src/a.py", "unified_diff": diff}
    )
    assert result.ok
    stage_cmd = sandbox.execs[0][1]
    assert stage_cmd[0] == "python3"
    patch_path = stage_cmd[3]
    assert patch_path.startswith("/tmp/.girder-patch-")
    assert patch_path.endswith(".diff")
    assert base64.b64decode(stage_cmd[4]).decode() == diff
    assert sandbox.execs[1][1] == ["git", "apply", "--whitespace=nowarn", patch_path]


async def test_tool_exception_becomes_output_not_raise(
    seeded: tuple[Database, str, str],
) -> None:
    db, run_id, attempt_id = seeded

    def boom(cmd: list[str]) -> ExecResult:
        raise RuntimeError("container gone")

    sandbox = FakeSandbox(side_effects=[boom])
    result = await _registry(sandbox, db, run_id, attempt_id).execute("read_file", {"path": "x"})
    assert not result.ok
    assert "error" in result.output
    rows = await repo.list_tool_calls_for_attempt(db, attempt_id)
    assert len(rows) == 1


async def test_unknown_tool_and_terminal_no_sandbox(
    seeded: tuple[Database, str, str],
) -> None:
    db, run_id, attempt_id = seeded
    sandbox = FakeSandbox()
    registry = _registry(sandbox, db, run_id, attempt_id)
    unknown = await registry.execute("nope", {})
    assert not unknown.ok
    done = await registry.execute("mark_task_complete", {"summary": "did it"})
    assert done.ok and sandbox.execs == []


async def test_verdict_column_reflects_scope_decision(
    seeded: tuple[Database, str, str],
) -> None:
    """One call per verdict class (impl-plan §6.6 R2): ALLOW, ALLOW_LOGGED and
    VIOLATION must be distinguishable in the persisted ``tool_calls.verdict``
    column (migration 011) instead of all looking identical."""
    db, run_id, attempt_id = seeded
    sandbox = FakeSandbox(results=[ExecResult(0, "body\n", "")])
    registry = _registry(sandbox, db, run_id, attempt_id)
    # ALLOW: a write inside the declared write scope.
    assert (await registry.execute("write_file", {"path": "src/a.py", "content": "x"})).ok
    # ALLOW_LOGGED: a read inside the worktree (non-strict scopes).
    assert (await registry.execute("read_file", {"path": "src/a.py"})).ok
    # VIOLATION: a write outside the write scope — held, never executed.
    held = await registry.execute("write_file", {"path": "tests/x.py", "content": "y"})
    assert held.held and held.scope_violation
    rows = await repo.list_tool_calls_for_attempt(db, attempt_id)
    verdicts = {r["tool_name"] + ":" + str(r["scope_violation"]): r["verdict"] for r in rows}
    assert verdicts["write_file:0"] == "allow"
    assert verdicts["read_file:0"] == "allow_logged"
    assert verdicts["write_file:1"] == "violation"


def test_mark_task_complete_schema_declares_no_changes_and_terminal_contract() -> None:
    """Group C: mark_task_complete must be schema-marked as the mandatory
    terminal action, with the optional no_changes boolean passthrough."""
    schema = next(t for t in TOOL_SCHEMAS if t["function"]["name"] == "mark_task_complete")
    fn = schema["function"]
    desc_lower = fn["description"].lower()
    assert "only way to finish" in desc_lower
    assert "uncommitted work is lost" in desc_lower
    param = fn["parameters"]["properties"]["no_changes"]
    assert param["type"] == "boolean"
    assert param["default"] is False
    assert "summary" in fn["parameters"]["required"]


def test_read_file_description_documents_funnel_and_clipping() -> None:
    """Group C: read_file steers the agent to outline-then-range reading and
    warns that output is clipped to the configured line budget."""
    schema = next(t for t in TOOL_SCHEMAS if t["function"]["name"] == "read_file")
    desc = schema["function"]["description"]
    assert "view_symbol_outline" in desc
    assert "line_start" in desc and "line_end" in desc
    assert "truncated" in desc


async def test_symbol_outline_dispatches_stack_plugin_argv(
    seeded: tuple[Database, str, str],
) -> None:
    """WS-07B: the .py branch routes through the stack plugin's outline argv."""
    db, run_id, attempt_id = seeded
    sandbox = FakeSandbox(results=[ExecResult(0, "1: function: foo\n", "")])
    result = await _registry(sandbox, db, run_id, attempt_id).execute(
        "view_symbol_outline", {"path": "src/a.py"}
    )
    assert result.ok
    expected = STACK_REGISTRY["python-3.12"].symbol_outline_command("src/a.py")
    assert sandbox.execs == [("ctr", expected, 120.0)]


async def test_symbol_outline_non_python_fallback_pattern(
    seeded: tuple[Database, str, str],
) -> None:
    """WS-07B: extensions outside the stack's set use the spec's grep fallback."""
    db, run_id, attempt_id = seeded
    sandbox = FakeSandbox(results=[ExecResult(0, "", "")])
    await _registry(sandbox, db, run_id, attempt_id).execute(
        "view_symbol_outline", {"path": "src/a.go"}
    )
    assert sandbox.execs == [
        (
            "ctr",
            ["grep", "-nE", r"^\s*(def|class|function|func|pub fn)\b", "src/a.go"],
            120.0,
        )
    ]


async def test_symbol_outline_routes_ts_through_node_stack(
    seeded: tuple[Database, str, str],
) -> None:
    """WS-07B WP 11.4: a .ts path under a node stack dispatches the node outline."""
    db, run_id, attempt_id = seeded
    sandbox = FakeSandbox(results=[ExecResult(0, "1: function: foo\n", "")])
    result = await _registry(
        sandbox, db, run_id, attempt_id, stack=STACK_REGISTRY["node-20"]
    ).execute("view_symbol_outline", {"path": "src/a.ts"})
    assert result.ok
    expected = STACK_REGISTRY["node-20"].symbol_outline_command("src/a.ts")
    assert sandbox.execs == [("ctr", expected, 120.0)]


async def test_symbol_outline_unknown_extension_greps_under_node_stack(
    seeded: tuple[Database, str, str],
) -> None:
    """WS-07B WP 11.4: .txt is outside every stack's extensions -> grep."""
    db, run_id, attempt_id = seeded
    sandbox = FakeSandbox(results=[ExecResult(0, "", "")])
    await _registry(
        sandbox, db, run_id, attempt_id, stack=STACK_REGISTRY["node-20"]
    ).execute("view_symbol_outline", {"path": "notes.txt"})
    assert sandbox.execs == [
        (
            "ctr",
            ["grep", "-nE", r"^\s*(def|class|function|func|pub fn)\b", "notes.txt"],
            120.0,
        )
    ]
# ---------------------------------------------------------------------------
# WP 7.1-7.5: new tools. RealSandbox actually executes argv in a tmpdir so
# the python3 -c snippets and git plumbing are exercised for real; FakeSandbox
# is used for gate/logging/script-shape assertions.
# ---------------------------------------------------------------------------


class RealSandbox(SandboxEngine):
    """Executes argv via subprocess in a host tmpdir (no podman).

    Host-execution argv translation: the tools under test emit
    container-conventional bare interpreters (``pytest``, ``python``,
    ``python3``) that may not exist on the host PATH. Rewrite those to the
    interpreter running this suite; every other argv is left untouched.
    """

    def __init__(self, cwd: object) -> None:
        self.cwd = str(cwd)
        self.execs: list[tuple[str, list[str], float]] = []

    async def start(self, spec: ContainerSpec) -> str:
        return "ctr"

    @staticmethod
    def _host_argv(cmd: list[str]) -> list[str]:
        if not cmd:
            return cmd
        head = cmd[0]
        if head == "pytest":
            return [sys.executable, "-m", "pytest", *cmd[1:]]
        if head in ("python", "python3"):
            return [sys.executable, *cmd[1:]]
        return cmd

    async def exec(
        self, name: str, cmd: list[str], *, timeout_s: float = 120.0, user: str | None = None
    ) -> ExecResult:
        self.execs.append((name, cmd, timeout_s))
        proc = await asyncio.create_subprocess_exec(
            *self._host_argv(cmd),
            cwd=self.cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        assert proc.returncode is not None
        return ExecResult(proc.returncode, out.decode(), err.decode())

    async def kill(self, name: str) -> None:
        pass

    async def exists(self, name: str) -> bool:
        return True


def _real_registry(
    sandbox: RealSandbox, db: Database, run_id: str, attempt_id: str
) -> ToolRegistry:
    return ToolRegistry(
        sandbox=sandbox,
        container="ctr",
        scopes=SCOPES,
        limits=LIMITS,
        redactor=Redactor(),
        db=db,
        attempt_id=attempt_id,
        run_id=run_id,
        task=TASK,
        stack=STACK_REGISTRY["python-3.12"],
    )


_TOOL_NAMES = {t["function"]["name"] for t in TOOL_SCHEMAS}


def test_tool_schemas_include_all_six_new_tools() -> None:
    for name in (
        "edit_file",
        "list_directory",
        "git_status",
        "git_diff",
        "run_tests",
        "search_symbols",
    ):
        assert name in _TOOL_NAMES
    assert len(_TOOL_NAMES) == 15  # 9 existing + 6 new


def test_tool_description_audit_wp76() -> None:
    by_name = {t["function"]["name"]: t["function"]["description"] for t in TOOL_SCHEMAS}
    assert "fd" not in by_name["find_files"]  # no availability detail
    assert "rg if available" not in by_name["ripgrep"]
    assert "native toolchain" in by_name["view_symbol_outline"].lower()
    assert "fall back" in by_name["view_symbol_outline"].lower()
    for denied in ("curl", "wget", "nc", "ssh", "git push", "sudo", "podman", "docker", "mount"):
        assert denied in by_name["run_command"]
    # read_file description was already improved (Group C) — still funnel-shaped.
    assert "view_symbol_outline" in by_name["read_file"]


# ------------------------------------------------------------------ edit_file


async def test_edit_file_replaces_middle_block_accurately(
    seeded: tuple[Database, str, str], tmp_path: object
) -> None:
    db, run_id, attempt_id = seeded
    src = tmp_path / "src" / "a.py"  # type: ignore[attr-defined]
    src.parent.mkdir()
    src.write_text("line1\nline2\nline3\nline4\n")
    sandbox = RealSandbox(tmp_path)
    result = await _real_registry(sandbox, db, run_id, attempt_id).execute(
        "edit_file",
        {"path": "src/a.py", "start_line": 2, "end_line": 3, "replacement": "two\nthree"},
    )
    assert result.ok, result.output
    assert src.read_text() == "line1\ntwo\nthree\nline4\n"


async def test_edit_file_first_and_last_lines(
    seeded: tuple[Database, str, str], tmp_path: object
) -> None:
    db, run_id, attempt_id = seeded
    src = tmp_path / "src" / "a.py"  # type: ignore[attr-defined]
    src.parent.mkdir()
    src.write_text("head\nmid\ntail\n")
    sandbox = RealSandbox(tmp_path)
    registry = _real_registry(sandbox, db, run_id, attempt_id)
    assert (
        await registry.execute(
            "edit_file", {"path": "src/a.py", "start_line": 1, "end_line": 1, "replacement": "HEAD"}
        )
    ).ok
    assert (
        await registry.execute(
            "edit_file", {"path": "src/a.py", "start_line": 3, "end_line": 3, "replacement": "TAIL"}
        )
    ).ok
    assert src.read_text() == "HEAD\nmid\nTAIL\n"


async def test_edit_file_preserves_untouched_crlf_lines(
    seeded: tuple[Database, str, str], tmp_path: object
) -> None:
    db, run_id, attempt_id = seeded
    src = tmp_path / "src" / "a.txt"  # type: ignore[attr-defined]
    src.parent.mkdir()
    src.write_bytes(b"one\r\ntwo\r\nthree\r\n")
    sandbox = RealSandbox(tmp_path)
    result = await _real_registry(sandbox, db, run_id, attempt_id).execute(
        "edit_file", {"path": "src/a.txt", "start_line": 2, "end_line": 2, "replacement": "TWO"}
    )
    assert result.ok, result.output
    assert src.read_bytes() == b"one\r\nTWO\nthree\r\n"


async def test_edit_file_out_of_range_fails_cleanly(
    seeded: tuple[Database, str, str], tmp_path: object
) -> None:
    db, run_id, attempt_id = seeded
    src = tmp_path / "src" / "a.py"  # type: ignore[attr-defined]
    src.parent.mkdir()
    src.write_text("one\ntwo\n")
    sandbox = RealSandbox(tmp_path)
    registry = _real_registry(sandbox, db, run_id, attempt_id)
    for bad in (
        {"start_line": 1, "end_line": 9},  # end past EOF
        {"start_line": 0, "end_line": 1},  # 0 is not a line
        {"start_line": 3, "end_line": 2},  # end before start
    ):
        result = await registry.execute(
            "edit_file", {"path": "src/a.py", **bad, "replacement": "x"}
        )
        assert not result.ok
        assert "range" in result.output
    assert src.read_text() == "one\ntwo\n"  # untouched


async def test_edit_file_out_of_scope_is_held_as_write_violation(
    seeded: tuple[Database, str, str],
) -> None:
    db, run_id, attempt_id = seeded
    sandbox = FakeSandbox()
    result = await _registry(sandbox, db, run_id, attempt_id).execute(
        "edit_file", {"path": "tests/test_x.py", "start_line": 1, "end_line": 2, "replacement": "x"}
    )
    assert not result.ok and result.held and result.scope_violation
    assert sandbox.execs == []  # never executed
    violations = await db.fetchall("SELECT kind FROM integrity_violations")
    assert [v["kind"] for v in violations] == ["out_of_scope_write"]


async def test_edit_file_protected_path_held(seeded: tuple[Database, str, str]) -> None:
    db, run_id, attempt_id = seeded
    result = await _registry(FakeSandbox(), db, run_id, attempt_id).execute(
        "edit_file", {"path": ".github/w.yml", "start_line": 1, "end_line": 1, "replacement": "x"}
    )
    assert result.held and result.scope_violation


# -------------------------------------------------------------- list_directory


async def test_list_directory_entries_types_and_sizes(
    seeded: tuple[Database, str, str], tmp_path: object
) -> None:
    db, run_id, attempt_id = seeded
    (tmp_path / "pkg").mkdir()  # type: ignore[attr-defined]
    (tmp_path / "pkg" / "mod.py").write_text("x = 1\n")
    (tmp_path / "README.md").write_text("hello\n")
    sandbox = RealSandbox(tmp_path)
    result = await _real_registry(sandbox, db, run_id, attempt_id).execute(
        "list_directory", {"path": ".", "depth": 2}
    )
    assert result.ok, result.output
    assert "./pkg [dir]" in result.output
    assert "./pkg/mod.py [file 6 bytes]" in result.output
    assert "./README.md [file 6 bytes]" in result.output


async def test_list_directory_depth_and_git_pruned(
    seeded: tuple[Database, str, str], tmp_path: object
) -> None:
    db, run_id, attempt_id = seeded
    base = tmp_path / "pkg" / "sub"  # type: ignore[attr-defined]
    base.mkdir(parents=True)
    (base / "deep.py").write_text("y = 2\n")
    (tmp_path / ".git").mkdir()  # type: ignore[attr-defined]
    (tmp_path / ".git" / "HEAD").write_text("ref\n")
    sandbox = RealSandbox(tmp_path)
    registry = _real_registry(sandbox, db, run_id, attempt_id)
    shallow = await registry.execute("list_directory", {"path": "pkg", "depth": 1})
    assert "sub" in shallow.output and "deep.py" not in shallow.output
    deep = await registry.execute("list_directory", {"path": "pkg", "depth": 3})
    assert "deep.py" in deep.output
    assert ".git" not in deep.output


async def test_list_directory_caps_output_and_errors_on_missing(
    seeded: tuple[Database, str, str], tmp_path: object
) -> None:
    db, run_id, attempt_id = seeded
    for i in range(_MATCH_CAP + 20):
        (tmp_path / f"f{i:04}.txt").write_text("x")  # type: ignore[attr-defined]
    sandbox = RealSandbox(tmp_path)
    registry = _real_registry(sandbox, db, run_id, attempt_id)
    capped = await registry.execute("list_directory", {"path": "."})
    assert "[truncated:" in capped.output
    missing = await registry.execute("list_directory", {"path": "nope"})
    assert not missing.ok and "not a directory" in missing.output


async def test_list_directory_protected_path_held(seeded: tuple[Database, str, str]) -> None:
    db, run_id, attempt_id = seeded
    result = await _registry(FakeSandbox(), db, run_id, attempt_id).execute(
        "list_directory", {"path": ".github"}
    )
    assert result.held and result.scope_violation


# ------------------------------------------------------- git_status / git_diff


async def test_git_status_short_format_and_allow_logged_verdict(
    seeded: tuple[Database, str, str],
) -> None:
    db, run_id, attempt_id = seeded
    sandbox = FakeSandbox(results=[ExecResult(0, " M src/a.py\n", "")])
    result = await _registry(sandbox, db, run_id, attempt_id).execute("git_status", {})
    assert result.ok and result.output == " M src/a.py\n"
    assert sandbox.execs == [("ctr", ["git", "status", "--short"], 120.0)]
    rows = await repo.list_tool_calls_for_attempt(db, attempt_id)
    assert rows[0]["verdict"] == "allow_logged" and not rows[0]["held"]


async def test_git_diff_staged_and_path_arguments(seeded: tuple[Database, str, str]) -> None:
    db, run_id, attempt_id = seeded
    sandbox = FakeSandbox(results=[ExecResult(0, "diff\n", ""), ExecResult(0, "diff2\n", "")])
    registry = _registry(sandbox, db, run_id, attempt_id)
    assert (await registry.execute("git_diff", {})).ok
    assert (await registry.execute("git_diff", {"path": "src/a.py", "staged": True})).ok
    assert sandbox.execs[0][1] == ["git", "diff", "--"]
    assert sandbox.execs[1][1] == ["git", "diff", "--cached", "--", "src/a.py"]


async def test_git_status_and_diff_real_repo(
    seeded: tuple[Database, str, str], tmp_path: object
) -> None:
    db, run_id, attempt_id = seeded
    env_git = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run([*env_git, "init", "-q"], cwd=tmp_path, check=True)  # noqa: ASYNC221
    (tmp_path / "a.txt").write_text("v1\n")  # type: ignore[attr-defined]
    subprocess.run([*env_git, "add", "a.txt"], cwd=tmp_path, check=True)  # noqa: ASYNC221
    subprocess.run([*env_git, "commit", "-qm", "init"], cwd=tmp_path, check=True)  # noqa: ASYNC221
    (tmp_path / "a.txt").write_text("v2\n")  # type: ignore[attr-defined]
    (tmp_path / "b.txt").write_text("new\n")  # type: ignore[attr-defined]
    sandbox = RealSandbox(tmp_path)
    registry = _real_registry(sandbox, db, run_id, attempt_id)
    status = await registry.execute("git_status", {})
    assert status.ok and " M a.txt" in status.output and "?? b.txt" in status.output
    diff = await registry.execute("git_diff", {"path": "a.txt"})
    assert diff.ok and "-v1" in diff.output and "+v2" in diff.output


# ------------------------------------------------------------------ run_tests


_JUNIT = (
    "<testsuites><testsuite>"
    '<testcase classname="tests.test_api" name="test_ok" file="tests/test_api.py"/>'
    '<testcase classname="tests.test_api" name="test_bad" file="tests/test_api.py">'
    "<failure>AssertionError</failure></testcase>"
    '<testcase classname="tests.test_api" name="test_boom" file="tests/test_api.py">'
    "<error>KeyError</error></testcase>"
    '<testcase classname="tests.test_api" name="test_skip" file="tests/test_api.py">'
    "<skipped/></testcase>"
    "</testsuite></testsuites>"
)


def test_summarize_junit_counts_and_failure_list() -> None:
    summary = _summarize_junit(_JUNIT)
    assert summary is not None
    first = summary.splitlines()[0]
    assert first == "PASSED: 1  FAILED: 1  ERROR: 1  SKIPPED: 1"
    assert "FAILURES:" in summary
    # WP 7.4: failure lines carry the failure message (em-dash + space).
    assert (
        "  tests/test_api.py::test_bad — AssertionError" in summary
    )
    assert "  tests/test_api.py::test_boom — KeyError" in summary
    # Passed/skipped tests render no failure line at all.
    assert "test_ok —" not in summary
    assert "test_skip" not in summary


def test_summarize_junit_message_attr_wins_multiline_body_first_line() -> None:
    junit = (
        "<testsuites><testsuite>"
        '<testcase classname="tests.test_api" name="test_attr" file="tests/test_api.py">'
        '<failure message="AssertionError: expected 429, got 200">'
        "E   AssertionError: expected 429, got 200\nE   at api.py:12"
        "</failure></testcase>"
        "</testsuite></testsuites>"
    )
    summary = _summarize_junit(junit)
    assert summary is not None
    assert (
        "  tests/test_api.py::test_attr — AssertionError: expected 429, got 200"
        in summary
    )
    assert "api.py:12" not in summary  # first line only


def test_summarize_junit_unparseable_returns_none() -> None:
    assert _summarize_junit("not xml <") is None
    assert _summarize_junit("<testsuite/>") is None  # zero testcases


async def test_run_tests_builds_pytest_command_and_summary(
    seeded: tuple[Database, str, str],
) -> None:
    db, run_id, attempt_id = seeded
    sandbox = FakeSandbox(results=[ExecResult(1, "short output\n", ""), ExecResult(0, _JUNIT, "")])
    result = await _registry(sandbox, db, run_id, attempt_id).execute(
        "run_tests", {"paths": ["tests/test_api.py"], "keyword": "rate_limit and not slow"}
    )
    pytest_cmd = sandbox.execs[0][1]
    # WS-02: the JUnit report path is attempt-scoped (concurrent run_tests
    # calls in one container raced on the old fixed /tmp path).
    junit = f"/tmp/.girder-run-tests-{attempt_id[-8:]}.xml"
    assert pytest_cmd[:5] == [
        "pytest",
        "--tb=short",
        "--no-header",
        "-q",
        f"--junitxml={junit}",
    ]
    assert pytest_cmd[5:] == ["tests/test_api.py", "-k", "rate_limit and not slow"]
    assert sandbox.execs[1][1] == ["cat", junit]
    assert result.output.startswith("PASSED: 1  FAILED: 1  ERROR: 1  SKIPPED: 1")
    assert "FAILURES:" in result.output
    rows = await repo.list_tool_calls_for_attempt(db, attempt_id)
    assert rows[0]["tool_name"] == "run_tests"


async def test_run_tests_missing_report_falls_back_to_raw(
    seeded: tuple[Database, str, str],
) -> None:
    db, run_id, attempt_id = seeded
    sandbox = FakeSandbox(
        results=[ExecResult(2, "collection error\n", ""), ExecResult(1, "", "cat: no file")]
    )
    result = await _registry(sandbox, db, run_id, attempt_id).execute("run_tests", {})
    assert not result.ok
    assert "collection error" in result.output


async def test_run_tests_out_of_scope_paths_held(seeded: tuple[Database, str, str]) -> None:
    db, run_id, attempt_id = seeded
    sandbox = FakeSandbox()
    # The run_command policy on the synthesized command applies: a denylisted
    # -k expression is held without execution...
    held = await _registry(sandbox, db, run_id, attempt_id).execute(
        "run_tests", {"keyword": "curl"}
    )
    assert not held.ok and held.held and held.scope_violation
    assert sandbox.execs == []
    # ...while ordinary test-path reads are allowed (reads are not write-gated).
    ok = await _registry(sandbox, db, run_id, attempt_id).execute(
        "run_tests", {"paths": ["tests/"]}
    )
    assert ok.ok


async def test_run_tests_timeout_is_capped(seeded: tuple[Database, str, str]) -> None:
    db, run_id, attempt_id = seeded
    sandbox = FakeSandbox(results=[ExecResult(0, "", ""), ExecResult(1, "", "x")])
    await _registry(sandbox, db, run_id, attempt_id).execute("run_tests", {"timeout_s": 99999})
    assert sandbox.execs[0][2] == 600.0


async def test_run_tests_real_pytest_subset(
    seeded: tuple[Database, str, str], tmp_path: object
) -> None:
    db, run_id, attempt_id = seeded
    tests_dir = tmp_path / "tests"  # type: ignore[attr-defined]
    tests_dir.mkdir()
    (tests_dir / "test_t.py").write_text(
        "def test_pass():\n    assert True\n\n\ndef test_fail():\n    assert 1 == 2\n"
    )
    sandbox = RealSandbox(tmp_path)
    result = await _real_registry(sandbox, db, run_id, attempt_id).execute(
        "run_tests", {"paths": ["tests/test_t.py"]}
    )
    assert result.output.splitlines()[0] == "PASSED: 1  FAILED: 1  ERROR: 0"
    assert "  tests/test_t.py::test_fail" in result.output


# -------------------------------------------------------------- search_symbols


async def test_search_symbols_composes_rg_regex_per_kind(
    seeded: tuple[Database, str, str],
) -> None:
    db, run_id, attempt_id = seeded
    sandbox = FakeSandbox(results=[ExecResult(0, "x\n", "") for _ in range(3)])
    registry = _registry(sandbox, db, run_id, attempt_id)
    await registry.execute("search_symbols", {"name": "RateLimiter", "kind": "definition"})
    await registry.execute("search_symbols", {"name": "handle_request", "kind": "usage"})
    await registry.execute("search_symbols", {"name": "mymod", "kind": "export", "path": "src"})
    scripts = [cmd[2] for _, cmd, _ in sandbox.execs]
    assert "^(async def|def|class)\\s+RateLimiter\\b" in scripts[0]
    assert "\\bhandle_request\\s*\\(" in scripts[1]
    assert "__all__" in scripts[2] and "*mymod*" in scripts[2] and "src" in scripts[2]


async def test_search_symbols_rejects_unknown_kind(seeded: tuple[Database, str, str]) -> None:
    db, run_id, attempt_id = seeded
    result = await _registry(FakeSandbox(), db, run_id, attempt_id).execute(
        "search_symbols", {"name": "x", "kind": "schema"}
    )
    assert not result.ok and "kind" in result.output


async def test_search_symbols_real_definitions_and_usages(
    seeded: tuple[Database, str, str], tmp_path: object
) -> None:
    db, run_id, attempt_id = seeded
    (tmp_path / "m.py").write_text("class Foo:\n    pass\n\n\ndef bar():\n    return Foo()\n")
    sandbox = RealSandbox(tmp_path)
    registry = _real_registry(sandbox, db, run_id, attempt_id)
    defs = await registry.execute("search_symbols", {"name": "Foo", "kind": "definition"})
    assert "class Foo:" in defs.output and defs.output.endswith(":1:class Foo:\n")
    uses = await registry.execute("search_symbols", {"name": "Foo", "kind": "usage"})
    assert "return Foo()" in uses.output



# ------------------------------------------------ WP 8.2 structured scratchpad


async def test_registry_populates_structured_scratchpad(
    seeded: tuple[Database, str, str],
) -> None:
    """Successful reads/writes/tests are recorded in the shared scratchpad,
    deduped preserving order; held calls contribute nothing."""
    db, run_id, attempt_id = seeded
    pad = Scratchpad()
    registry = _registry(FakeSandbox(), db, run_id, attempt_id, scratchpad=pad)
    ok_results = [ExecResult(0, "body\n", ""), ExecResult(0, "", "")]
    sandbox = FakeSandbox(results=ok_results)
    registry.sandbox = sandbox
    await registry.execute("read_file", {"path": "src/a.py"})
    await registry.execute("read_file", {"path": "src/a.py"})  # dedupe
    await registry.execute("write_file", {"path": "src/b.py", "content": "x"})
    # run_tests: pytest cmd exec + junit cat exec, both scripted
    registry.sandbox = FakeSandbox(
        results=[
            ExecResult(1, "out\n", ""),
            ExecResult(0, _JUNIT, ""),
            ExecResult(0, _JUNIT, ""),
        ]
    )
    await registry.execute("run_tests", {})
    await registry.execute("write_file", {"path": "tests/test_x.py", "content": "boom"})  # held
    assert pad.files_read == ["src/a.py"]
    assert pad.files_written == ["src/b.py"]  # the held out-of-scope write is NOT recorded
    assert pad.test_results and pad.test_results[0].startswith("PASSED: 1  FAILED: 1")
    # serialization round-trips through the compact JSON block
    assert '"files_read": ["src/a.py"]' in pad.to_json()


async def test_run_tests_junit_path_is_attempt_scoped(
    seeded: tuple[Database, str, str],
) -> None:
    """WS-02 fix: concurrent run_tests calls sharing a container must not race
    on one fixed /tmp JUnit path — the report path embeds the attempt id."""
    db, run_id, attempt_id = seeded
    sandbox = FakeSandbox(results=[ExecResult(0, "", ""), ExecResult(0, _JUNIT, "")])
    await _registry(sandbox, db, run_id, attempt_id).execute("run_tests", {})
    junit = f"/tmp/.girder-run-tests-{attempt_id[-8:]}.xml"
    assert f"--junitxml={junit}" in sandbox.execs[0][1]
    assert sandbox.execs[1][1] == ["cat", junit]
