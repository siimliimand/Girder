"""Scope enforcement for agent tool calls (D11, §8.5 / impl-plan §6.6, R2).

Verdict policy (resolved ambiguity R2 of the implementation plan):

* **Writes** (``write_file``, ``apply_patch``) must fall inside the task's
  declared ``scope_globs`` — otherwise ``VIOLATION`` (intercepted, never
  executed, logged as an integrity-relevant event). For ``apply_patch`` the
  declared ``path`` argument alone is not enough: every ``+++``/``---``
  target in the unified diff body is a write path and is checked with the
  same policy BEFORE execution (§8.5: every write path is checked before
  execution). Any violating target holds the whole call.
* **Reads** (``read_file``, ``ripgrep``, ``find_files``,
  ``view_symbol_outline``) are ``ALLOW_LOGGED`` anywhere inside the worktree —
  dynamic discovery (D6) requires it — *except* protected paths
  (``.github/**``, ``openspec/**``, credential-shaped files, …), which are
  always ``VIOLATION``: this is the primary defense against prompt injection
  steering the agent toward CI configuration or credentials.
* ``strict_read_scope = true`` restores the literal §8.5 behavior: any read
  outside ``scope_globs`` is a ``VIOLATION`` too.
* ``run_command`` gets the same write policy on "any argument that resolves
  to a path" (§6.6 R2): shell redirect operands (``>`` ``>>`` ``2>`` ``&>``)
  and ``tee`` operands are write targets — out-of-scope or protected targets
  hold the whole call. Any *other* token with path syntax that lexically
  resolves under the worktree root is treated as a read (protected paths
  still violation; ordinary out-of-scope reads stay allowed — R2). Tokens that
  do not look like paths (commands, flags, pipes, heredoc text) are ignored:
  this stays a path-argument policy, and `python -c "open(...)"`-style
  indirection remains the Layer 2/3 audits' backstop.
* Path escapes (``..`` out of the worktree, absolute paths outside the root)
  are always ``VIOLATION``.
"""

from __future__ import annotations

import re
import shlex
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
# Tools with no path argument at all.
_NO_PATH_TOOLS = {"mark_task_complete", "request_spec_amendment"}

# run_command: redirect operators whose operand is a WRITE target, and those
# whose operand is a read/here-doc delimiter (not a write).
_WRITE_REDIRECTS = {">", ">>", "2>", "&>", "1>", "2>>", "1>>"}
_READ_REDIRECTS = {"<", "<<", "<<<"}
# Attached forms (">out", "2>err") — longest first so "2>>" wins over "2>".
_REDIRECT_PREFIXES = ("2>>", "1>>", "&>", ">>", "2>", "1>", ">")
_FD_DUP = re.compile(r"^&\d+$")
# Tokens that end an operand run for `tee` (and other multi-operand scanning).
_SHELL_BREAKS = {"|", "||", "&&", ";", "&", "(", ")", "<", "<<", "<<<",
                 ">", ">>", "2>", "&>", "1>", "2>>", "1>>"}


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


def run_command_path_args(cmd: str) -> tuple[list[str], list[str]]:
    """Split a run_command shell string into (write targets, other path tokens).

    Per impl-plan §6.6 R2 ("any run_command argument that resolves to a path"):

    * write targets — redirect operands (``>``/``>>``/``2>``/``&>``, attached
      or separate, fd-duplicates like ``2>&1`` excluded) and every non-flag
      operand of ``tee`` up to the next shell break;
    * other path tokens — any remaining token with path syntax (a ``/``
      separator or a dotted segment, excluding flags). Resolution/existence is
      decided by the caller; this function is purely lexical.

    Unparsable shell (unbalanced quotes) yields no paths — the registry
    denylist and the Layer 2/3 audits remain the backstop.
    """
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        return [], []
    writes: list[str] = []
    consumed: set[int] = set()
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in _WRITE_REDIRECTS and i + 1 < len(tokens):
            if not _FD_DUP.match(tokens[i + 1]):
                writes.append(tokens[i + 1])
                consumed.add(i + 1)
            i += 2
            continue
        if tok in _READ_REDIRECTS and i + 1 < len(tokens):
            consumed.add(i + 1)
            i += 2
            continue
        attached = _attached_redirect_target(tok)
        if attached is not None:
            writes.append(attached)
            consumed.add(i)
            i += 1
            continue
        if tok == "tee":
            j = i + 1
            while j < len(tokens) and tokens[j] not in _SHELL_BREAKS:
                if not tokens[j].startswith("-"):
                    writes.append(tokens[j])
                    consumed.add(j)
                j += 1
            i = j
            continue
        i += 1
    others = [
        t for k, t in enumerate(tokens) if k not in consumed and _looks_like_path(t)
    ]
    return writes, others


def _attached_redirect_target(tok: str) -> str | None:
    """``">out"`` → ``"out"``; fd-dups (``2>&1``) and non-redirects → None."""
    for prefix in _REDIRECT_PREFIXES:
        if tok.startswith(prefix) and len(tok) > len(prefix):
            target = tok[len(prefix):]
            if not _FD_DUP.match(target):
                return target
            return None
    return None


def _looks_like_path(tok: str) -> bool:
    """Path syntax heuristic: flags aren't paths; ``/`` or a dotted segment is."""
    return bool(tok) and not tok.startswith("-") and ("/" in tok or "." in tok)


def _check_run_command(cmd: str, scopes: TaskScopes) -> Verdict:
    """Write-policy on path arguments (§6.6 R2); reads are not write-gated."""
    writes, others = run_command_path_args(cmd)
    for raw in writes:
        rel = resolve_path(raw, scopes.root)
        if rel is None:
            return Verdict.VIOLATION  # escape attempt via redirect
        if _matches_any(rel, scopes.protected_globs):
            return Verdict.VIOLATION
        if not _matches_any(rel, scopes.write_globs):
            return Verdict.VIOLATION
    # §6.6 R2: EVERY read-shaped path token must be evaluated — returning on
    # the first token would let a later protected/escaping path smuggle
    # through ("cat src/a.py .github/workflows/ci.yml"). Track the worst
    # verdict (VIOLATION > ALLOW_LOGGED > ALLOW) and hold the whole call on
    # any violating token, mirroring apply_patch's diff-body policy.
    worst = Verdict.ALLOW
    for raw in others:
        rel = resolve_path(raw, scopes.root)
        if rel is None:
            return Verdict.VIOLATION  # path escape, even read-shaped
        if _matches_any(rel, scopes.protected_globs):
            return Verdict.VIOLATION
        # The token already resolved inside the task root (resolve_path
        # returns None for escapes), so this is purely a path-shape
        # classification: in-root path-shaped tokens behave like read_file
        # (R2: reads are ALLOW_LOGGED). Protected paths and escapes were
        # handled above. Existence is deliberately not probed on the host
        # filesystem — run_command executes in the guest, where the host
        # layout is meaningless (plan §8.5 / impl-plan §6.6 R2).
        if scopes.strict_read_scope:
            # strict_read_scope (§8.5 literal): mirror the read-tool branch —
            # in-scope reads are ALLOW, out-of-scope reads are VIOLATIONS.
            if not _matches_any(rel, scopes.write_globs):
                return Verdict.VIOLATION
        elif worst is Verdict.ALLOW:
            worst = Verdict.ALLOW_LOGGED
    return worst


def diff_target_paths(diff: str) -> list[str]:
    """Extract write targets from a unified diff body (§8.5/D11).

    Parses ``+++``/``---`` lines: ``/dev/null`` (pure create/delete) is
    skipped, ``a/``/``b/`` prefixes are stripped, a trailing tab-separated
    timestamp is dropped, and quotes (``core.quotePath``-style) are removed.
    A diff with no parseable targets yields an empty list — the caller then
    falls back to the declared-path policy alone.
    """
    targets: list[str] = []
    for line in diff.splitlines():
        if not line.startswith(("+++ ", "--- ")):
            continue
        path = line[4:].split("\t", 1)[0].strip().strip('"')
        if not path or path == "/dev/null":
            continue
        if path.startswith(("a/", "b/")):
            path = path[2:]
        targets.append(path)
    return targets


def check_tool_call(tool: str, args: dict[str, object], scopes: TaskScopes) -> Verdict:
    """The single scope decision point run before any tool executes."""
    arg_name = _WRITE_TOOLS.get(tool) or _READ_TOOLS.get(tool)
    if tool in _NO_PATH_TOOLS:
        return Verdict.ALLOW
    if tool == "run_command":
        return _check_run_command(str(args.get("cmd", "")), scopes)
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
        if not _matches_any(rel, scopes.write_globs):
            return Verdict.VIOLATION
        if tool == "apply_patch":
            # The declared path is not the only write target: every file the
            # diff body touches is written when the patch applies. One
            # violating target holds the whole call (§8.5).
            for raw_target in diff_target_paths(str(args.get("unified_diff", ""))):
                trel = resolve_path(raw_target, scopes.root)
                if trel is None:
                    return Verdict.VIOLATION  # escape attempt inside the diff
                if _matches_any(trel, scopes.protected_globs):
                    return Verdict.VIOLATION
                if not _matches_any(trel, scopes.write_globs):
                    return Verdict.VIOLATION
        return Verdict.ALLOW

    # read tools
    if scopes.strict_read_scope:
        return Verdict.ALLOW if _matches_any(rel, scopes.write_globs) else Verdict.VIOLATION
    return Verdict.ALLOW_LOGGED


def _matches_any(rel: str, patterns: list[str]) -> bool:
    return any(path_matches(rel, p) for p in patterns)
