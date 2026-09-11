"""Scope gating and command screening for the tool registry (impl-plan §6.10).

Split out of the former monolithic ``agent/tools.py`` (origin SHA a698868).
``run_command`` gets its own screening layer on top of the scope gate: the
command string is matched against a network/escalation denylist before it is
handed to ``sh -c`` inside the (network=none) container. The scope gate itself
also applies the §6.6 R2 write policy to the command's path arguments — see
``guard.scope.run_command_path_args``.
"""

from __future__ import annotations

import re
import shlex
from typing import Any

from girder.guard.scope import TaskScopes, Verdict, check_tool_call

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

# run_command denylist — word-boundary tokens plus explicit network verbs.
_DENY_TOKENS = re.compile(r"\b(curl|wget|nc|ncat|netcat|ssh|scp|sftp|sudo|podman|docker|mount)\b")
_TEST_SIGNAL = re.compile(r"(tests?/|test_|_test|conftest|pytest)", re.IGNORECASE)
# Bare test-directory segments that count as a test signal even without a
# trailing slash or dotted filename (impl-plan §6.6).
_TEST_DIR_SEGMENTS = frozenset({"tests", "test"})


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
        return check_tool_call("run_command", {"cmd": _run_tests_command(args)}, scopes)
    return Verdict.ALLOW


def _run_tests_command(args: dict[str, Any]) -> str:
    """The run_command-equivalent pytest invocation (gate + denylist input)."""
    paths = " ".join(shlex.quote(str(p)) for p in (args.get("paths") or []))
    cmd = "pytest " + paths
    keyword = str(args.get("keyword") or "")
    if keyword:
        cmd += " -k " + shlex.quote(keyword)
    return cmd



__all__ = [
    "CommandDenied",
    "screen_command",
    "_GATED_NEW_TOOLS",
    "_NEW_TOOL_ALIASES",
    "_READ_TOOLS",
    "_TERMINAL_TOOLS",
    "_WRITE_TOOLS",
    "_gate_new_tool",
    "_run_tests_command",
]
