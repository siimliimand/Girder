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
import re
import shlex
import time
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable
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
_WRITE_TOOLS = ("write_file", "apply_patch", "edit_file")
_READ_TOOLS = (
    "read_file",
    "ripgrep",
    "find_files",
    "view_symbol_outline",
    "list_directory",
    "git_status",
    "git_diff",
    "run_tests",
    "search_symbols",
)

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
_RUN_TESTS_JUNIT = "/tmp/.girder-run-tests.xml"

# edit_file (WP 7.1): replace lines start..end (1-indexed, inclusive) with the
# base64-argv replacement. Out-of-range ranges fail via assert — a silent
# clamp would corrupt the wrong lines.
_EDIT_SNIPPET = (
    "import sys, pathlib, base64; "
    "p = pathlib.Path(sys.argv[1]); "
    "lines = p.read_text().splitlines(keepends=True); "
    "s, e = int(sys.argv[2]) - 1, int(sys.argv[3]); "
    "repl = base64.b64decode(sys.argv[4]).decode(); "
    "assert 1 <= s + 1 <= e <= len(lines), ("
    "f'line range {s + 1}..{e} out of range: file has {len(lines)} lines'); "
    "lines[s:e] = [repl] if repl.endswith('\\n') else [repl + '\\n']; "
    "p.write_text(''.join(lines))"
)

# list_directory (WP 7.2): os.walk with a depth limit; depth counts tree
# levels (1 = direct children of `root`). Output lines are
# "<path> [dir]" / "<path> [file N bytes]", .git pruned, entries sorted.
_DIR_LIST_SNIPPET = (
    "import os, sys; "
    "root, maxd = sys.argv[1], max(1, min(3, int(sys.argv[2]))); "
    "if not os.path.isdir(root): raise SystemExit('error: not a directory: ' + root); "
    "base = os.path.normpath(root); "
    "out = []; "
    "for d, dirs, files in os.walk(root): "
    "rel = os.path.relpath(d, base); "
    "depth = 0 if rel == '.' else rel.count(os.sep) + 1; "
    "dirs[:] = sorted(x for x in dirs if x != '.git'); "
    "files = sorted(files); "
    "out.extend(os.path.join(d, n) + ' [dir]' for n in dirs); "
    "out.extend("
    "os.path.join(d, n) + ' [file ' + str("
    "os.path.getsize(os.path.join(d, n)) if os.path.exists(os.path.join(d, n)) else 0"
    ") + ' bytes]' for n in files); "
    "if depth + 1 >= maxd: dirs[:] = []; "
    "print('\\n'.join(out))"
)

# New tools whose scope verdict is derived from an existing gate shape
# (R-SP7 scope policy applied without touching guard/scope.py, which WS-01
# does not own): edit_file -> write_file policy on `path`,
# list_directory/search_symbols -> read_file policy on `path`,
# run_tests -> run_command policy on the synthesized pytest command.
_NEW_TOOL_ALIASES = {
    "edit_file": "write_file",
    "list_directory": "read_file",
    "search_symbols": "read_file",
}
_GATED_NEW_TOOLS = frozenset(_NEW_TOOL_ALIASES) | {"git_status", "git_diff", "run_tests"}

# git-status --porcelain XY codes whose working tree is UNMERGED (conflict):
# completing on top of these is judged downstream, never bounced (see
# worktree_dirty_with_output).
_UNMERGED_XY_CODES = {"UU", "AA", "DD", "AU", "UA", "DU", "UD"}

# run_command denylist — word-boundary tokens plus explicit network verbs.
_DENY_TOKENS = re.compile(r"\b(curl|wget|nc|ncat|netcat|ssh|scp|sftp|sudo|podman|docker|mount)\b")
_TEST_SIGNAL = re.compile(r"(tests?/|test_|_test|conftest|pytest)", re.IGNORECASE)
# Bare test-directory segments that count as a test signal even without a
# trailing slash or dotted filename (impl-plan §6.6).
_TEST_DIR_SEGMENTS = frozenset({"tests", "test"})

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read a file from the worktree (optionally a line range)."
                " For large files, call view_symbol_outline first, then read"
                " targeted ranges via line_start/line_end."
                " Output is clipped to the configured line budget — you will"
                " see a '[truncated: N more lines]' marker instead of the rest."
            ),
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
            "description": "Find files matching a glob pattern across the worktree.",
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
            "description": "Regex search across the worktree.",
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
            "description": (
                "List classes/functions with line numbers for a source file."
                " Full support for Python (precise AST outlines); other"
                " languages fall back to a line-based declaration grep."
            ),
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
            "description": (
                "Run a shell command inside the sandbox (no network)."
                " Denied commands: curl, wget, nc, ssh, git push, sudo,"
                " podman, docker, mount."
            ),
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
            "name": "edit_file",
            "description": (
                "Replace a contiguous block of lines in a file."
                " Use view_symbol_outline + read_file first to identify exact"
                " line numbers. start_line and end_line are 1-indexed and"
                " inclusive. The replacement text replaces those lines exactly"
                " — do not include surrounding context lines."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {
                        "type": "integer",
                        "description": "First line to replace (1-indexed, inclusive)",
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "Last line to replace (1-indexed, inclusive)",
                    },
                    "replacement": {
                        "type": "string",
                        "description": "New content for lines start_line..end_line",
                    },
                },
                "required": ["path", "start_line", "end_line", "replacement"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": (
                "List entries in a directory. Returns name, type (file/dir),"
                " and size for each entry. Depth controls recursion"
                " (1 = direct children only). Use this instead of 'ls' or"
                " 'find' when surveying a new area."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "default": "."},
                    "depth": {
                        "type": "integer",
                        "default": 1,
                        "description": "Max recursion depth (1-3)",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_status",
            "description": "Show the worktree status (git status --short). Read-only.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_diff",
            "description": "Show unstaged (or staged) changes for the worktree. Read-only.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Restrict diff to this file (optional)",
                    },
                    "staged": {"type": "boolean", "default": False},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_tests",
            "description": (
                "Run the project test suite (or a subset) and return a"
                " structured summary. Prefer this over 'run_command' with"
                " pytest — it returns structured results and clips verbose"
                " passing output automatically."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Test files or directories to run (empty = full suite)",
                    },
                    "keyword": {"type": "string", "description": "pytest -k filter expression"},
                    "timeout_s": {"type": "number", "default": 120},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_symbols",
            "description": (
                "Find symbol definitions or usages across the codebase."
                " kind='definition' finds where a symbol is defined;"
                " kind='usage' finds all call sites;"
                " kind='export' lists what a module exports."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Symbol name (class, function, variable)",
                    },
                    "kind": {
                        "type": "string",
                        "enum": ["definition", "usage", "export"],
                        "default": "definition",
                    },
                    "path": {"type": "string", "description": "Restrict search to this path"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mark_task_complete",
            "description": (
                "The ONLY way to finish your attempt. The worktree must be"
                " CLEAN first: commit with `git add -A && git commit -m"
                " \"<message>\"` BEFORE calling this — a dirty tree bounces"
                " the call and wastes a turn. If the turn budget runs out"
                " before you call this, the attempt is destroyed and all"
                " uncommitted work is lost. Provide a short summary of what"
                " changed; set no_changes=true only when you genuinely made"
                " no changes and none were needed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "no_changes": {
                        "type": "boolean",
                        "description": (
                            "Set true only when you genuinely made no changes"
                            " and none were needed."
                        ),
                        "default": False,
                    },
                },
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


def _normalized_tokens(cmd: str) -> list[str]:
    """shlex tokens with quote/backslash artifacts stripped (impl-plan §6.6).

    Defeats lexical evasions like ``c'u'r'l`` or ``cur\\l``: shlex already
    removes shell quotes, and remaining backslashes are dropped before the
    denylist regex runs over the re-joined token sequence. Falls back to a
    raw whitespace split when the shell string is unparsable.
    """
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        tokens = cmd.split()
    # Strip quotes per-character (not just at the edges) so concatenations
    # like ``c'u'r'l`` collapse to ``curl`` even when shlex bails.
    return [t.replace("\\", "").replace("'", "").replace('"', "") for t in tokens]


def _is_test_signal_path(token: str) -> bool:
    """A path-shaped token pointing at a test file/dir (impl-plan §6.6)."""
    if _TEST_SIGNAL.search(token):
        return True
    segments = [s for s in token.replace("\\", "/").split("/") if s]
    return bool(segments) and segments[0] in _TEST_DIR_SEGMENTS


def screen_command(cmd: str) -> None:
    """Raise :class:`CommandDenied` if the command string is denylisted."""
    normalized = " ".join(_normalized_tokens(cmd))
    for candidate in (cmd, normalized):
        if _DENY_TOKENS.search(candidate):
            raise CommandDenied(f"denied: network/escalation command: {cmd[:120]}")
    tokens = normalized.split()
    if "git" in tokens and "push" in tokens[tokens.index("git") + 1 :]:
        raise CommandDenied(f"denied: git push is orchestrator-only: {cmd[:120]}")
    if "git push" in cmd:  # keep the literal check as a cheap backstop
        raise CommandDenied(f"denied: git push is orchestrator-only: {cmd[:120]}")
    if "chmod" in tokens:
        for tok in tokens:
            if tok != "chmod" and not tok.startswith("-") and _is_test_signal_path(tok):
                raise CommandDenied(f"denied: chmod targeting a test-signal path: {cmd[:120]}")


def _gate_new_tool(name: str, args: dict[str, Any], scopes: TaskScopes) -> Verdict:
    """Scope verdict for the WP 7.x tools, expressed via existing gate shapes.

    ``check_tool_call`` returns ``ALLOW`` for tools it does not know; these
    tools re-enter the same decision point under their policy-equivalent
    tool/args so write-policy, protected globs, strict read scope and the
    run_command path screening all apply unchanged.
    """
    if name in _NEW_TOOL_ALIASES:
        return check_tool_call(_NEW_TOOL_ALIASES[name], {"path": str(args.get("path", ""))}, scopes)
    if name in ("git_status", "git_diff"):
        # Read-only over the git object store — always in-scope, but logged.
        return Verdict.ALLOW_LOGGED
    if name == "run_tests":
        # Same policy as run_command on the equivalent pytest invocation.
        paths = " ".join(shlex.quote(str(p)) for p in (args.get("paths") or []))
        cmd = "pytest " + paths
        keyword = str(args.get("keyword") or "")
        if keyword:
            cmd += " -k " + shlex.quote(keyword)
        return check_tool_call("run_command", {"cmd": cmd}, scopes)
    return Verdict.ALLOW


def _summarize_junit(xml_text: str) -> str | None:
    """JUnit XML -> stable one-line summary plus a full failure list.

    Stable format (WS-02 parses the first line)::

        PASSED: 42  FAILED: 2  ERROR: 0  [SKIPPED: 1]

        FAILURES:
          tests/test_api.py::test_rate_limit
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
            failures.append(r.test_id)
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
        lines.extend(f"  {test_id}" for test_id in failures)
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
            line
            for line in out.splitlines()
            if line.strip() and line[:2] not in _UNMERGED_XY_CODES
        ]
        return bool(committable), out

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
        cmd = ["pytest", "--tb=short", "--no-header", "-q", f"--junitxml={_RUN_TESTS_JUNIT}"]
        cmd.extend(paths)
        keyword = str(args.get("keyword") or "")
        if keyword:
            cmd.extend(["-k", keyword])
        ok, raw = await self._exec(cmd, timeout_s=timeout)
        # The JUnit report lives inside the container; fetch it, then parse.
        xml_ok, xml_text = await self._exec(["cat", _RUN_TESTS_JUNIT])
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
