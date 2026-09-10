"""Task scheduler: v1 sequential pick + v2 wave planning (WP 3.5, WP 5.1).

**v1 (Sprint 3)** — one task at a time, in (wave.sequence_order, task.seq)
order. Tasks already mid-flight (``running`` — an attempt is executing) or
parked (``awaiting_amendment``) are deliberately NOT re-picked: the pump is
single-threaded, so a ``running`` task seen here would mean either a leaked
state (recovery handles those at boot) or an amendment resume — the run
engine drives the resume case explicitly, not the scheduler.

**v2 (Sprint 5, impl-plan §11 WP 5.1)** — the wave DAG planner. Tier-1
proposes ``depends_on`` edges; :func:`compute_levels` layers the DAG with
Kahn-style longest-chain numbering; :func:`plan_waves` adds the *mechanical*
disjoint-scope demotion: within a wave, any pair of tasks whose expanded
scope globs intersect against the base-commit file tree forces the
LATER-SEQ task one wave later — the model's independence opinion is
irrelevant (plan.md Phase 4 task 2). :meth:`Scheduler.plan_run_waves`
materializes the plan into waves; :meth:`Scheduler.next_wave` hands the
run engine the next executable wave.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path

from girder.db import repo
from girder.db.engine import Database
from girder.db.models import TERMINAL_TASK_STATUSES, Run, Task, TaskStatus, Wave
from girder.guard.scope import path_matches
from girder.util import run_host_cmd

_PICKABLE = frozenset({TaskStatus.PENDING, TaskStatus.RETRY_SCHEDULED})

TERMINAL_WAVE_STATUSES = frozenset({"completed", "failed"})


class WavePlanError(ValueError):
    """The task DAG cannot be planned (cycle, unknown dependency, or
    demotion failed to converge)."""


# ------------------------------------------------------------------ pure planner


def compute_levels(tasks: Sequence[Task]) -> dict[str, int]:
    """Kahn layering over ``depends_on`` (task ids).

    Level of a task = length of the longest dependency chain ending at it
    (roots at 0). Raises :class:`WavePlanError` on unknown dependency ids or
    cycles.
    """
    ids = {t.id for t in tasks}
    for t in tasks:
        for dep in t.depends_on:
            if dep not in ids:
                raise WavePlanError(f"task {t.id!r} depends on unknown task {dep!r}")
    levels = {t.id: 0 for t in tasks}
    for _ in range(len(tasks) + 1):
        changed = False
        for t in tasks:
            want = max((levels[d] + 1 for d in t.depends_on), default=0)
            if want > levels[t.id]:
                levels[t.id] = want
                changed = True
        if not changed:
            return levels
    raise WavePlanError("dependency cycle detected among tasks")


def expand_globs(globs: Iterable[str], tree: Iterable[str]) -> set[str]:
    """Tree files (repo-relative) matching any glob, via
    :func:`girder.guard.scope.path_matches`."""
    files = list(tree)
    return {f for f in files if any(path_matches(f, g) for g in globs)}


def files_overlap(a: Iterable[str], b: Iterable[str]) -> bool:
    """True when the two expanded file sets intersect."""
    return not set(a).isdisjoint(b)


def plan_waves(tasks: Sequence[Task], tree_files: Iterable[str]) -> list[list[Task]]:
    """Compute wave levels with mechanical disjoint-set demotion.

    1. ``levels = compute_levels(tasks)``.
    2. Fixpoint: within each level, order tasks by ``seq``; for any pair
       (A, B) with ``A.seq < B.seq`` whose expanded file sets overlap, add
       the constraint ``level[B] >= level[A] + 1`` (the LATER task is
       demoted). Recompute all levels honoring both ``depends_on`` edges and
       accumulated constraints, repeat until no same-level overlap remains.
       A ``len(tasks) ** 2 + 1`` iteration cap guards non-convergence.
    3. Return levels as task lists sorted by ``seq``; empty levels dropped;
       every task appears exactly once.
    """
    if not tasks:
        return []
    tree = list(tree_files)
    expanded = {t.id: expand_globs(t.scope_globs, tree) for t in tasks}
    by_seq = sorted(tasks, key=lambda t: t.seq)
    levels = compute_levels(tasks)
    cap = len(tasks) ** 2 + 1

    for _ in range(cap):
        # Collect demotion constraints from same-level overlapping pairs.
        at_level: dict[int, list[Task]] = {}
        for t in by_seq:
            at_level.setdefault(levels[t.id], []).append(t)
        constraint_pairs: list[tuple[str, str]] = []
        for level_tasks in at_level.values():
            for i, a in enumerate(level_tasks):
                for b in level_tasks[i + 1 :]:  # by_seq order ⇒ a.seq < b.seq
                    if files_overlap(expanded[a.id], expanded[b.id]):
                        constraint_pairs.append((a.id, b.id))

        # Relax: deps give the base layering, constraints only push later.
        for _ in range(len(tasks) + 1):
            changed = False
            for t in by_seq:
                want = max((levels[d] + 1 for d in t.depends_on), default=levels[t.id])
                want = max(want, levels[t.id])
                if want > levels[t.id]:
                    levels[t.id] = want
                    changed = True
            for a_id, b_id in constraint_pairs:
                if levels[a_id] + 1 > levels[b_id]:
                    levels[b_id] = levels[a_id] + 1
                    changed = True
            if not changed:
                break
        else:
            raise WavePlanError("constraint relaxation failed to converge")

        # Invariant: constraints only push levels LATER, so every task must
        # still sit strictly after its own dependencies.
        for t in tasks:
            for d in t.depends_on:
                if levels[t.id] <= levels[d]:
                    raise WavePlanError(
                        f"demotion broke dependency invariant: {t.id!r} <= {d!r}"
                    )

        # Converged when no same-level pair overlaps anymore.
        if not constraint_pairs:
            break
    else:
        raise WavePlanError("demotion failed to converge")

    waves: dict[int, list[Task]] = {}
    for t in by_seq:
        waves.setdefault(levels[t.id], []).append(t)
    return [waves[level] for level in sorted(waves)]


# --------------------------------------------------------------------- Scheduler


class Scheduler:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def next_task(self, run: Run) -> Task | None:
        """First schedulable task for *run*, or ``None`` when none is pending.

        Ordering is (wave.sequence_order, task.seq) as returned by
        :func:`repo.list_tasks_for_run`.
        """
        for task in await repo.list_tasks_for_run(self.db, run.id):
            if task.status in _PICKABLE:
                return task
        return None

    async def pending_or_retry_count(self, run: Run) -> int:
        tasks = await repo.list_tasks_for_run(self.db, run.id)
        return sum(1 for t in tasks if t.status in _PICKABLE)

    async def all_terminal(self, run: Run) -> bool:
        tasks = await repo.list_tasks_for_run(self.db, run.id)
        return bool(tasks) and all(t.status in TERMINAL_TASK_STATUSES for t in tasks)

    async def all_completed(self, run: Run) -> bool:
        """Every task reached a success state. Sprint 6: operator steering
        (skip / force-pass, Phase 5) counts as success — a manually skipped
        task must not fail the run at the completion gate."""
        tasks = await repo.list_tasks_for_run(self.db, run.id)
        success = (TaskStatus.COMPLETED, TaskStatus.SKIPPED, TaskStatus.FORCE_PASSED)
        return bool(tasks) and all(t.status in success for t in tasks)

    # ----------------------------------------------------------- wave planning

    async def plan_run_waves(self, run: Run, repo_path: Path) -> list[Wave]:
        """Materialize the wave plan for a decomposed run (idempotent).

        Idempotency: if the audit event ``waves_planned`` already exists for
        this run, the existing wave rows are returned unchanged.
        """
        existing = await repo.get_latest_event(self.db, run.id, "waves_planned")
        if existing is not None:
            return await repo.list_waves_for_run(self.db, run.id)

        tasks = await repo.list_tasks_for_run(self.db, run.id)
        proc = await run_host_cmd(
            ["git", "-C", str(repo_path), "ls-tree", "-r", "--name-only", run.branch],
        )
        tree = [line for line in proc.stdout.splitlines() if line.strip()]
        levels = plan_waves(tasks, tree)
        # Audit reporting (plan.md Phase 5): a task counts as demoted when its
        # final wave is STRICTLY later than the level implied by its
        # depends_on edges alone, computed on the PRE-demotion layering
        # (compute_levels is transitive, so a task pushed later only because
        # its own dependency was demoted is reported too). Wave assignment is
        # untouched: this is reporting only.
        final_level = {t.id: i for i, level_tasks in enumerate(levels) for t in level_tasks}
        dep_levels = compute_levels(tasks)

        demoted: list[str] = []
        waves_payload: list[dict[str, object]] = []
        for i, level_tasks in enumerate(levels):
            wave = await repo.get_or_create_wave(self.db, run.id, i)
            for t in level_tasks:
                if t.wave_id != wave.id:
                    await repo.update_task_fields(self.db, t.id, wave_id=wave.id)
                if final_level[t.id] > dep_levels[t.id]:
                    demoted.append(t.id)
            waves_payload.append({"sequence_order": i, "task_ids": [t.id for t in level_tasks]})

        await repo.insert_event(
            self.db,
            "waves_planned",
            {
                "waves": waves_payload,
                "demoted_task_ids": demoted,
                "wave_assignment": final_level,
            },
            run_id=run.id,
        )
        return await repo.list_waves_for_run(self.db, run.id)

    async def next_wave(self, run: Run) -> Wave | None:
        """First wave (by sequence_order) whose status is not terminal."""
        for wave in await repo.list_waves_for_run(self.db, run.id):
            if wave.status not in TERMINAL_WAVE_STATUSES:
                return wave
        return None
