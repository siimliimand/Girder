"""Unit tests for the tool registry: gating, sandbox plumbing, redaction."""

from __future__ import annotations

import base64

import pytest

from girder.agent.tools import CommandDenied, ToolRegistry, screen_command
from girder.config import LimitsConfig
from girder.db import repo
from girder.db.engine import Database
from girder.db.models import Task, TaskStatus, TaskType
from girder.guard.redact import Redactor
from girder.guard.scope import TaskScopes
from girder.sandbox.engine import ContainerSpec, ExecResult, SandboxEngine


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
    sandbox: FakeSandbox, db: Database, run_id: str, attempt_id: str
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


async def test_run_command_denied_vs_benign(seeded: tuple[Database, str, str]) -> None:
    db, run_id, attempt_id = seeded
    sandbox = FakeSandbox(results=[ExecResult(0, "ok\n", "")])
    registry = _registry(sandbox, db, run_id, attempt_id)

    denied = await registry.execute("run_command", {"cmd": "curl http://evil.example"})
    assert not denied.ok and denied.held
    assert "held" in denied.output or "denied" in denied.output

    benign = await registry.execute("run_command", {"cmd": "python -m pytest -q"})
    assert benign.ok
    assert ("ctr", ["sh", "-c", "python -m pytest -q"]) in [
        (n, c) for n, c, _ in sandbox.execs
    ]
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
    leaky = "\n".join(
        ["key AKIAIOSFODNN7EXAMPLE here", *(f"line {i}" for i in range(25))]
    )
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
    result = await _registry(sandbox, db, run_id, attempt_id).execute(
        "read_file", {"path": "x"}
    )
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
