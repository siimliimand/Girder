"""Worktree garbage-collection daemon (plan.md §8.1, impl-plan §6.12).

The instant a task reaches a terminal status, its worktree is pruned with
``git worktree remove --force`` — target: gone within 60 seconds. A disk
guard alerts when the worktree volume passes the configured usage threshold
(80% default) — edge-triggered, so it re-alerts only after recovering below
the threshold and crossing again.

Notifications go through the redaction pipeline by contract: the Notifier
applies it; this daemon never handles raw tool output anyway.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from girder.db import repo
from girder.db.engine import Database
from girder.db.models import WorktreeState
from girder.gitops.worktree import DEFAULT_BASE, WorktreeManager

log = logging.getLogger(__name__)


@dataclass
class GCResult:
    pruned: list[str] = field(default_factory=list)
    quarantined: list[str] = field(default_factory=list)
    disk_alert: str | None = None


class WorktreeGC:
    def __init__(
        self,
        db: Database,
        base: Path = DEFAULT_BASE,
        *,
        alert_pct: float = 80.0,
        # poll_interval_s carries the 60s pruning guarantee (WP-1.6 / plan.md §8.1):
        # with a 15s cadence a terminal worktree is pruned within two polls.
        poll_interval_s: float = 15.0,
        notifier: object | None = None,
    ) -> None:
        self.db = db
        self.base = Path(base)
        self.alert_pct = alert_pct
        self.poll_interval_s = poll_interval_s
        self.notifier = notifier
        self._alert_armed = True  # edge trigger state
        self._bg_tasks: set[asyncio.Task[None]] = set()  # fire-and-forget notifies

    async def run_once(self) -> GCResult:
        result = GCResult()
        for wt in await repo.find_stale_worktrees(self.db):
            try:
                # The owning repo is the one true place to run git against.
                ctx = await self._context_for(wt.attempt_id)
                if ctx is None:
                    continue
                manager = WorktreeManager(Path(ctx["repo_path"]), self.base)
                await manager.remove(wt.path, force=True)
                await repo.set_worktree_state(self.db, wt.id, WorktreeState.PRUNED)
                result.pruned.append(wt.path)
                log.info("pruned worktree %s (terminal since %s)", wt.path, wt.removed_at)
            except Exception:
                await repo.set_worktree_state(self.db, wt.id, WorktreeState.QUARANTINED)
                result.quarantined.append(wt.path)
                log.exception("failed to prune worktree %s — quarantined", wt.path)

        result.disk_alert = self._disk_guard()
        return result

    async def run_forever(self) -> None:
        """Background loop; intended to run as an asyncio task in the daemon."""
        while True:
            try:
                await self.run_once()
            except Exception:
                log.exception("GC pass failed; retrying next interval")
            await asyncio.sleep(self.poll_interval_s)

    async def _context_for(self, attempt_id: str) -> dict[str, str] | None:
        for ctx in await repo.find_active_worktrees_with_context(self.db):
            if ctx["attempt_id"] == attempt_id:
                return ctx
        return None

    def _disk_guard(self) -> str | None:
        """Edge-triggered threshold alert on the worktree volume."""
        if not self.base.exists():
            return None
        usage = shutil.disk_usage(self.base)
        pct = usage.used / usage.total * 100 if usage.total else 0.0
        if pct >= self.alert_pct:
            if self._alert_armed:
                self._alert_armed = False
                msg = (
                    f"worktree volume {self.base} at {pct:.0f}% "
                    f"({usage.used // (1 << 20)}MiB/{usage.total // (1 << 20)}MiB)"
                )
                log.warning("DISK ALERT: %s", msg)
                if self.notifier is not None and hasattr(self.notifier, "notify"):
                    task = asyncio.create_task(  # fire-and-forget; notifier redacts
                        self.notifier.notify("warning", "Worktree volume nearly full", msg)
                    )
                    self._bg_tasks.add(task)
                    task.add_done_callback(self._bg_tasks.discard)
                return msg
            return None
        self._alert_armed = True  # recovered below threshold; re-arm
        return None
