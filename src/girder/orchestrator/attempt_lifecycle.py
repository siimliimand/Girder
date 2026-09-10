"""Attempt lifecycle: allocation, teardown, and terminal bookkeeping.

Cut from :mod:`girder.orchestrator.task_engine` (WP 10.2). One attempt's
container/worktree provisioning (:func:`allocate_attempt`), the guaranteed
teardown (:func:`teardown_attempt`, cancellation-safe), and the terminal
transition helpers shared by the verify loop (``close_attempt``,
``retry_or_fail``, ``fail_task``, ``record_engine_error``).
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from girder.agent.runtime import AgentRuntime
from girder.db import repo
from girder.db.models import (
    Attempt,
    AttemptStatus,
    SteeringKind,
    Task,
    TaskStatus,
    TaskType,
    Worktree,
    WorktreeState,
)
from girder.fsm import InvalidTransition, transition_attempt, transition_task
from girder.gitops.audit import TestManifest
from girder.gitops.worktree import DEFAULT_BASE, WorktreeManager, WorktreeRef
from girder.guard.scope import TaskScopes
from girder.stacks import STACK_REGISTRY
from girder.util import utcnow_iso

if TYPE_CHECKING:
    from girder.db.models import Run
    from girder.orchestrator.task_engine import TaskEngine

# Keep the historical logger name (tests assert on it via caplog).
log = logging.getLogger("girder.orchestrator.task_engine")


@dataclass(frozen=True)
class AttemptContext:
    """Everything the verify loop needs about one live attempt."""

    attempt: Attempt
    worktree: WorktreeRef
    manifest: TestManifest
    container_id: str


async def allocate_attempt(
    engine: TaskEngine, task: Task, run: Run, base_commit: str
) -> AttemptContext:
    """Allocate one attempt: DB row, worktree, RO test snapshots, container."""
    attempt = await repo.create_attempt(engine.db, task.id, base_commit)
    manager = WorktreeManager(engine.repo_path, engine.worktree_base or DEFAULT_BASE)
    # Group B: a retry worktree must be created at the salvaged task-branch
    # tip — the default base (run-branch tip) does not contain the salvage
    # commit, and the retry would redo (or clobber) completed work. The
    # task branch persists across attempts (only the worktree is pruned),
    # and `worktree add -b` falls back to a detached checkout at this base
    # when the branch already exists.
    wt_base_commit = engine._salvage_tips.pop(task.id, base_commit)
    try:
        ref = await manager.create(run.id, task.id, wt_base_commit)
    except FileExistsError:
        # A prior attempt crashed mid-allocation (after provisioning,
        # before its teardown could prune). Attempts are sequential per
        # task, so the path can only be an orphan here — clear it and
        # provision fresh.
        await manager.heal_orphan(run.id, task.id)
        ref = await manager.create(run.id, task.id, wt_base_commit)
    await repo.create_worktree(engine.db, attempt.id, str(ref.path), ref.branch)
    attempt.worktree_path = str(ref.path)  # keep the local object in sync for teardown
    await repo.update_attempt_fields(
        engine.db, attempt.id, worktree_path=str(ref.path), started_at=utcnow_iso()
    )
    manifest = await engine._audit.capture_test_manifest(ref.path)
    # Layer 2 anchor: hash of every test-signal file before the agent acts.
    await repo.update_task_fields(engine.db, task.id, test_content_hash=manifest.root_hash)

    spec = engine._container_spec(f"girder-{attempt.id[:8]}", ref.path)
    # Layer 1 test shadowing (§8.2): mount a pristine RO snapshot of each
    # configured test directory over the worktree's own copy — one
    # snapshot dir + one RO shadow mount per test directory (the engine
    # renders ``ro_mounts`` as ``-v host:container:ro`` before the
    # workspace contents are visible to the agent). With no configured
    # test directories Layer 1 is inactive — warn, and rely on Layers 2
    # (re-hash) and 3 (diff audit) to catch tampering after the fact.
    test_dirs = engine.settings.project.test_directories
    snapshot_dirs: list[Path] = []
    if task.task_type is not TaskType.TEST_CHANGE:
        if not test_dirs:
            log.warning(
                "attempt %s: project.test_directories is empty — "
                "Layer 1 RO test snapshot is INACTIVE (§8.2)",
                attempt.id,
            )
        for test_dir in test_dirs:
            # A test-less repo (allow_empty_baseline) has no test dirs at
            # the base commit: skip the snapshot instead of crashing the
            # attempt — Layer 1 has nothing to protect there; Layers 2/3
            # still cover the (empty) test-signal surface.
            if not await engine._audit.commit_has_path(engine.repo_path, base_commit, test_dir):
                log.info(
                    "attempt %s: test dir %r absent at base commit — "
                    "Layer 1 RO snapshot skipped",
                    attempt.id,
                    test_dir,
                )
                continue
            snapshot_dir = Path(tempfile.mkdtemp(prefix="girder-snap-"))
            await engine._audit.materialize_test_snapshot(
                engine.repo_path, base_commit, [test_dir], snapshot_dir
            )
            # `git archive` keeps the path prefix, so the shadow source is
            # <snapshot>/<test_dir>, mounted over /workspace/<test_dir>.
            spec.ro_mounts[str(snapshot_dir / test_dir.strip("/"))] = (
                "/workspace/" + test_dir.strip("/")
            )
            snapshot_dirs.append(snapshot_dir)
    # §8.1 package cache (plan.md Phase 0 task 3, impl-plan §6.4): bind the
    # host cache dirs read-only at the paths the image's env vars point at
    # (PIP_CACHE_DIR/npm_config_cache/CARGO_HOME = /cache/*) so pip/npm/
    # cargo never fetch over the public internet per worktree. A missing
    # host subdir (dev machine, first boot) just skips that mount.
    cache_root = Path(engine.settings.sandbox.cache_dir)
    for cache_sub, container_path in (
        ("pip", "/cache/pip"),
        ("npm", "/cache/npm"),
        ("cargo", "/cache/cargo"),
    ):
        host_dir = cache_root / cache_sub
        if host_dir.is_dir():
            spec.ro_mounts[str(host_dir)] = container_path
        else:
            log.debug(
                "attempt %s: package cache %s missing — RO cache mount skipped",
                attempt.id,
                host_dir,
            )
    engine._snapshot_dirs[spec.name] = snapshot_dirs

    await engine.sandbox.start(spec)
    await repo.update_attempt_fields(engine.db, attempt.id, container_id=spec.name)
    await transition_attempt(engine.db, attempt.id, AttemptStatus.RUNNING)
    return AttemptContext(
        attempt=attempt, worktree=ref, manifest=manifest, container_id=spec.name
    )


async def teardown_attempt(engine: TaskEngine, ctx: AttemptContext, *, force: bool = True) -> None:
    """Tear down one attempt's container, snapshots and worktree.

    With ``force=False`` (cancellation protocol: the run pump owns the
    terminal transition) only the container kill and snapshot cleanup run —
    no worktree pruning, no DB writes.
    """
    with suppress(Exception):
        await engine.sandbox.kill(ctx.container_id)
    for snapshot_dir in engine._snapshot_dirs.pop(ctx.container_id, []):
        shutil.rmtree(snapshot_dir, ignore_errors=True)
    if not force:
        return
    wt_path = ctx.attempt.worktree_path
    if wt_path is not None:
        manager = WorktreeManager(engine.repo_path, engine.worktree_base or DEFAULT_BASE)
        try:
            await manager.remove(wt_path, force=True)
            row = await worktree_row(engine, ctx.attempt.id)
            if row is not None:
                await repo.set_worktree_state(engine.db, row.id, WorktreeState.PRUNED)
        except Exception:  # pragma: no cover - git failures leave it to the GC
            log.exception("failed to prune worktree %s", wt_path)
    await repo.update_attempt_fields(engine.db, ctx.attempt.id, ended_at=utcnow_iso())


async def kill_attempt_container(engine: TaskEngine, container: str) -> None:
    """Kill one attempt's container and its RO test snapshots, best-effort.

    Idempotent: the engine-level ``finally`` teardown repeats both steps
    (a second kill is a no-op, the snapshot map entry is already gone).
    """
    with suppress(Exception):
        await engine.sandbox.kill(container)
    for snapshot_dir in engine._snapshot_dirs.pop(container, []):
        shutil.rmtree(snapshot_dir, ignore_errors=True)


async def worktree_row(engine: TaskEngine, attempt_id: str) -> Worktree | None:
    for row in await repo.list_worktrees(engine.db):
        if row.attempt_id == attempt_id:
            return row
    return None


def build_runtime(
    engine: TaskEngine, run: Run, task: Task, attempt: Attempt, container: str
) -> AgentRuntime:
    """Compose the agent runtime for one live attempt."""
    return AgentRuntime(
        gateway=engine.gateway,
        sandbox=engine.sandbox,
        container=container,
        scopes=TaskScopes(
            write_globs=task.scope_globs,
            protected_globs=engine.settings.project.protected_read_paths,
            strict_read_scope=engine.settings.project.strict_read_scope,
        ),
        limits=engine.settings.limits,
        redactor=engine.redactor,
        db=engine.db,
        run_id=run.id,
        attempt=attempt,
        task=task,
        stack=STACK_REGISTRY[engine.settings.project.stack],
    )


async def record_engine_error(
    engine: TaskEngine, attempt: Attempt, exc: Exception
) -> None:
    """Record an unexpected engine error on the attempt row, then re-raise.

    Never silently swallow programming errors — mark the attempt FAILED
    (best-effort) with the failure reason, persist the traceback shape, and
    let the exception propagate.
    """
    with suppress(InvalidTransition):
        await transition_attempt(engine.db, attempt.id, AttemptStatus.FAILED)
    await repo.update_attempt_fields(
        engine.db,
        attempt.id,
        failure_reason=f"engine error: {type(exc).__name__}: {exc}",
        ended_at=utcnow_iso(),
    )


async def close_attempt(
    engine: TaskEngine,
    attempt_id: str,
    status: str,
    reason: str,
    *,
    turns_used: int | None = None,
) -> None:
    with suppress(InvalidTransition):
        await transition_attempt(engine.db, attempt_id, status)
    fields: dict[str, str | int | None] = {"failure_reason": reason, "ended_at": utcnow_iso()}
    if turns_used is not None:
        # Group G: every close path persists the agent turn count.
        fields["turns_used"] = turns_used
    await repo.update_attempt_fields(engine.db, attempt_id, **fields)


async def retry_or_fail(
    engine: TaskEngine, run: Run, task: Task, tail: str, *, event: str, note: str
) -> str | None:
    """Retry-schedule with feedback, or fail the task; returns next guidance."""
    fresh = await repo.get_task(engine.db, task.id)
    if fresh is None:
        return None
    await repo.insert_event(
        engine.db, event, {"task_id": task.id, "note": note, "tail": tail}, run_id=run.id
    )
    if fresh.attempts_used < engine.settings.limits.task_max_attempts:
        await transition_task(
            engine.db, fresh.id, TaskStatus.RETRY_SCHEDULED, payload={"reason": note}
        )
        return tail
    await fail_task(engine, fresh, note)
    return None


async def standing_guidance(engine: TaskEngine, run: Run, task: Task) -> str | None:
    """Compose the standing per-task guidance (§6.9 + WP 6.2).

    Rejection guidance (§6.9): a user-rejected spec amendment persists
    guidance that must reach the agent as trusted steering. Steering injects
    that arrived between tasks or during a pause were never consumed by an
    agent turn — fold them in too (blank-line separated). User-authored ⇒
    trusted per §7.
    """
    rejection_guidance = await repo.get_rejected_guidance(engine.db, run.id, task.id)
    injects = await repo.consume_steering_events(
        engine.db, run.id, kinds=[SteeringKind.INJECT.value]
    )
    directives = [
        str(e["payload"].get("directive", "") or "").strip()
        for e in injects
        if isinstance(e["payload"], dict)
        and str(e["payload"].get("directive", "") or "").strip()
    ]
    return "\n\n".join(g for g in (rejection_guidance, *directives) if g) or None


async def fail_task(engine: TaskEngine, task: Task, reason: str) -> None:
    fresh = await repo.get_task(engine.db, task.id)
    if fresh is None:
        return
    with suppress(InvalidTransition):
        await transition_task(engine.db, fresh.id, TaskStatus.FAILED, payload={"reason": reason})
    if engine.notifier is not None:
        await engine.notifier.notify(
            "error",
            "Girder: task failed",
            f"task {task.id} ({task.title}): {reason}",
        )
