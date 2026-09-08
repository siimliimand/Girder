"""Scope enforcement for agent tool calls (D11, §8.5 / impl-plan §6.6, R2).

Verdict policy (resolved ambiguity R2 of the implementation plan):

* **Writes** (``write_file``, ``apply_patch``) must fall inside the task's
  declared ``scope_globs`` — otherwise ``VIOLATION`` (intercepted, never
  executed, logged as an integrity-relevant event).
* **Reads** (``read_file``, ``ripgrep``, ``find_files``,
  ``view_symbol_outline``) are ``ALLOW_LOGGED`` anywhere inside the worktree —
  dynamic discovery (D6) requires it — *except* protected paths
  (``.github/**``, ``openspec/**``, credential-shaped files, …), which are
  always ``VIOLATION``: this is the primary defense against prompt injection
  steering the agent toward CI configuration or credentials.
* ``strict_read_scope = true`` restores the literal §8.5 behavior: any read
  outside ``scope_globs`` is a ``VIOLATION`` too.
* Path escapes (``..`` out of the worktree, absolute paths outside the root)
  are always ``VIOLATION``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import PurePosixPath

_MAGIC = re.compile(r"[*?\[]")


class Verdict(Enum):
    ALLOW = "allow"  # in-scope, execute
    ALLOW_LOGGED = "allow_logged"  # execute, but record the access
    VIOLATION = "violation"  # intercept; never execute


# Tools whose path argument is treated as a write target.
_WRITE_TOOLS = {"write_file": "path", "apply_patch": "path"}
# Tools whose path argument is read-only discovery.
_READ_TOOLS = {
    "read_file": "path",
    "view_symbol_outline": "path",
    "ripgrep": "path",
    "find_files": "glob",
}
# Tools with no path argument at all (run_command's command screening is a
# Sprint 3 concern — the tool registry — not a scope question).
_NO_PATH_TOOLS = {"mark_task_complete", "request_spec_amendment", "run_command"}


@dataclass
class TaskScopes:
    write_globs: list[str] = field(default_factory=list)
    protected_globs: list[str] = field(default_factory=list)
    strict_read_scope: bool = False
    root: str = "/workspace"


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a git-style glob (with ``**``) to an anchored regex.

    ``**`` crosses directory separators, ``*``/``?`` stay within one segment.
    Character classes are not part of the scope-glob vocabulary and are
    matched literally.
    """
    i, out = 0, "^"
    while i < len(pattern):
        c = pattern[i]
        if c == "*":
            if pattern[i : i + 3] == "**/":
                out += "(?:.*/)?"  # **/ matches zero or more directories
                i += 3
                continue
            if pattern[i : i + 2] == "**":
                out += ".*"
                i += 2
                continue
            out += "[^/]*"
        elif c == "?":
            out += "[^/]"
        else:
            out += re.escape(c)
        i += 1
    return re.compile(out + "$")


def path_matches(path: str, pattern: str) -> bool:
    """Match a POSIX-relative *path* against a glob *pattern*.

    A magic-free pattern (``src/app``) matches itself and everything beneath
    it; ``tests/**`` matches ``tests`` and everything under ``tests``.
    """
    path = path.strip("./")
    pattern = pattern.strip("./")
    if not _MAGIC.search(pattern):
        return path == pattern or path.startswith(pattern.rstrip("/") + "/")
    regex = glob_to_regex(pattern)
    if regex.match(path):
        return True
    # "tests/**" should also match the bare directory "tests"
    if pattern.endswith("/**"):
        return path == pattern[:-3]
    return False


def resolve_path(raw: str, root: str = "/workspace") -> str | None:
    """Normalize a tool-call path to worktree-relative POSIX form.

    Returns ``None`` when the path escapes the worktree root (``..`` climb or
    absolute path outside root) — callers treat that as a violation.
    """
    p = PurePosixPath(raw.strip())
    if p.is_absolute():
        try:
            p = p.relative_to(root.rstrip("/") or "/")
        except ValueError:
            return None
    parts: list[str] = []
    for segment in p.parts:
        if segment == "." or segment == "":
            continue
        if segment == "..":
            if not parts:
                return None  # climbed above the root
            parts.pop()
        else:
            parts.append(segment)
    return "/".join(parts)


def check_tool_call(tool: str, args: dict[str, object], scopes: TaskScopes) -> Verdict:
    """The single scope decision point run before any tool executes."""
    arg_name = _WRITE_TOOLS.get(tool) or _READ_TOOLS.get(tool)
    if tool in _NO_PATH_TOOLS:
        return Verdict.ALLOW
    if arg_name is None:
        return Verdict.ALLOW  # unknown tool: not a scope question (registry rejects later)

    raw = str(args.get(arg_name, "")).strip()
    if not raw:
        # No path argument: a read tool with an empty path is a repo-wide scan
        # (allowed, logged — results still framed as untrusted data); a write
        # with no target is malformed and treated as a violation.
        if tool in _WRITE_TOOLS:
            return Verdict.VIOLATION
        if tool in _READ_TOOLS:
            return Verdict.ALLOW_LOGGED
        return Verdict.ALLOW

    rel = resolve_path(raw, scopes.root)
    if rel is None:
        return Verdict.VIOLATION  # escape attempt

    if _matches_any(rel, scopes.protected_globs):
        return Verdict.VIOLATION

    if tool in _WRITE_TOOLS:
        return Verdict.ALLOW if _matches_any(rel, scopes.write_globs) else Verdict.VIOLATION

    # read tools
    if scopes.strict_read_scope:
        return Verdict.ALLOW if _matches_any(rel, scopes.write_globs) else Verdict.VIOLATION
    return Verdict.ALLOW_LOGGED


def _matches_any(rel: str, patterns: list[str]) -> bool:
    return any(path_matches(rel, p) for p in patterns)
