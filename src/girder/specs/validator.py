"""OpenSpec validation (impl-plan §6.9, R7).

In-process structural validation is always authoritative for parsing; the
optional ``openspec`` CLI shell-out (``Settings.specs.cli``) runs *after* it
and, when enabled and the binary is present, is authoritative for the final
verdict:

* ``cli=False`` (default) — in-process validation only, exactly as before.
* ``cli=True``, binary present, exit 0 — accepted.
* ``cli=True``, binary present, non-zero exit / timeout — rejected; CLI stderr
  is aggregated into the :class:`SpecValidationError` error list.
* ``cli=True``, binary absent — warning logged, in-process verdict stands
  ("optional shell-out *when present*").
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from girder.db.models import TaskType
from girder.guard.scope import glob_to_regex

log = logging.getLogger(__name__)

SPEC_SCHEMA = "girder.openspec/v1"

#: Wall-clock cap for the optional `openspec` CLI shell-out (§6.9).
CLI_TIMEOUT_S = 10.0

_TASK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# Canonical document template — the single source of truth the generator
# prompt embeds (impl-plan §6.9). Must itself pass parse_spec (unit-tested
# against template rot).
OPENSPEC_TEMPLATE = """\
---
# OpenSpec proposal — fill in every field; the '---' delimiter lines must stay
# exactly where they are. Field notes:
#   schema:           fixed value, do not change.
#   title:            one-line human summary of the change.
#   intent:           why the change exists; the outcome users observe.
#   tasks:            1..N tasks, each a scheduled unit of work.
#     id:             kebab-case, unique (^[a-z0-9][a-z0-9-]*$).
#     title:          imperative one-liner.
#     type:           code_change | test_change | fix | refactor | documentation.
#     scope_globs:    non-empty list of repo-relative write globs.
#     success_criteria: non-empty list of checkable statements.
#     depends_on:     optional list of other task ids here (must stay acyclic).
schema: girder.openspec/v1
title: Add user authentication
intent: Users must be able to log in with a token.
tasks:
  - id: auth-endpoint
    title: Implement token endpoint
    type: code_change
    scope_globs: ["src/auth/**"]
    success_criteria:
      - "POST /token returns a JWT for valid credentials"
      - "expired tokens are rejected with 401"
    depends_on: []
---
Optional human-readable narrative goes here (may be empty).
"""


class SpecValidationError(ValueError):
    """Aggregated structural errors — .errors is a list[str], one per finding."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("invalid openspec: " + "; ".join(errors))


@dataclass(slots=True)
class TaskSpec:
    id: str
    title: str
    type: TaskType
    scope_globs: list[str]
    success_criteria: list[str]
    depends_on: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SpecDocument:
    title: str
    intent: str
    tasks: list[TaskSpec]
    body_md: str
    raw: str


def _split_frontmatter(text: str, errors: list[str]) -> dict[str, object] | None:
    """Return the frontmatter dict, or None after recording violations."""
    lines = text.split("\n")
    if not lines or lines[0] != "---":
        errors.append("document must start with a '---' frontmatter delimiter line")
        return None
    try:
        close = lines.index("---", 1)
    except ValueError:
        errors.append("frontmatter has no closing '---' delimiter line")
        return None
    try:
        data = yaml.safe_load("\n".join(lines[1:close]))
    except yaml.YAMLError as exc:
        errors.append(f"frontmatter is not valid YAML: {exc}")
        return None
    if not isinstance(data, dict):
        errors.append("frontmatter must be a YAML mapping")
        return None
    return data


def _check_glob(pattern: object, task_id: str, errors: list[str]) -> None:
    glob = str(pattern)
    if glob.startswith("/"):
        errors.append(
            f"task {task_id!r}: scope glob {glob!r} must be repo-relative (no leading '/')"
        )
    if ".." in glob.split("/"):
        errors.append(f"task {task_id!r}: scope glob {glob!r} must not contain '..' path segments")
    try:
        glob_to_regex(glob)
    except re.error as exc:  # defensive: glob_to_regex currently escapes everything
        errors.append(f"task {task_id!r}: scope glob {glob!r} is not compilable: {exc}")


def _detect_cycle(dep: dict[str, list[str]], errors: list[str]) -> None:
    white, gray, black = 0, 1, 2
    color = dict.fromkeys(dep, white)
    stack: list[str] = []

    def visit(node: str) -> bool:
        color[node] = gray
        stack.append(node)
        for nxt in dep[node]:
            if color[nxt] == gray:
                cycle = [*stack[stack.index(nxt):], nxt]
                errors.append("depends_on graph has a cycle: " + " -> ".join(cycle))
                return True
            if color[nxt] == white and visit(nxt):
                return True
        stack.pop()
        color[node] = black
        return False

    for node in dep:
        if color[node] == white and visit(node):
            return


async def _run_openspec_cli(cli_bin: str, text: str) -> tuple[int | None, list[str]]:
    """Run ``<cli_bin> validate --json <file>`` on the serialized proposal.

    Returns ``(returncode, stderr_lines)``; ``returncode is None`` means the
    binary was not found on PATH. A timeout is treated as a non-zero exit.
    """
    fd = tempfile.NamedTemporaryFile("w", suffix=".md", delete=False)
    try:
        fd.write(text)
        fd.close()
        proc = await asyncio.create_subprocess_exec(
            cli_bin,
            "validate",
            "--json",
            fd.name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), CLI_TIMEOUT_S)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return 1, [f"openspec CLI timed out after {CLI_TIMEOUT_S:g}s"]
        lines = [ln for ln in stderr.decode(errors="replace").splitlines() if ln.strip()]
        return proc.returncode, lines
    except FileNotFoundError:
        return None, []
    finally:
        await asyncio.to_thread(Path(fd.name).unlink, missing_ok=True)


def _cli_validate(cli_bin: str, text: str) -> tuple[int | None, list[str]]:
    """Sync bridge for :func:`_run_openspec_cli` (safe inside a running loop)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_run_openspec_cli(cli_bin, text))
    # Already inside an event loop (e.g. FastAPI route): the CLI call is
    # blocking-by-contract, so run it in a worker thread with its own loop.
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, _run_openspec_cli(cli_bin, text)).result()


def parse_spec(
    text: str,
    *,
    cli: bool = False,
    cli_bin: str = "openspec",
) -> SpecDocument:
    """Parse + validate. Collects ALL violations into one SpecValidationError
    (fail-with-everything, not fail-first, so the review UI can show them all).

    With ``cli=True`` (``Settings.specs.cli``), the in-process check runs first
    (cheap, no subprocess); if it passes, the ``openspec`` CLI is invoked and
    its verdict wins — see the module docstring for the semantics.
    """
    errors: list[str] = []
    data = _split_frontmatter(text, errors)
    if data is None:
        raise SpecValidationError(errors)

    if data.get("schema") != SPEC_SCHEMA:
        errors.append(f"frontmatter 'schema' must be {SPEC_SCHEMA!r}")

    title = data.get("title")
    if not isinstance(title, str) or not title.strip():
        errors.append("frontmatter 'title' must be a non-empty string")

    intent = data.get("intent")
    if not isinstance(intent, str) or not intent.strip():
        errors.append("frontmatter 'intent' must be a non-empty string")

    tasks_raw = data.get("tasks")
    if not isinstance(tasks_raw, list) or not tasks_raw:
        errors.append("frontmatter 'tasks' must be a non-empty list")
        tasks_raw = []

    tasks: list[TaskSpec] = []
    seen_ids: set[str] = set()
    dep_graph: dict[str, list[str]] = {}
    for i, raw in enumerate(tasks_raw):
        label = f"tasks[{i}]"
        if not isinstance(raw, dict):
            errors.append(f"{label}: each task must be a mapping")
            continue
        tid = raw.get("id")
        task_id = tid if isinstance(tid, str) else ""
        if not isinstance(tid, str) or not _TASK_ID_RE.match(tid):
            errors.append(
                f"{label}: 'id' {tid!r} must match ^[a-z0-9][a-z0-9-]*$"
            )
        elif tid in seen_ids:
            errors.append(f"{label}: duplicate task id {tid!r}")
        else:
            seen_ids.add(tid)
        label = f"task {tid!r}" if isinstance(tid, str) and tid else label

        t_title = raw.get("title")
        if not isinstance(t_title, str) or not t_title.strip():
            errors.append(f"{label}: 'title' must be a non-empty string")

        t_type = raw.get("type")
        try:
            task_type = TaskType(t_type) if isinstance(t_type, str) else None
        except ValueError:
            task_type = None
        if task_type is None:
            allowed = ", ".join(t.value for t in TaskType)
            errors.append(f"{label}: 'type' {t_type!r} must be one of: {allowed}")

        globs = raw.get("scope_globs")
        if not isinstance(globs, list) or not globs or not all(
            isinstance(g, str) and g.strip() for g in globs
        ):
            errors.append(f"{label}: 'scope_globs' must be a non-empty list of non-empty strings")
            globs = []
        else:
            for g in globs:
                _check_glob(g, str(tid), errors)

        criteria = raw.get("success_criteria")
        if not isinstance(criteria, list) or not criteria or not all(
            isinstance(c, str) and c.strip() for c in criteria
        ):
            errors.append(
                f"{label}: 'success_criteria' must be a non-empty list of non-empty strings"
            )

        deps = raw.get("depends_on", [])
        if deps is None:
            deps = []
        if not isinstance(deps, list) or not all(isinstance(d, str) and d for d in deps):
            errors.append(f"{label}: 'depends_on' must be a list of task ids")
            deps = []

        tasks.append(
            TaskSpec(
                id=task_id,
                title=t_title if isinstance(t_title, str) else "",
                type=task_type or TaskType.CODE_CHANGE,
                scope_globs=list(globs) if isinstance(globs, list) else [],
                success_criteria=list(criteria) if isinstance(criteria, list) else [],
                depends_on=[d for d in deps if isinstance(d, str)],
            )
        )
        dep_graph[task_id or label] = [d for d in deps if isinstance(d, str)]

    known = {t.id for t in tasks if t.id}
    for tid, deps in dep_graph.items():
        for d in deps:
            if d not in known:
                errors.append(f"task {tid!r}: depends_on references unknown task id {d!r}")
    _detect_cycle({k: [d for d in v if d in dep_graph] for k, v in dep_graph.items()}, errors)

    if errors:
        raise SpecValidationError(errors)

    if cli:
        returncode, cli_errors = _cli_validate(cli_bin, text)
        if returncode is None:
            log.warning(
                "openspec CLI %r not found on PATH; falling back to in-process "
                "validation only (set specs.cli = false to silence)",
                cli_bin,
            )
        elif returncode != 0:
            raise SpecValidationError(
                cli_errors or [f"openspec CLI exited {returncode} without diagnostics"]
            )

    close = text.split("\n").index("---", 1)
    body = "\n".join(text.split("\n")[close + 1 :]).lstrip("\n")
    return SpecDocument(
        title=str(title),
        intent=str(intent),
        tasks=tasks,
        body_md=body,
        raw=text,
    )
