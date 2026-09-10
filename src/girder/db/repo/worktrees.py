"""Worktree rows: lifecycle and GC/recovery queries."""

from __future__ import annotations

from aiosqlite import Row

from girder.db.engine import Database
from girder.db.models import Worktree, WorktreeState
from girder.db.repo._common import _TERMINAL_ATTEMPT_SQL, _TERMINAL_TASK_SQL
from girder.util import new_id, utcnow_iso


async def create_worktree(db: Database, attempt_id: str, path: str, branch: str) -> Worktree:
    wt = Worktree(
        id=new_id(), attempt_id=attempt_id, path=path, branch=branch, created_at=utcnow_iso()
    )
    async with db.tx() as conn:
        # A task retry reuses the same per-task path; the previous (pruned or
        # quarantined) row for it is superseded history — drop it so the
        # UNIQUE(path) constraint doesn't block the new attempt's row.
        await conn.execute(
            "DELETE FROM worktrees WHERE path = ? AND state != 'active'", (path,)
        )
        await conn.execute(
            "INSERT INTO worktrees (id, attempt_id, path, branch, state, created_at)"
            " VALUES (?, ?, ?, ?, 'active', ?)",
            (wt.id, attempt_id, path, branch, wt.created_at),
        )
    return wt


def _row_to_worktree(r: Row) -> Worktree:
    return Worktree(
        id=r["id"],
        attempt_id=r["attempt_id"],
        path=r["path"],
        branch=r["branch"],
        state=WorktreeState(r["state"]),
        created_at=r["created_at"],
        removed_at=r["removed_at"],
    )


async def get_worktree(db: Database, worktree_id: str) -> Worktree | None:
    r = await db.fetchone("SELECT * FROM worktrees WHERE id = ?", (worktree_id,))
    return _row_to_worktree(r) if r else None


async def list_worktrees(db: Database, state: WorktreeState | None = None) -> list[Worktree]:
    if state is None:
        rows = await db.fetchall("SELECT * FROM worktrees ORDER BY created_at")
    else:
        rows = await db.fetchall(
            "SELECT * FROM worktrees WHERE state = ? ORDER BY created_at", (state.value,)
        )
    return [_row_to_worktree(r) for r in rows]


async def set_worktree_state(db: Database, worktree_id: str, state: WorktreeState) -> None:
    removed_at = utcnow_iso() if state is not WorktreeState.ACTIVE else None
    async with db.tx() as conn:
        await conn.execute(
            "UPDATE worktrees SET state = ?, removed_at = ? WHERE id = ?",
            (state.value, removed_at, worktree_id),
        )


async def find_stale_worktrees(db: Database) -> list[Worktree]:
    """Active worktrees whose attempt or task has reached a terminal state.

    These are the GC daemon's targets: pruned within 60s of task resolution
    (plan.md §8.1).
    """
    rows = await db.fetchall(
        f"""
        SELECT w.* FROM worktrees w
        JOIN attempts a ON w.attempt_id = a.id
        LEFT JOIN tasks t ON a.task_id = t.id
        WHERE w.state = 'active'
          AND (a.status IN ({_TERMINAL_ATTEMPT_SQL})
               OR t.status IN ({_TERMINAL_TASK_SQL}))
        """
    )
    return [_row_to_worktree(r) for r in rows]


async def find_active_worktrees_with_context(db: Database) -> list[dict[str, str]]:
    """Active worktrees joined to their repo and run — recovery uses this to
    prune worktrees of crashed attempts (needs the owning repo for git)."""
    rows = await db.fetchall(
        """
        SELECT w.id AS worktree_id, w.path, w.branch, a.id AS attempt_id, a.status,
               r.status AS run_status, p.repo_path
        FROM worktrees w
        JOIN attempts a ON w.attempt_id = a.id
        JOIN tasks t ON a.task_id = t.id
        JOIN waves v ON t.wave_id = v.id
        JOIN runs r ON v.run_id = r.id
        JOIN projects p ON r.project_id = p.id
        WHERE w.state = 'active'
        """
    )
    return [dict(r) for r in rows]
