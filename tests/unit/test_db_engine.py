"""WP-1.3 — Database engine: migrations, tamper detection, txn semantics."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from girder.db.engine import Database, MigrationError, default_migrations_dir


async def test_fresh_boot_applies_all_migrations(tmp_path: Path) -> None:
    db = await Database.open(tmp_path / "g.db", migrations_dir=default_migrations_dir())
    try:
        rows = await db.fetchall("SELECT version FROM schema_migrations ORDER BY version")
        versions = [r["version"] for r in rows]
        assert versions == [1, 2, 3, 4, 5, 6, 7]
        # every table from the DDL exists
        for table in (
            "projects",
            "runs",
            "baseline_runs",
            "waves",
            "tasks",
            "flaky_tests",
            "attempts",
            "worktrees",
            "agent_events",
            "token_usage",
            "tool_calls",
            "redaction_log",
            "spec_amendments",
            "ci_check_results",
            "integrity_violations",
            "notifications_log",
            "steering_events",
        ):
            r = await db.fetchone(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
            )
            assert r is not None, f"missing table {table}"
    finally:
        await db.close()


async def test_open_creates_missing_parent_dirs(tmp_path: Path) -> None:
    """First-ever run: the DB's parent directory (e.g. ~/.local/share/girder) may not exist."""
    path = tmp_path / "a" / "b" / "g.db"
    db = await Database.open(path, migrations_dir=default_migrations_dir())
    try:
        applied = await db.fetchall("SELECT version FROM schema_migrations")
        assert [r["version"] for r in applied] == [1, 2, 3, 4, 5, 6, 7]
    finally:
        await db.close()
    assert path.is_file()


async def test_memory_db_creates_no_file(tmp_path: Path) -> None:
    db = await Database.open(":memory:", migrations_dir=default_migrations_dir())
    try:
        r = await db.fetchone("SELECT COUNT(*) AS c FROM schema_migrations")
        assert r["c"] == 7
    finally:
        await db.close()
    assert list(tmp_path.iterdir()) == []  # noqa: ASYNC240


async def test_reopen_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "g.db"
    db1 = await Database.open(path, migrations_dir=default_migrations_dir())
    await db1.close()
    db2 = await Database.open(path, migrations_dir=default_migrations_dir())
    try:
        applied = await db2.fetchall("SELECT version FROM schema_migrations")
        assert len(applied) == 7  # nothing re-applied
    finally:
        await db2.close()


async def test_tampered_migration_fails_boot(tmp_path: Path) -> None:
    migrations = tmp_path / "migrations"
    shutil.copytree(default_migrations_dir(), migrations)
    db = await Database.open(tmp_path / "g.db", migrations_dir=migrations)
    await db.close()

    victim = migrations / "003_attempts_worktrees.sql"
    victim.write_text(victim.read_text() + "\n-- edited after apply")

    with pytest.raises(MigrationError, match="modified after being applied"):
        await Database.open(tmp_path / "g.db", migrations_dir=migrations)


async def test_deleted_migration_fails_boot(tmp_path: Path) -> None:
    migrations = tmp_path / "migrations"
    shutil.copytree(default_migrations_dir(), migrations)
    db = await Database.open(tmp_path / "g.db", migrations_dir=migrations)
    await db.close()

    (migrations / "005_redaction_amendments_ci.sql").unlink()

    with pytest.raises(MigrationError, match="missing from disk"):
        await Database.open(tmp_path / "g.db", migrations_dir=migrations)


async def test_bad_filename_rejected(tmp_path: Path) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "no_version_prefix.sql").write_text("SELECT 1;")
    with pytest.raises(MigrationError, match="NNN_description"):
        await Database.open(tmp_path / "g.db", migrations_dir=migrations)


async def test_pragmas(tmp_path: Path) -> None:
    db = await Database.open(tmp_path / "g.db", migrations_dir=default_migrations_dir())
    try:
        mode = await db.fetchone("PRAGMA journal_mode")
        fk = await db.fetchone("PRAGMA foreign_keys")
        busy = await db.fetchone("PRAGMA busy_timeout")
        assert mode["journal_mode"] == "wal"
        assert fk["foreign_keys"] == 1
        assert busy["timeout"] == 5000
    finally:
        await db.close()


async def test_concurrent_writers_no_lost_updates(db: Database) -> None:
    """Three tasks inserting through serialized IMMEDIATE txns — no losses."""

    async def worker(n: int) -> None:
        for _ in range(25):
            async with db.tx() as conn:
                await conn.execute(
                    "INSERT INTO projects (id, name, repo_path, autonomy_tier,"
                    " clean_merge_streak, config_json, created_at, updated_at)"
                    " VALUES (?, ?, '.', 0, 0, '{}', 't', 't')",
                    (f"p{n}-{_:04d}", f"proj{n}-{_:04d}"),
                )

    await asyncio.gather(worker(1), worker(2), worker(3))
    count = await db.fetchone("SELECT COUNT(*) AS c FROM projects")
    assert count["c"] == 75


async def test_tx_rollback_on_error(db: Database) -> None:
    with pytest.raises(Exception, match="boom"):
        async with db.tx() as conn:
            await conn.execute(
                "INSERT INTO projects (id, name, repo_path, created_at, updated_at)"
                " VALUES ('x1', 'x', '.', 't', 't')"
            )
            raise Exception("boom")
    r = await db.fetchone("SELECT COUNT(*) AS c FROM projects WHERE id = 'x1'")
    assert r["c"] == 0
