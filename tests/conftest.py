"""Shared fixtures: an opened, migrated, throwaway database."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from girder.db.engine import Database, default_migrations_dir


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = await Database.open(tmp_path / "girder.db", migrations_dir=default_migrations_dir())
    yield database
    await database.close()


async def seed_run_status(db: Database, run_id: str, status: str) -> None:
    """Deliberately bypasses the FSM — test-only seeding of a run's status."""
    await db.execute("UPDATE runs SET status = ? WHERE id = ?", (status, run_id))
    await db.conn.commit()
