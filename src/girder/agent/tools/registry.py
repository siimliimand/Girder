"""Agent tool registry: schemas, scope gating, sandbox execution (impl-plan §6.10).

Two hard constraints shape every implementation:

* **No stdin.** ``SandboxEngine.exec`` takes only argv, so every in-container
  write goes through ``base64``-encoded argv decoded by a ``python3 -c``
  one-liner — no shell-quoting hazards, no temp files on the host.
* **Nothing raw escapes.** Every output is truncated to the configured line /
  token budget, then redacted via the §8.6 pipeline, then persisted to
  ``tool_calls`` — in that order — before the caller ever sees it.

``run_command`` gets its own screening layer on top of the scope gate: the
command string is matched against a network/escalation denylist before it is
handed to ``sh -c`` inside the (network=none) container. The scope gate itself
now also applies the §6.6 R2 write policy to the command's path arguments
(redirect operands, ``tee`` targets) — see ``guard.scope.run_command_path_args``.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import shlex
import time
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from girder.agent.context import Scratchpad, estimate_tokens
from girder.agent.tools.gating import (
    CommandDenied,
    _GATED_NEW_TOOLS,
    _READ_TOOLS,
    _TERMINAL_TOOLS,
    _WRITE_TOOLS,
    _gate_new_tool,
    _run_tests_command,
    screen_command,
)
from girder.agent.tools.snippets import (
    _B64_DECODE_SNIPPET,
    _DIR_LIST_SNIPPET,
    _EDIT_SNIPPET,
    _MATCH_CAP,
    _PATCH_PREFIX,
)
from girder.config import LimitsConfig
from girder.db import repo
from girder.db.engine import Database
from girder.db.models import IntegrityKind, Task
from girder.guard.redact import Redactor, redact_and_log
from girder.guard.scope import TaskScopes, Verdict, check_tool_call
from girder.sandbox.engine import SandboxEngine
from girder.stacks import StackPlugin

logger = logging.getLogger(__name__)

# git-status --porcelain XY codes whose working tree is UNMERGED (conflict):
# completing on top of these is judged downstream, never bounced (see
# worktree_dirty_with_output).
_UNMERGED_XY_CODES = {"UU", "AA", "DD", "AU", "UA", "DU", "UD"}


@dataclass
class ToolExecResult:
    ok: bool
    output: str
    held: bool = False
    scope_violation: bool = False


def _summarize_junit(xml_text: str) -> str | None:
    """JUnit XML -> stable one-line summary plus a full failure list.

    Stable format (WS-02 parses the first line)::

        PASSED: 42  FAILED: 2  ERROR: 0  [SKIPPED: 1]

        FAILURES:
          tests/test_api.py::test_rate_limit — AssertionError: expected 429, got 200
    """
    # Deferred import: girder.orchestrator.__init__ pulls in conflict.py ->
    # agent.runtime, which would make agent.tools -> orchestrator a cycle.
    from girder.orchestrator.baseline import parse_junit_xml

    try:
        results = parse_junit_xml(xml_text)
    except ET.ParseError:
        return None
    if not results:
        return None
    counts: dict[str, int] = {}
    failures: list[str] = []
    for r in results.values():
        counts[r.status] = counts.get(r.status, 0) + 1
        if r.status in ("failed", "error"):
            failures.append(
                f"{r.test_id} — {r.message}" if r.message else r.test_id
            )
    head = (
        f"PASSED: {counts.get('passed', 0)}  "
        f"FAILED: {counts.get('failed', 0)}  "
        f"ERROR: {counts.get('error', 0)}"
    )
    if counts.get("skipped"):
        head += f"  SKIPPED: {counts['skipped']}"
    lines = [head]
    if failures:
        lines.append("")
        lines.append("FAILURES:")
        lines.extend(f"  {entry}" for entry in failures)
    return "\n".join(lines)


def _truncate(text: str, limits: LimitsConfig) -> str:
    """Line budget first, then the token budget (R8 estimate)."""
    lines = text.splitlines()
    if len(lines) > limits.tool_output_max_lines:
        dropped = len(lines) - limits.tool_output_max_lines
        text = "\n".join(lines[: limits.tool_output_max_lines])
        text += f"\n[truncated: {dropped} more lines]"
    if estimate_tokens(text) > limits.tool_output_max_tokens:
        text = text[: limits.tool_output_max_tokens * 4] + "\n[truncated: output too long]"
    return text


def _b64(data: str) -> str:
    return base64.b64encode(data.encode()).decode()


class ToolRegistry:
    """Executes one tool call at a time against one sandboxed attempt."""

    def __init__(
        self,
        *,
        sandbox: SandboxEngine,
        container: str,
        scopes: TaskScopes,
        limits: LimitsConfig,
        redactor: Redactor,
        db: Database,
        attempt_id: str,
        run_id: str,
        task: Task,
        stack: StackPlugin,
        scratchpad: Scratchpad | None = None,
    ) -> None:
        self.sandbox = sandbox
        self.container = container
        self.scopes = scopes
        self.limits = limits
        self.redactor = redactor
        self.db = db
        self.attempt_id = attempt_id
        self.run_id = run_id
        self.task = task
        self.stack = stack
        # WP 8.2: structured scratchpad shared with the runtime; the registry
        # records reads/writes/tests on every executed call (dedupe upstream).
        self.scratchpad = scratchpad if scratchpad is not None else Scratchpad()
        # Clean name -> handler registry (WS-02 hooks tool execution here).
        self._handlers: dict[str, Callable[[dict[str, Any]], Awaitable[tuple[bool, str]]]] = {
            "read_file": self._read_file,
            "write_file": self._write_file,
            "apply_patch": self._apply_patch,
            "find_files": self._find_files,
            "ripgrep": self._ripgrep,
            "view_symbol_outline": self._symbol_outline,
            "run_command": self._run_command,
            "edit_file": self._edit_file,
            "list_directory": self._list_directory,
            "git_status": self._git_status,
            "git_diff": self._git_diff,
            "run_tests": self._run_tests,
            "search_symbols": self._search_symbols,
        }
        # Scratchpad extraction (WP 8.2): tool name -> which list a successful
        # call contributes to. Read tools contribute only when a path argument
        # is present (dedupe preserving order happens in Scratchpad).
        self._scratchpad_reads = frozenset(
            {
                "read_file",
                "find_files",
                "ripgrep",
                "search_symbols",
                "list_directory",
                "git_status",
                "git_diff",
                "view_symbol_outline",
            }
        )

    async def execute(self, name: str, args: dict[str, Any]) -> ToolExecResult:
        """Gate, dispatch, truncate, redact, log. Never raises outward."""
        started = time.monotonic()
        input_json = _dumps(args)

        # 1. Scope gate — violations are intercepted, never executed (§6.6 R2).
        verdict = check_tool_call(name, args, self.scopes)
        if verdict is Verdict.ALLOW and name in _GATED_NEW_TOOLS:
            verdict = _gate_new_tool(name, args, self.scopes)
        if verdict is Verdict.VIOLATION:
            return await self._violation(name, args, input_json)

        # 2. Terminal tools never touch the sandbox.
        if name in _TERMINAL_TOOLS:
            text = await self._finalize(
                name, input_json, f"{name} recorded", True, started, verdict=verdict
            )
            return ToolExecResult(ok=True, output=text)

        try:
            ok, raw = await self._dispatch(name, args)
            self._record_scratchpad(name, args, raw, ok=ok)
        except CommandDenied as exc:
            # Denylisted commands are held like scope violations — the verdict
            # column must agree with scope_violation=1/held=1.
            text = await self._finalize(
                name,
                input_json,
                str(exc),
                False,
                started,
                held=True,
                verdict=Verdict.VIOLATION,
            )
            await repo.insert_integrity_violation(
                self.db,
                self.run_id,
                IntegrityKind.SCOPE_VIOLATION,
                {"tool": name, "args": args, "detail": str(exc)},
                task_id=self.task.id,
                attempt_id=self.attempt_id,
            )
            return ToolExecResult(ok=False, output=text, held=True, scope_violation=True)
        except Exception as exc:
            ok, raw = False, f"error: {type(exc).__name__}: {exc}"
        text = await self._finalize(name, input_json, raw, ok, started, verdict=verdict)
        return ToolExecResult(ok=ok, output=text)

    # ------------------------------------------------------------- dispatch

    async def _dispatch(self, name: str, args: dict[str, Any]) -> tuple[bool, str]:
        handler = self._handlers.get(name)
        if handler is None:
            return False, f"error: unknown tool {name!r}"
        return await handler(args)

    def _record_scratchpad(
        self, name: str, args: dict[str, Any], raw: str, *, ok: bool
    ) -> None:
        """WP 8.2: feed an executed tool call into the structured scratchpad.

        Read tools contribute their path argument (when one is involved);
        write tools contribute the written path; run_tests contributes its
        stable first-line summary (even on a red suite — a FAILED count is
        exactly what a retry needs). Purely additive: never raises.
        """
        try:
            if name == "run_tests":
                first = raw.splitlines()[0].strip() if raw.splitlines() else ""
                if first.startswith("PASSED:"):
                    self.scratchpad.note_test(first)
            elif not ok:
                return
            elif name in self._scratchpad_reads:
                self.scratchpad.note_read(str(args.get("path") or ""))
            elif name in _WRITE_TOOLS:
                self.scratchpad.note_write(str(args.get("path") or ""))
        except Exception:  # pragma: no cover - observability must never bite
            logger.debug("scratchpad recording failed for %s", name, exc_info=True)

    async def worktree_dirty_with_output(self) -> tuple[bool, str]:
        """``(bounce-worthy dirt, porcelain output)`` for the terminal-tool gate.

        Best-effort: a failed probe yields ``(False, "")`` so the caller fails
        open — the verify leftover audit remains the backstop. Unmerged
        entries (``UU``/``AA``/…, e.g. the conflict resolver's pre-staged
        ``merge --no-commit`` worktree) are NOT bounce-worthy: completing on
        top of a conflict is a legitimate failure declaration the conflict
        machinery judges — telling the model to "commit" would stage conflict
        markers.
        """
        try:
            ok, out = await self._exec(["git", "status", "--porcelain"])
        except Exception:
            return False, ""
        if not ok:
            return False, ""
        committable = [
            line for line in out.splitlines() if line.strip() and line[:2] not in _UNMERGED_XY_CODES
        ]
        return bool(committable), out

    async def _exec(self, cmd: list[str], *, timeout_s: float = 120.0) -> tuple[bool, str]:
        result = await self.sandbox.exec(self.container, cmd, timeout_s=timeout_s)
        if result.timed_out:
            return False, "error: timed out"
        out = result.stdout
        if result.exit_code != 0:
            out += ("\n" if out and not out.endswith("\n") else "") + result.stderr
            return False, out.strip() or f"error: exit code {result.exit_code}"
        return True, out

    async def _read_file(self, args: dict[str, Any]) -> tuple[bool, str]:
        path = str(args["path"])
        try:
            s = int(args.get("line_start") or 1)
            e = int(args.get("line_end") or 0)
        except (TypeError, ValueError) as exc:
            return False, f"error: invalid line range: {exc}"
        span = f"{s},{e}p" if e else f"{s},$p" if s > 1 else "1,$p"
        return await self._exec(["sed", "-n", span, path])

    async def _write_file(self, args: dict[str, Any]) -> tuple[bool, str]:
        path = str(args["path"])
        parent = path.rsplit("/", 1)[0] if "/" in path else "."
        ok1, out1 = await self._exec(["mkdir", "-p", parent])
        if not ok1:
            return False, f"error: mkdir failed: {out1}"
        return await self._exec(
            ["python3", "-c", _B64_DECODE_SNIPPET, path, _b64(str(args.get("content", "")))]
        )

    async def _apply_patch(self, args: dict[str, Any]) -> tuple[bool, str]:
        patch_path = f"{_PATCH_PREFIX}-{self.attempt_id[-8:]}.diff"
        ok1, out1 = await self._exec(
            ["python3", "-c", _B64_DECODE_SNIPPET, patch_path, _b64(str(args["unified_diff"]))]
        )
        if not ok1:
            return False, f"error: staging patch failed: {out1}"
        return await self._exec(["git", "apply", "--whitespace=nowarn", patch_path])

    async def _find_files(self, args: dict[str, Any]) -> tuple[bool, str]:
        pattern = shlex.quote(str(args["glob"]))
        script = (
            "if command -v fd >/dev/null 2>&1; then fd --glob "
            f"{pattern}; else find . -path ./.git -prune -o -name {pattern} -print; fi"
        )
        return await self._exec(["sh", "-c", script])

    async def _ripgrep(self, args: dict[str, Any]) -> tuple[bool, str]:
        regex = shlex.quote(str(args["regex"]))
        path = shlex.quote(str(args.get("path") or "."))
        script = (
            "if command -v rg >/dev/null 2>&1; then "
            f"rg -n -- {regex} {path}; "
            f"else grep -rnE -- {regex} {path}; fi | head -n {_MATCH_CAP}"
        )
        return await self._exec(["sh", "-c", script])

    async def _symbol_outline(self, args: dict[str, Any]) -> tuple[bool, str]:
        path = str(args["path"])
        if path.lower().endswith(self.stack.symbol_outline_extensions()):
            return await self._exec(self.stack.symbol_outline_command(path))
        return await self._exec(
            ["grep", "-nE", r"^\s*(def|class|function|func|pub fn)\b", path]
        )

    async def _run_command(self, args: dict[str, Any]) -> tuple[bool, str]:
        cmd = str(args["cmd"])
        screen_command(cmd)  # raises CommandDenied — execute() maps it to a held result
        timeout = min(float(args.get("timeout_s") or 300.0), 300.0)
        return await self._exec(["sh", "-c", cmd], timeout_s=timeout)

    async def _edit_file(self, args: dict[str, Any]) -> tuple[bool, str]:
        path = str(args["path"])
        try:
            s = int(args["start_line"])
            e = int(args["end_line"])
        except (KeyError, TypeError, ValueError) as exc:
            return False, f"error: invalid line range: {exc}"
        if s < 1 or e < s:
            return False, f"error: invalid line range: {s}..{e} (1-indexed, end >= start)"
        return await self._exec(
            [
                "python3",
                "-c",
                _EDIT_SNIPPET,
                path,
                str(s),
                str(e),
                _b64(str(args.get("replacement", ""))),
            ]
        )

    async def _list_directory(self, args: dict[str, Any]) -> tuple[bool, str]:
        root = str(args.get("path") or ".")
        try:
            depth = max(1, min(3, int(args.get("depth") or 1)))
        except (TypeError, ValueError) as exc:
            return False, f"error: invalid depth: {exc}"
        ok, out = await self._exec(["python3", "-c", _DIR_LIST_SNIPPET, root, str(depth)])
        lines = out.splitlines()
        if len(lines) > _MATCH_CAP:
            dropped = len(lines) - _MATCH_CAP
            out = "\n".join(lines[:_MATCH_CAP]) + f"\n[truncated: {dropped} more entries]"
        return ok, out

    async def _git_status(self, args: dict[str, Any]) -> tuple[bool, str]:
        return await self._exec(["git", "status", "--short"])

    async def _git_diff(self, args: dict[str, Any]) -> tuple[bool, str]:
        cmd = ["git", "diff"]
        if args.get("staged"):
            cmd.append("--cached")
        cmd.append("--")
        path = str(args.get("path") or "").strip()
        if path:
            cmd.append(path)
        return await self._exec(cmd)

    async def _run_tests(self, args: dict[str, Any]) -> tuple[bool, str]:
        raw_paths = args.get("paths") or []
        paths = [str(p) for p in raw_paths]
        timeout = min(float(args.get("timeout_s") or 120.0), 600.0)
        # WS-02 fix: the JUnit path is attempt-scoped — the old fixed
        # /tmp/.girder-run-tests.xml raced between concurrent run_tests
        # invocations inside one container.
        junit_path = f"/tmp/.girder-run-tests-{self.attempt_id[-8:]}.xml"
        cmd = ["pytest", "--tb=short", "--no-header", "-q", f"--junitxml={junit_path}"]
        cmd.extend(paths)
        keyword = str(args.get("keyword") or "")
        if keyword:
            cmd.extend(["-k", keyword])
        screen_command(_run_tests_command(args))  # same denylist as run_command
        ok, raw = await self._exec(cmd, timeout_s=timeout)
        # The JUnit report lives inside the container; fetch it, then parse.
        xml_ok, xml_text = await self._exec(["cat", junit_path])
        if not xml_ok or not xml_text.strip():
            # No report (collection error, pytest missing, …): fall back to
            # the raw pytest output rather than inventing a summary.
            return ok, raw.strip() or "error: no test report produced"
        summary = _summarize_junit(xml_text)
        if summary is None:
            return ok, raw.strip() or "error: unparseable test report"
        return ok, summary

    async def _search_symbols(self, args: dict[str, Any]) -> tuple[bool, str]:
        name = re.escape(str(args["name"]))
        path = shlex.quote(str(args.get("path") or "."))
        kind = str(args.get("kind") or "definition")
        if kind == "definition":
            regex = shlex.quote(rf"^(async def|def|class)\s+{name}\b")
        elif kind == "usage":
            regex = shlex.quote(rf"\b{name}\s*\(")
        elif kind == "export":
            # Phase 1: __all__ assignments, restricted (rg only) to files
            # whose path matches the module name. WS-07B replaces this with
            # stack-plugin commands.
            regex = shlex.quote(r"__all__")
            script = (
                "if command -v rg >/dev/null 2>&1; then "
                f"rg -n -g '*{name}*' -- {regex} {path}; "
                f"else grep -rnE -- {regex} {path}; fi | head -n {_MATCH_CAP}"
            )
            return await self._exec(["sh", "-c", script])
        else:
            return False, f"error: unknown kind {kind!r} (definition|usage|export)"
        script = (
            "if command -v rg >/dev/null 2>&1; then "
            f"rg -n -- {regex} {path}; "
            f"else grep -rnE -- {regex} {path}; fi | head -n {_MATCH_CAP}"
        )
        return await self._exec(["sh", "-c", script])

    # ------------------------------------------------------ logging helpers

    async def _finalize(
        self,
        name: str,
        input_json: str,
        raw: str,
        ok: bool,
        started: float,
        *,
        held: bool = False,
        verdict: Verdict,
    ) -> str:
        """Truncate -> redact -> persist; returns the conversation-safe text.

        The ScopeGuard ``verdict`` is persisted verbatim (migration 011) so
        ALLOW_LOGGED stays distinguishable from ALLOW in the audit trail
        (impl-plan §6.6 R2).
        """
        duration_ms = int((time.monotonic() - started) * 1000)
        text = await redact_and_log(
            self.redactor,
            _truncate(raw, self.limits),
            source_field=f"tool:{name}",
            db=self.db,
            attempt_id=self.attempt_id,
        )
        await repo.insert_tool_call(
            self.db,
            attempt_id=self.attempt_id,
            tool_name=name,
            input_json=input_json,
            output_redacted=text,
            duration_ms=duration_ms,
            scope_violation=held,
            held=held,
            verdict=verdict.value,
        )
        return text

    async def _violation(self, name: str, args: dict[str, Any], input_json: str) -> ToolExecResult:
        if name in _WRITE_TOOLS:
            kind = IntegrityKind.OUT_OF_SCOPE_WRITE
        elif name in _READ_TOOLS:
            kind = IntegrityKind.PROTECTED_READ
        else:
            kind = IntegrityKind.SCOPE_VIOLATION
        detail = {"tool": name, "args": args}
        refusal = f"[held for review] {name} was blocked by scope policy and not executed."
        text = await redact_and_log(
            self.redactor,
            refusal,
            source_field=f"tool:{name}",
            db=self.db,
            attempt_id=self.attempt_id,
        )
        await repo.insert_tool_call(
            self.db,
            attempt_id=self.attempt_id,
            tool_name=name,
            input_json=input_json,
            output_redacted=text,
            duration_ms=0,
            scope_violation=True,
            held=True,
            verdict=Verdict.VIOLATION.value,
        )
        await repo.insert_integrity_violation(
            self.db,
            self.run_id,
            kind,
            detail,
            task_id=self.task.id,
            attempt_id=self.attempt_id,
        )
        return ToolExecResult(ok=False, output=text, held=True, scope_violation=True)


def _dumps(args: dict[str, Any]) -> str:
    return json.dumps(args, sort_keys=True, default=str)
