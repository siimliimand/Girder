"""Decomposer: frozen spec -> ordered wave-0 tasks (impl-plan §11 Sprint 3 WP 3.4).

Runs exactly once per run, right after the spec freezes. Validation happened in
:mod:`girder.specs.validator`; this module is the last line of defense before
persistence and then materializes one :class:`~girder.db.models.Task` per
``TaskSpec``, in declared order, each carrying the slice of the frozen spec that
will later be mounted into its agent's prompt.
"""

from __future__ import annotations

import re

from girder.db import repo
from girder.db.engine import Database
from girder.db.models import Run, Task, TaskType
from girder.specs.validator import SpecDocument, TaskSpec

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


class DecompositionError(ValueError):
    """Raised when a frozen spec cannot be decomposed into tasks."""


def _synthesized_slice(task: TaskSpec) -> str:
    """Deterministic fallback slice when no markdown section names the task."""
    criteria = "\n".join(f"- {c}" for c in task.success_criteria)
    globs = ", ".join(f"`{g}`" for g in task.scope_globs)
    return (
        f"## Task: {task.id}\n\n"
        f"{task.title}\n\n"
        f"- type: {task.type.value}\n"
        f"- scope_globs: {globs}\n\n"
        f"### Success criteria\n\n{criteria}\n"
    )


def _heading_level(line: str) -> int | None:
    m = _HEADING_RE.match(line)
    return len(m.group(1)) if m else None


def _extract_slice(raw: str, task: TaskSpec) -> str:
    """Extract the task's section from the full markdown document.

    Tolerant matcher: scans headings for one whose text names the task id or
    title (case-insensitive), then takes lines through the next heading of the
    same or higher level. Falls back to a synthesized slice — never empty.
    """
    lines = raw.split("\n")
    needles = [n.lower() for n in (task.id, task.title) if n]
    start = end = None
    for i, line in enumerate(lines):
        level = _heading_level(line)
        if level is None:
            continue
        text = _HEADING_RE.match(line).group(2)  # type: ignore[union-attr]
        lowered = text.lower()
        if any(n in lowered for n in needles):
            start = i
            for j in range(i + 1, len(lines)):
                nxt = _heading_level(lines[j])
                if nxt is not None and nxt <= level:
                    end = j
                    break
            break
    if start is None:
        return _synthesized_slice(task)
    section = "\n".join(lines[start:end]).strip()
    if not section:
        return _synthesized_slice(task)
    return section + "\n"


async def decompose_spec(db: Database, run: Run, spec: SpecDocument) -> list[Task]:
    """Materialize wave 0 tasks from a frozen spec. Exactly-once per run.

    Raises :class:`DecompositionError` if tasks already exist, the spec has no
    tasks, or any task arrives without scope globs (defense in depth — the
    validator should have rejected it already).
    """
    if await repo.list_tasks_for_run(db, run.id):
        raise DecompositionError(
            f"run {run.id} already has tasks decomposed; a spec is decomposed exactly once"
        )
    if not spec.tasks:
        raise DecompositionError("spec has no tasks; nothing to decompose")
    scopeless = [t.id for t in spec.tasks if not t.scope_globs]
    if scopeless:
        raise DecompositionError(
            "tasks without scope_globs cannot be decomposed: " + ", ".join(scopeless)
        )

    wave0 = await repo.get_or_create_wave0(db, run.id)
    tasks: list[Task] = []
    for seq, task_spec in enumerate(spec.tasks, start=1):
        tasks.append(
            await repo.create_task(
                db,
                wave0.id,
                seq,
                task_spec.title,
                TaskType(task_spec.type),
                scope_globs=list(task_spec.scope_globs),
                spec_slice_md=_extract_slice(spec.raw, task_spec),
                depends_on=list(task_spec.depends_on),
            )
        )

    await repo.insert_event(
        db,
        "spec_decomposed",
        {"task_count": len(tasks), "task_ids": [t.id for t in spec.tasks]},
        run_id=run.id,
    )
    return tasks
