"""WP-1.3 — Database engine: migrations, tamper detection, txn semantics."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

import pytest

from girder.db.engine import Database, MigrationError, default_migrations_dir


async def test_fresh_boot_applies_all_migrations(tmp_path: Path) -> None:
    db = await Database.open(tmp_path / "g.db", migrations_dir=default_migrations_dir())
    try:
        rows = await db.fetchall("SELECT version FROM schema_migrations ORDER BY version")
        versions = [r["version"] for r in rows]
        assert versions == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]
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
            "attempt_prompts",
        ):
            r = await db.fetchone(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
            )
            assert r is not None, f"missing table {table}"

        # Sprint 2 columns landed by 008/009 (plan.md Phase 1).
        run_cols = {r["name"] for r in await db.fetchall("PRAGMA table_info(runs)")}
        usage_cols = {r["name"] for r in await db.fetchall("PRAGMA table_info(token_usage)")}
        assert "proposal_md" in run_cols
        assert "run_id" in usage_cols
        assert "attempt_id" in usage_cols
        # Sprint 6 columns landed by 010 (Phase 5 steering + notification trail).
        assert "paused" in run_cols
        # Sprint 6 hardening columns landed by 011.
        tc_cols = {r["name"] for r in await db.fetchall("PRAGMA table_info(tool_calls)")}
        assert "verdict" in tc_cols
        notif_cols = {r["name"] for r in await db.fetchall("PRAGMA table_info(notifications_log)")}
        assert "run_id" in notif_cols
    finally:
        await db.close()


async def test_open_creates_missing_parent_dirs(tmp_path: Path) -> None:
    """First-ever run: the DB's parent directory (e.g. ~/.local/share/girder) may not exist."""
    path = tmp_path / "a" / "b" / "g.db"
    db = await Database.open(path, migrations_dir=default_migrations_dir())
    try:
        applied = await db.fetchall("SELECT version FROM schema_migrations")
        assert [r["version"] for r in applied] == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]
    finally:
        await db.close()
    assert path.is_file()


async def test_memory_db_creates_no_file(tmp_path: Path) -> None:
    db = await Database.open(":memory:", migrations_dir=default_migrations_dir())
    try:
        r = await db.fetchone("SELECT COUNT(*) AS c FROM schema_migrations")
        assert r["c"] == 12
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
        assert len(applied) == 12  # nothing re-applied
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


async def _schema_fingerprint(db: Database, table: str) -> list[tuple[str, str, int, int, Any]]:
    rows = await db.fetchall(f"PRAGMA table_info({table})")
    return [(r["name"], r["type"], r["notnull"], r["pk"], r["dflt_value"]) for r in rows]


async def test_migration_008_retries_after_mid_script_crash(tmp_path: Path) -> None:
    """§4.1: a crash between applying 008's script and recording its version
    (autocommit per statement, so a leftover token_usage_new may exist) must
    still boot: the retry drops the leftover _new table and re-applies."""
    # Reference schema from a clean boot.
    ref_path = tmp_path / "clean.db"
    ref = await Database.open(ref_path, migrations_dir=default_migrations_dir())
    try:
        ref_usage = await _schema_fingerprint(ref, "token_usage")
        ref_runs = await _schema_fingerprint(ref, "runs")
    finally:
        await ref.close()

    # Build a crashed DB: migrations through 007 applied, 008 half-applied —
    # old (pre-008) token_usage still present plus a leftover _new table.
    crash_path = tmp_path / "crash.db"
    shutil.copyfile(ref_path, crash_path)
    crash = await Database.open(crash_path, run_migrations=False)
    try:
        await crash.conn.executescript(
            """
            DROP TABLE token_usage;
            CREATE TABLE token_usage (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              attempt_id TEXT NOT NULL REFERENCES attempts(id),
              model_role TEXT NOT NULL,
              model_id TEXT NOT NULL,
              prompt_tokens INTEGER NOT NULL,
              completion_tokens INTEGER NOT NULL,
              cost_usd REAL NOT NULL,
              estimated_before_call REAL NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE token_usage_new (leftover TEXT);
            DELETE FROM schema_migrations WHERE version >= 8;
            DROP TABLE steering_events;
            ALTER TABLE runs DROP COLUMN proposal_md;
            -- rewind Sprint 6 migration 010 artifacts so the replay re-adds them
            ALTER TABLE runs DROP COLUMN paused;
            ALTER TABLE notifications_log DROP COLUMN run_id;
            -- rewind Sprint 6 hardening migration 011 artifacts
            ALTER TABLE tool_calls DROP COLUMN verdict;
            DROP TABLE attempt_prompts;
            -- rewind migration 012 artifacts so the replay re-creates them
            DROP TABLE attempt_diffs;
            """
        )
    finally:
        await crash.close()

    db = await Database.open(crash_path, migrations_dir=default_migrations_dir())
    try:
        versions = [
            r["version"]
            for r in await db.fetchall("SELECT version FROM schema_migrations ORDER BY version")
        ]
        assert versions == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]
        # final schema matches the clean boot
        assert await _schema_fingerprint(db, "token_usage") == ref_usage
        assert await _schema_fingerprint(db, "runs") == ref_runs
        leftover = await db.fetchone(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='token_usage_new'"
        )
        assert leftover is None
    finally:
        await db.close()


async def test_poisoned_duplicate_column_self_heals(tmp_path: Path) -> None:
    """Regression: the pre-transaction runner applied scripts statement-by-
    statement on an autocommit connection, so a crash between 009's ALTER and
    the version-row insert left runs.proposal_md present with no version row —
    and every later boot died on "duplicate column name". Boot must now
    self-heal (skip the already-applied statement, record the version)."""
    ref_path = tmp_path / "clean.db"
    ref = await Database.open(ref_path, migrations_dir=default_migrations_dir())
    try:
        ref_prompts = await _schema_fingerprint(ref, "attempt_prompts")
    finally:
        await ref.close()

    # Build the poisoned DB: fully migrated, but versions 9-11 forgotten while
    # 009's artifact (runs.proposal_md) survived the "crash".
    crash_path = tmp_path / "crash.db"
    shutil.copyfile(ref_path, crash_path)
    crash = await Database.open(crash_path, run_migrations=False)
    try:
        await crash.conn.executescript(
            """
            DELETE FROM schema_migrations WHERE version >= 9;
            -- 010/011 artifacts rolled back with the "lost" transaction
            ALTER TABLE runs DROP COLUMN paused;
            ALTER TABLE notifications_log DROP COLUMN run_id;
            ALTER TABLE tool_calls DROP COLUMN verdict;
            DROP TABLE attempt_prompts;
            -- migration 012 artifact rolled back with the "lost" transaction
            DROP TABLE attempt_diffs;
            """
        )
    finally:
        await crash.close()

    db = await Database.open(crash_path, migrations_dir=default_migrations_dir())
    try:
        versions = [
            r["version"]
            for r in await db.fetchall("SELECT version FROM schema_migrations ORDER BY version")
        ]
        assert versions == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]
        assert await _schema_fingerprint(db, "attempt_prompts") == ref_prompts
    finally:
        await db.close()


async def test_mid_migration_failure_leaves_no_partial_state(tmp_path: Path) -> None:
    """§4.1: a migration failing mid-script must roll back atomically — no
    tables from its earlier statements, no version row — so a retry starts
    clean instead of tripping over half-applied DDL."""
    migrations = tmp_path / "migrations"
    shutil.copytree(default_migrations_dir(), migrations)
    (migrations / "012_atomicity_probe.sql").write_text(
        "CREATE TABLE probe_should_not_exist (id INTEGER);\n"
        "INSERT INTO no_such_table VALUES (1);\n"
    )

    with pytest.raises(MigrationError, match="012_atomicity_probe"):
        await Database.open(tmp_path / "g.db", migrations_dir=migrations)

    db = await Database.open(tmp_path / "g.db", migrations_dir=migrations, run_migrations=False)
    try:
        probe = await db.fetchone(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='probe_should_not_exist'"
        )
        assert probe is None
        v = await db.fetchone("SELECT MAX(version) AS v FROM schema_migrations")
        assert v["v"] == 11  # the failed migration's version row is absent
    finally:
        await db.close()
