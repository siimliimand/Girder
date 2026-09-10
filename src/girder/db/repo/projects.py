"""Project rows: creation, lookup, autonomy-tier and clean-merge streak."""

from __future__ import annotations

from typing import Any

from aiosqlite import Row

from girder.db.engine import Database
from girder.db.models import Project
from girder.util import new_id, utcnow_iso


async def create_project(
    db: Database, name: str, repo_path: str, *, autonomy_tier: int = 0
) -> Project:
    project = Project(id=new_id(), name=name, repo_path=repo_path, autonomy_tier=autonomy_tier)
    async with db.tx() as conn:
        await conn.execute(
            "INSERT INTO projects (id, name, repo_path, autonomy_tier, clean_merge_streak,"
            " config_json, created_at, updated_at) VALUES (?, ?, ?, ?, 0, '{}', ?, ?)",
            (project.id, name, repo_path, autonomy_tier, utcnow_iso(), utcnow_iso()),
        )
    return project


def _row_to_project(r: Row) -> Project:
    return Project(
        id=r["id"],
        name=r["name"],
        repo_path=r["repo_path"],
        autonomy_tier=r["autonomy_tier"],
        clean_merge_streak=r["clean_merge_streak"],
        config_json=r["config_json"],
    )


async def get_project(db: Database, project_id: str) -> Project | None:
    r = await db.fetchone("SELECT * FROM projects WHERE id = ?", (project_id,))
    return _row_to_project(r) if r else None


async def get_project_by_name(db: Database, name: str) -> Project | None:
    r = await db.fetchone("SELECT * FROM projects WHERE name = ?", (name,))
    return _row_to_project(r) if r else None


async def list_projects(db: Database) -> list[Project]:
    return [_row_to_project(r) for r in await db.fetchall("SELECT * FROM projects ORDER BY name")]


async def set_project_tier(db: Database, project_id: str, tier: int) -> None:
    """Autonomy-tier override (§2.3). Demotion is instant; promotion gating is
    the caller's job (tier console route enforces the T2 streak threshold)."""
    if tier not in (0, 1, 2):
        raise ValueError(f"invalid autonomy tier: {tier}")
    await db.execute(
        "UPDATE projects SET autonomy_tier = ?, updated_at = ? WHERE id = ?",
        (tier, utcnow_iso(), project_id),
    )
    await db.conn.commit()


async def bump_clean_merge_streak(db: Database, project_id: str) -> int:
    """Increment and return the project's clean-merge streak (§2.3 T2 gate).

    Zeroing on integrity violations already happens inside
    :func:`girder.db.repo.integrity.insert_integrity_violation`; escalated/clean
    bookkeeping lives here.
    """
    async with db.tx() as conn:
        await conn.execute(
            "UPDATE projects SET clean_merge_streak = clean_merge_streak + 1, updated_at = ?"
            " WHERE id = ?",
            (utcnow_iso(), project_id),
        )
        async with conn.execute(
            "SELECT clean_merge_streak FROM projects WHERE id = ?", (project_id,)
        ) as cur:
            row = await cur.fetchone()
    streak: Any = row["clean_merge_streak"] if row else 0
    return int(streak)


async def reset_clean_merge_streak(db: Database, project_id: str) -> None:
    """Reset the project's clean-merge streak to zero.

    Appended by the delivery-fix pass (impl-plan §9.2: any escalated merge
    resets the streak) — previously the reset existed only inline inside
    :func:`girder.db.repo.integrity.insert_integrity_violation`, with no
    reusable helper.
    """
    await db.execute(
        "UPDATE projects SET clean_merge_streak = 0, updated_at = ? WHERE id = ?",
        (utcnow_iso(), project_id),
    )
