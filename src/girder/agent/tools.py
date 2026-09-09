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
handed to ``sh -c`` inside the (network=none) container.
"""

from __future__ import annotations

import base64
import json
import re
import shlex
import time
from dataclasses import dataclass
from typing import Any

from girder.agent.context import estimate_tokens
from girder.config import LimitsConfig
from girder.db import repo
from girder.db.engine import Database
from girder.db.models import IntegrityKind, Task
from girder.guard.redact import Redactor, redact_and_log
from girder.guard.scope import TaskScopes, Verdict, check_tool_call
from girder.sandbox.engine import SandboxEngine

_TERMINAL_TOOLS = ("mark_task_complete", "request_spec_amendment")
_WRITE_TOOLS = ("write_file", "apply_patch")
_READ_TOOLS = ("read_file", "ripgrep", "find_files", "view_symbol_outline")

_B64_DECODE_SNIPPET = (
    "import base64,sys,pathlib; pathlib.Path(sys.argv[1]).write_bytes("
    "base64.b64decode(sys.argv[2]))"
)
_AST_OUTLINE_SNIPPET = (
    "import ast,sys;"
    "t=ast.parse(open(sys.argv[1]).read());"
    "[print(f\"{n.lineno}: {'class' if isinstance(n,ast.ClassDef) else 'function'}:"
    " {n.name}\") for n in ast.walk(t)"
    " if isinstance(n,(ast.ClassDef,ast.FunctionDef,ast.AsyncFunctionDef))]"
)

_PATCH_PREFIX = "/tmp/.girder-patch"
_MATCH_CAP = 200  # rg / grep match cap before truncation

# run_command denylist — word-boundary tokens plus explicit network verbs.
_DENY_TOKENS = re.compile(r"\b(curl|wget|nc|ncat|netcat|ssh|scp|sftp|sudo|podman|docker|mount)\b")
_TEST_SIGNAL = re.compile(r"(tests?/|test_|_test|conftest|pytest)", re.IGNORECASE)

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file from the worktree (optionally a line range).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "line_start": {"type": "integer"},
                    "line_end": {"type": "integer"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a file inside the declared write scope.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apply_patch",
            "description": "Apply a unified diff to the worktree.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Primary file the diff touches."},
                    "unified_diff": {"type": "string"},
                },
                "required": ["path", "unified_diff"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_files",
            "description": "Find files by glob pattern (fd if available, find otherwise).",
            "parameters": {
                "type": "object",
                "properties": {"glob": {"type": "string"}},
                "required": ["glob"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ripgrep",
            "description": "Regex search across the worktree (rg if available, grep otherwise).",
            "parameters": {
                "type": "object",
                "properties": {
                    "regex": {"type": "string"},
                    "path": {"type": "string"},
                    "glob": {"type": "string"},
                },
                "required": ["regex"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "view_symbol_outline",
            "description": "List classes/functions with line numbers for a source file.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a shell command inside the sandbox (no network).",
            "parameters": {
                "type": "object",
                "properties": {
                    "cmd": {"type": "string"},
                    "timeout_s": {"type": "number"},
                },
                "required": ["cmd"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mark_task_complete",
            "description": "Declare the task done. Provide a short summary of what changed.",
            "parameters": {
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": ["summary"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "request_spec_amendment",
            "description": "The frozen spec is impossible/contradictory — request an amendment.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string"},
                    "suggested_change": {"type": "string"},
                },
                "required": ["reason", "suggested_change"],
            },
        },
    },
]


@dataclass
class ToolExecResult:
    ok: bool
    output: str
    held: bool = False
    scope_violation: bool = False


class CommandDenied(RuntimeError):
    """A run_command string matched the denylist; it was never executed."""


def screen_command(cmd: str) -> None:
    """Raise :class:`CommandDenied` if the command string is denylisted."""
    if _DENY_TOKENS.search(cmd):
        raise CommandDenied(f"denied: network/escalation command: {cmd[:120]}")
    if "git push" in cmd:
        raise CommandDenied(f"denied: git push is orchestrator-only: {cmd[:120]}")
    if re.search(r"\bchmod\b", cmd) and _TEST_SIGNAL.search(cmd):
        raise CommandDenied(f"denied: chmod targeting a test-signal path: {cmd[:120]}")


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

    async def execute(self, name: str, args: dict[str, Any]) -> ToolExecResult:
        """Gate, dispatch, truncate, redact, log. Never raises outward."""
        started = time.monotonic()
        input_json = _dumps(args)

        # 1. Scope gate — violations are intercepted, never executed (§6.6 R2).
        verdict = check_tool_call(name, args, self.scopes)
        if verdict is Verdict.VIOLATION:
            return await self._violation(name, args, input_json)

        # 2. Terminal tools never touch the sandbox.
        if name in _TERMINAL_TOOLS:
            text = await self._finalize(
                name, input_json, f"{name} recorded", True, started
            )
            return ToolExecResult(ok=True, output=text)

        try:
            ok, raw = await self._dispatch(name, args)
        except CommandDenied as exc:
            text = await self._finalize(name, input_json, str(exc), False, started, held=True)
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
        text = await self._finalize(name, input_json, raw, ok, started)
        return ToolExecResult(ok=ok, output=text)

    # ------------------------------------------------------------- dispatch

    async def _dispatch(self, name: str, args: dict[str, Any]) -> tuple[bool, str]:
        if name == "read_file":
            return await self._read_file(args)
        if name == "write_file":
            return await self._write_file(args)
        if name == "apply_patch":
            return await self._apply_patch(args)
        if name == "find_files":
            return await self._find_files(args)
        if name == "ripgrep":
            return await self._ripgrep(args)
        if name == "view_symbol_outline":
            return await self._symbol_outline(args)
        if name == "run_command":
            return await self._run_command(args)
        return False, f"error: unknown tool {name!r}"

    async def _exec(
        self, cmd: list[str], *, timeout_s: float = 120.0
    ) -> tuple[bool, str]:
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
        if path.endswith(".py"):
            return await self._exec(["python3", "-c", _AST_OUTLINE_SNIPPET, path])
        return await self._exec(
            ["grep", "-nE", r"^\s*(def|class|function)\b", path]
        )

    async def _run_command(self, args: dict[str, Any]) -> tuple[bool, str]:
        cmd = str(args["cmd"])
        screen_command(cmd)  # raises CommandDenied — execute() maps it to a held result
        timeout = min(float(args.get("timeout_s") or 300.0), 300.0)
        return await self._exec(["sh", "-c", cmd], timeout_s=timeout)

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
    ) -> str:
        """Truncate -> redact -> persist; returns the conversation-safe text."""
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
