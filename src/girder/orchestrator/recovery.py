"""Boot-time crash recovery (plan.md §8.7 / impl-plan §6.12).

The SQLite state store is the single source of truth; on boot, *after*
migrations, this service reconciles the world to match it:

1. Any attempt left ``running`` is transitioned to ``crashed`` — an attempt is
   disposable (D8); completed tasks are never re-executed.
2. Worktrees of crashed/terminal attempts are pruned (``git worktree remove
   --force``); unremovable ones are quarantined for human inspection.
3. Tasks caught mid-flight (``running`` / ``verifying``) are rescheduled — or
   marked failed outright when the attempt budget is exhausted.
4. Git/DB consistency is verified for every non-terminal run (§8.7 step 4):
   a missing run branch is recreated from ``main`` — except for runs in a
   delivery state (pr_open … merge_pending_human), whose PR lives on the
   remote: their branch is restored from the remote head, or the run is
   left for operator attention (never silently rebuilt at ``main``);
   worktree rows whose path
   vanished from disk are pruned so the scheduler rebuilds cleanly from the
   task's base commit.
5. If anything was recovered, a notification with a recovery report goes out
   (through the redaction pipeline, like every notification).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from aiosqlite import Row

from girder.config import Settings
from girder.db import repo
from girder.db.engine import Database
from girder.db.models import (
    TERMINAL_RUN_STATUSES,
    AttemptStatus,
    RunStatus,
    TaskStatus,
    WorktreeState,
)
from girder.fsm import transition_attempt, transition_task
from girder.gitops.branch import BranchOps
from girder.gitops.worktree import WorktreeManager
from girder.notify.notifier import Notifier
from girder.util import run_host_cmd

log = logging.getLogger(__name__)

# Runs in a delivery state have (or are getting) a PR on the remote: their
# run branch must never be silently rebuilt at local ``main`` — the pump
# would poll/merge the PR against the wrong sha (§8.7 step 4).
_DELIVERY_RUN_STATUSES = frozenset(
    {
        RunStatus.PR_OPEN,
        RunStatus.CI_RUNNING,
        RunStatus.CI_FIXING,
        RunStatus.CONFORMANCE_REVIEW,
        RunStatus.MERGE_PENDING_HUMAN,
    }
)


@dataclass
class RecoveryReport:
    crashed_attempts: list[str] = field(default_factory=list)
    pruned_worktrees: list[str] = field(default_factory=list)
    quarantined_worktrees: list[str] = field(default_factory=list)
    rescheduled_tasks: list[str] = field(default_factory=list)
    failed_tasks: list[str] = field(default_factory=list)
    recreated_branches: list[str] = field(default_factory=list)
    restored_branches: list[str] = field(default_factory=list)
    attention_branches: list[str] = field(default_factory=list)

    @property
    def anything_recovered(self) -> bool:
        return bool(
            self.crashed_attempts
            or self.pruned_worktrees
            or self.quarantined_worktrees
            or self.rescheduled_tasks
            or self.failed_tasks
            or self.recreated_branches
            or self.restored_branches
            or self.attention_branches
        )

    def summary(self) -> str:
        lines = ["Recovery report:"]
        if self.crashed_attempts:
            lines.append(f"- {len(self.crashed_attempts)} attempt(s) crashed mid-flight")
        if self.recreated_branches:
            lines.append(
                f"- {len(self.recreated_branches)} missing run branch(es) recreated from main"
            )
        if self.restored_branches:
            lines.append(
                f"- {len(self.restored_branches)} delivery run branch(es) restored from remote"
            )
        if self.attention_branches:
            lines.append(
                f"- {len(self.attention_branches)} delivery run branch(es) missing locally AND"
                " remotely — operator attention required"
            )
        if self.pruned_worktrees:
            lines.append(f"- {len(self.pruned_worktrees)} worktree(s) pruned")
        if self.quarantined_worktrees:
            lines.append(f"- {len(self.quarantined_worktrees)} worktree(s) quarantined")
        if self.rescheduled_tasks:
            lines.append(f"- {len(self.rescheduled_tasks)} task(s) rescheduled for retry")
        if self.failed_tasks:
            lines.append(f"- {len(self.failed_tasks)} task(s) failed (attempt budget exhausted)")
        if not self.anything_recovered:
            lines.append("- nothing to recover; state was clean")
        return "\n".join(lines)


class RecoveryService:
    def __init__(self, db: Database, settings: Settings, notifier: Notifier | None = None):
        self.db = db
        self.settings = settings
        self.notifier = notifier

    async def recover(self) -> RecoveryReport:
        report = RecoveryReport()
        await self._crash_running_attempts(report)
        await self._reschedule_interrupted_tasks(report)
        await self._prune_dead_worktrees(report)
        await self._reconcile_run_branches(report)
        await self._verify_no_live_worktrees_on_terminal_runs()
        if report.anything_recovered and self.notifier is not None:
            await self.notifier.notify("warning", "Girder restarted after crash", report.summary())
        log.info("%s", report.summary())
        return report

    # ------------------------------------------------------------------ steps

    async def _crash_running_attempts(self, report: RecoveryReport) -> None:
        for attempt in await repo.find_attempts_in_status(self.db, AttemptStatus.RUNNING):
            await transition_attempt(
                self.db, attempt.id, AttemptStatus.CRASHED, payload={"reason": "host restart"}
            )
            await repo.update_attempt_fields(
                self.db, attempt.id, failure_reason="crashed: orchestrator restart"
            )
            report.crashed_attempts.append(attempt.id)

    async def _reschedule_interrupted_tasks(self, report: RecoveryReport) -> None:
        interrupted = await repo.find_tasks_in_statuses(
            self.db, [TaskStatus.RUNNING.value, TaskStatus.VERIFYING.value]
        )
        for task in interrupted:
            if task.attempts_used >= self.settings.limits.task_max_attempts:
                await transition_task(
                    self.db,
                    task.id,
                    TaskStatus.FAILED,
                    payload={"reason": "attempt budget exhausted at restart"},
                )
                report.failed_tasks.append(task.id)
            else:
                await transition_task(
                    self.db,
                    task.id,
                    TaskStatus.RETRY_SCHEDULED,
                    payload={"reason": "recovered after restart"},
                )
                report.rescheduled_tasks.append(task.id)

    async def _prune_dead_worktrees(self, report: RecoveryReport) -> None:
        """Remove worktrees whose attempt is no longer live (crashed/terminal)."""
        for ctx in await repo.find_active_worktrees_with_context(self.db):
            if ctx["status"] == AttemptStatus.RUNNING.value:
                continue  # still live — nothing to do (should not happen post-step-1)
            manager = WorktreeManager(Path(ctx["repo_path"]))
            if not (Path(ctx["path"]) / ".git").exists():
                # §8.7 step 4: the path is gone (or never was a worktree) —
                # the DB row is the stale side. Prune the ROW so the
                # scheduler rebuilds cleanly from the task's base commit;
                # prune git's stale metadata too.
                await manager.remove(ctx["path"], force=True)
                await repo.set_worktree_state(self.db, ctx["worktree_id"], WorktreeState.PRUNED)
                report.pruned_worktrees.append(ctx["path"])
                log.warning(
                    "worktree row %s points at missing path %s — pruned",
                    ctx["worktree_id"],
                    ctx["path"],
                )
                continue
            try:
                await manager.remove(ctx["path"], force=True)
                await repo.set_worktree_state(self.db, ctx["worktree_id"], WorktreeState.PRUNED)
                report.pruned_worktrees.append(ctx["path"])
            except Exception:
                await repo.set_worktree_state(
                    self.db, ctx["worktree_id"], WorktreeState.QUARANTINED
                )
                report.quarantined_worktrees.append(ctx["path"])
                log.exception("could not prune worktree %s", ctx["path"])

    async def _reconcile_run_branches(self, report: RecoveryReport) -> None:
        """§8.7 step 4: verify git state matches the DB. A non-terminal run
        whose branch is missing from git gets it recreated from ``main``
        (the baseline anchor) so pumping can resume — except for delivery
        states: there the PR lives on the remote, so the branch is restored
        from the remote head, or (remote unreachable/branch absent) the run
        is left for operator attention. Every action is logged, audited, and
        lands in the recovery report."""
        terminal = [s.value for s in TERMINAL_RUN_STATUSES]
        rows = await self.db.fetchall(
            f"""
            SELECT r.id AS run_id, r.status AS run_status, r.branch, p.repo_path
            FROM runs r
            JOIN projects p ON r.project_id = p.id
            WHERE r.status NOT IN ({','.join('?' for _ in terminal)})
            """,
            tuple(terminal),
        )
        for row in rows:
            branch_ops = BranchOps(Path(row["repo_path"]))
            if await branch_ops.run_branch_tip(row["branch"]) is not None:
                continue
            if row["run_status"] in {s.value for s in _DELIVERY_RUN_STATUSES}:
                await self._restore_delivery_branch(row, branch_ops, report)
                continue
            base = "HEAD"
            for ref in ("main", "origin/main"):
                sha = await branch_ops.resolve_ref(ref)
                if sha is not None:
                    base = sha
                    break
            tip = await branch_ops.ensure_run_branch(row["branch"], base_commit=base)
            log.warning(
                "run %s: branch %s missing from git — recreated at %s",
                row["run_id"],
                row["branch"],
                tip,
            )
            report.recreated_branches.append(row["branch"])

    async def _restore_delivery_branch(
        self, row: Row, branch_ops: BranchOps, report: RecoveryReport
    ) -> None:
        """A delivery-state run lost its local branch: restore it from the
        remote (the PR's source of truth), never rebuild at ``main``."""
        run_id, branch = row["run_id"], row["branch"]
        remote = self.settings.github.remote
        proc = await run_host_cmd(
            ["git", "-C", str(branch_ops.repo_path), "fetch", remote, f"{branch}:{branch}"],
            check=False,
            timeout_s=120,
        )
        tip = await branch_ops.run_branch_tip(branch) if proc.returncode == 0 else None
        if tip is not None:
            log.warning(
                "run %s: branch %s missing locally — restored from %s at %s",
                run_id,
                branch,
                remote,
                tip,
            )
            await repo.insert_event(
                self.db,
                "run_branch_restored_from_remote",
                {"branch": branch, "remote": remote, "tip": tip},
                run_id=run_id,
            )
            report.restored_branches.append(branch)
            return
        # Remote unreachable or branch absent remotely: rebuilding at main
        # would point the delivery pump at the wrong sha — leave for a human.
        log.error(
            "run %s: delivery branch %s missing locally and not restorable from %s"
            " (%s) — operator attention required",
            run_id,
            branch,
            remote,
            proc.stderr.strip()[:200],
        )
        await repo.insert_event(
            self.db,
            "run_branch_unrestorable",
            {"branch": branch, "remote": remote, "detail": proc.stderr.strip()[:500]},
            run_id=run_id,
        )
        report.attention_branches.append(branch)

    async def _verify_no_live_worktrees_on_terminal_runs(self) -> None:
        """Sanity probe: no *active* worktree row may belong to a terminal run.

        The GC daemon would eventually collect these; recovery flags them now
        so a reboot surfaces the inconsistency immediately.
        """
        terminal = {s.value for s in TERMINAL_RUN_STATUSES}
        for ctx in await repo.find_active_worktrees_with_context(self.db):
            if ctx["run_status"] in terminal:
                log.warning(
                    "active worktree %s belongs to terminal run (status=%s) — GC will collect it",
                    ctx["path"],
                    ctx["run_status"],
                )
