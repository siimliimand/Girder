"""In-process OpenSpec structural validation (impl-plan §6.9, R7).

Malformed proposals are rejected here, before the user ever reviews them
(plan.md Phase 1 task 2). The optional ``openspec`` CLI shell-out lives in
Settings.specs and is a future concern; this module is authoritative.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import yaml

from girder.db.models import TaskType
from girder.guard.scope import glob_to_regex

SPEC_SCHEMA = "girder.openspec/v1"

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


def parse_spec(text: str) -> SpecDocument:
    """Parse + validate. Collects ALL violations into one SpecValidationError
    (fail-with-everything, not fail-first, so the review UI can show them all).
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

    close = text.split("\n").index("---", 1)
    body = "\n".join(text.split("\n")[close + 1 :]).lstrip("\n")
    return SpecDocument(
        title=str(title),
        intent=str(intent),
        tasks=tasks,
        body_md=body,
        raw=text,
    )
