"""Shared fixtures: an opened, migrated, throwaway database."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from girder.db.engine import Database, default_migrations_dir


@pytest.fixture(autouse=True)
def _hermetic_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Run every test from a temp CWD so the TOML walk-up cannot see the
    developer's repo girder.toml.

    ``Settings()`` / ``load_settings()`` discover girder.toml by walking up
    from CWD; without this the suite silently picks up the repo's config
    (hermeticity bug). Tests that need a toml write one under ``tmp_path`` or
    pass an explicit path — the walk-up still works because tmp_path is
    outside the repository.
    """
    monkeypatch.chdir(tmp_path)
    yield


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = await Database.open(tmp_path / "girder.db", migrations_dir=default_migrations_dir())
    yield database
    await database.close()


async def seed_run_status(db: Database, run_id: str, status: str) -> None:
    """Deliberately bypasses the FSM — test-only seeding of a run's status."""
    await db.execute("UPDATE runs SET status = ? WHERE id = ?", (status, run_id))
    await db.conn.commit()
