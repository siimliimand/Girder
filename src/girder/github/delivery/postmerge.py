"""Post-merge bookkeeping (split from the original ``girder.github.delivery``
module, SHA 5ff969586361fac1ce28ade40cc8d7dabf2d62d9).

§9.2 after any tier's merge: sync local main, archive the frozen spec,
prune the run's worktrees, and produce a redacted diff stat for the
notification. All ``DeliveryEngine`` mixin methods; all best effort —
bookkeeping must never fail the merged run.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from girder.db import repo
from girder.db.models import Project, Run, WorktreeState
from girder.gitops.branch import BranchOps
from girder.gitops.worktree import WorktreeManager
from girder.util import run_host_cmd, utcnow_iso

from girder.github.delivery.suite import _PROPOSAL_PATH, _TAIL_CHARS, SuiteMixin

if TYPE_CHECKING:
    from girder.config import Settings
    from girder.db.engine import Database
    from girder.guard.redact import Redactor

log = logging.getLogger("girder.github.delivery")


class PostMergeMixin(SuiteMixin):
    """Best-effort post-merge bookkeeping (§9.2)."""

    if TYPE_CHECKING:  # attributes provided by DeliveryEngine
        settings: Settings
        redactor: Redactor
        db: Database

    async def _sync_main(self, repo_path: Path) -> None:
        """Fast-forward local ``main`` to the remote after a merge (best effort)."""
        try:
            await run_host_cmd(
                ["git", "-C", str(repo_path), "fetch", self.settings.github.remote, "main"],
                timeout_s=120,
            )
            fetched = (
                await run_host_cmd(
                    ["git", "-C", str(repo_path), "rev-parse", "FETCH_HEAD"], timeout_s=30
                )
            ).stdout.strip()
            old = await BranchOps(repo_path).run_branch_tip("main")
            if old is None:
                await run_host_cmd(
                    ["git", "-C", str(repo_path), "update-ref", "refs/heads/main", fetched],
                    timeout_s=30,
                )
                return
            if old == fetched:
                return
            # ff-only move: old must be an ancestor of fetched
            check = await run_host_cmd(
                ["git", "-C", str(repo_path), "merge-base", "--is-ancestor", old, fetched],
                check=False,
                timeout_s=30,
            )
            if check.returncode == 0:
                await run_host_cmd(
                    ["git", "-C", str(repo_path), "update-ref", "refs/heads/main", fetched, old],
                    timeout_s=30,
                )
        except Exception as exc:  # bookkeeping must never fail the merged run
            log.warning("main sync after merge failed: %s", exc)

    async def _archive_spec(self, run: Run, repo_path: Path) -> None:
        """``openspec/proposals/<run>.md`` → ``openspec/archive/<date>-<run8>.md``
        as a commit on local main (§9.2), via a detached throwaway worktree."""
        proposal = _PROPOSAL_PATH.format(run_id=run.id)
        date = utcnow_iso()[:10].replace("-", "")
        archive = f"openspec/archive/{date}-{run.id[:8]}.md"
        try:
            check = await run_host_cmd(
                ["git", "-C", str(repo_path), "cat-file", "-e", f"main:{proposal}"],
                check=False,
                timeout_s=30,
            )
            if check.returncode != 0:
                return
            main_tip = (
                await run_host_cmd(
                    ["git", "-C", str(repo_path), "rev-parse", "refs/heads/main"], timeout_s=30
                )
            ).stdout.strip()
            tmp = Path(tempfile.mkdtemp(prefix="girder-archive-"))
            worktree = tmp / "wt"
            try:
                await run_host_cmd(
                    ["git", "-C", str(repo_path), "worktree", "add", "--detach",
                     str(worktree), main_tip],
                    timeout_s=60,
                )
                (worktree / "openspec" / "archive").mkdir(parents=True, exist_ok=True)
                await run_host_cmd(
                    ["git", "-C", str(worktree), "mv", proposal, archive], timeout_s=30
                )
                await run_host_cmd(
                    ["git", "-C", str(worktree), "commit", "-m", f"spec: archive {run.id[:8]}"],
                    env={
                        "GIT_AUTHOR_NAME": "girder",
                        "GIT_AUTHOR_EMAIL": "girder@localhost",
                        "GIT_COMMITTER_NAME": "girder",
                        "GIT_COMMITTER_EMAIL": "girder@localhost",
                    },
                    timeout_s=60,
                )
                new_tip = (
                    await run_host_cmd(
                        ["git", "-C", str(worktree), "rev-parse", "HEAD"], timeout_s=30
                    )
                ).stdout.strip()
                await run_host_cmd(
                    ["git", "-C", str(repo_path), "update-ref", "refs/heads/main",
                     new_tip, main_tip],
                    timeout_s=30,
                )
            finally:
                await self._cleanup_worktree(repo_path, worktree)
                shutil.rmtree(tmp, ignore_errors=True)
        except Exception as exc:  # bookkeeping must never fail the merged run
            log.warning("spec archive after merge failed: %s", exc)

    async def _prune_run_worktrees(self, run: Run, repo_path: Path) -> None:
        """Remove any worktree rows belonging to this run's attempts (§9.2)."""
        manager = WorktreeManager(repo_path)
        run_tasks = {t.id for t in await repo.list_tasks_for_run(self.db, run.id)}
        for wt in await repo.list_worktrees(self.db):
            attempt = await repo.get_attempt(self.db, wt.attempt_id)
            if attempt is None or attempt.task_id not in run_tasks:
                continue
            try:
                await manager.remove(wt.path)
                await repo.set_worktree_state(self.db, wt.id, WorktreeState.PRUNED)
            except Exception as exc:
                log.warning("worktree prune failed for %s: %s", wt.path, exc)

    async def _diff_stat(self, run: Run, repo_path: Path) -> str:
        try:
            proc = await run_host_cmd(
                ["git", "-C", str(repo_path), "diff", "--stat", f"main...{run.branch}"],
                check=False,
                timeout_s=30,
            )
            clean, _ = self.redactor.redact(proc.stdout.strip()[-_TAIL_CHARS:])
            return clean
        except Exception:  # pragma: no cover - cosmetic
            return ""
