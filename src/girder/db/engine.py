"""SQLite persistence engine: WAL, hash-checked migrations, IMMEDIATE txns.

Guarantees (impl-plan §6.2 / plan.md §5.5):

* ``WAL`` journal + ``BEGIN IMMEDIATE`` write transactions serialized behind a
  single :class:`asyncio.Lock` — exactly one writer at a time, no lost updates.
* Migrations are ordered, hash-recorded SQL files. Editing an already-applied
  migration file changes its hash and **fails the next boot** (history is
  tamper-evident). Each migration is applied inside ONE ``BEGIN IMMEDIATE``
  transaction together with its ``schema_migrations`` version-row insert, so a
  crash mid-migration rolls back atomically (§4.1). Self-heal: a database
  poisoned by the pre-transaction runner (a bare ``ALTER TABLE ... ADD COLUMN``
  applied but the version row lost) is repaired at boot by re-applying the
  script while skipping statements that fail with duplicate-column errors.
* State is committed before the caller proceeds — the ``tx()`` context manager
  is the single write path.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite

from girder.util import utcnow_iso

log = logging.getLogger(__name__)


class MigrationError(RuntimeError):
    """Raised when migration history is inconsistent or a script fails."""


def default_migrations_dir() -> Path:
    """Resolve the migrations directory: env override > repo root via package path."""
    env = os.environ.get("GIRDER_MIGRATIONS_DIR")
    if env:
        return Path(env)
    # src/girder/db/engine.py -> walk up to the directory containing pyproject.toml
    # (editable installs keep the package inside the repo; uv sync default).
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file() and (parent / "migrations").is_dir():
            return parent / "migrations"
    raise MigrationError(
        "migrations directory not found; set GIRDER_MIGRATIONS_DIR or run from the repo"
    )


_VERSION_RE = re.compile(r"^(\d+)_")
_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA foreign_keys=ON",
    "PRAGMA busy_timeout=5000",
    "PRAGMA synchronous=NORMAL",
)


class Database:
    """Async SQLite wrapper. One connection, one writer lock, one tx() path."""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self.conn = conn
        self._write_lock = asyncio.Lock()

    @classmethod
    async def open(
        cls,
        path: str | Path,
        *,
        migrations_dir: Path | None = None,
        run_migrations: bool = True,
    ) -> Database:
        if str(path) != ":memory:":
            # first-run bootstrap: e.g. ~/.local/share/girder/ may not exist yet
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(str(path), isolation_level=None)
        conn.row_factory = aiosqlite.Row
        async with await conn.execute("PRAGMA journal_mode=WAL") as cur:
            await cur.fetchall()  # consume the mode row
        for pragma in _PRAGMAS[1:]:
            await conn.execute(pragma)
        db = cls(conn)
        if run_migrations:
            try:
                await db.migrate(migrations_dir or default_migrations_dir())
            except BaseException:
                # A leaked unclosed connection keeps the aiosqlite worker
                # thread alive, which blocks interpreter shutdown forever —
                # every migration failure would look like a mystery hang.
                await conn.close()
                raise
        return db

    async def close(self) -> None:
        await self.conn.close()

    @asynccontextmanager
    async def tx(self) -> AsyncIterator[aiosqlite.Connection]:
        """Single write path: BEGIN IMMEDIATE … COMMIT/ROLLBACK, serialized.

        Not reentrant — never call ``tx()`` inside ``tx()``.
        """
        async with self._write_lock:
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                await self.conn.execute("ROLLBACK")
                raise
            await self.conn.execute("COMMIT")

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> aiosqlite.Cursor:
        """Ad-hoc single-statement write: execute + commit, serialized with tx().

        The connection runs with ``isolation_level=None`` (autocommit), so the
        statement is its own transaction — but it must hold the same write
        lock as :meth:`tx`, otherwise a concurrent wave task's plain write
        lands inside someone else's open ``BEGIN IMMEDIATE`` and its commit
        would commit a half-finished transaction (Sprint 5: concurrent wave
        engines are the first concurrent users of one Database).
        """
        async with self._write_lock:
            cur = await self.conn.execute(sql, params)
            await self.conn.commit()
            return cur

    async def fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> aiosqlite.Row | None:
        async with self.conn.execute(sql, params) as cur:
            return await cur.fetchone()

    async def fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[aiosqlite.Row]:
        async with self.conn.execute(sql, params) as cur:
            return list(await cur.fetchall())

    # ------------------------------------------------------------------ migrations

    async def migrate(self, migrations_dir: Path) -> list[int]:
        """Apply pending migrations; verify applied hashes. Returns applied versions."""
        await self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
              version    INTEGER PRIMARY KEY,
              sha256     TEXT NOT NULL,
              applied_at TEXT NOT NULL
            )
            """
        )
        files = sorted(
            (f for f in migrations_dir.iterdir() if f.suffix == ".sql"),  # noqa: ASYNC240
            key=lambda f: Database._version_of(f),
        )
        applied_rows = await self.fetchall("SELECT version, sha256 FROM schema_migrations")
        applied = {int(r["version"]): str(r["sha256"]) for r in applied_rows}

        newly_applied: list[int] = []
        for f in files:
            version = Database._version_of(f)
            digest = hashlib.sha256(f.read_bytes()).hexdigest()
            if version in applied:
                if applied[version] != digest:
                    raise MigrationError(
                        f"migration {f.name} was modified after being applied "
                        f"(recorded {applied[version][:12]}, file {digest[:12]}); refusing to boot"
                    )
                continue
            script = f.read_text()
            try:
                await self._apply_migration(version, digest, script)
            except Exception as exc:
                raise MigrationError(f"migration {f.name} failed: {exc}") from exc
            newly_applied.append(version)

        # Files whose versions were recorded but no longer exist on disk.
        known = {Database._version_of(f) for f in files}
        missing = set(applied) - known
        if missing:
            raise MigrationError(f"applied migrations missing from disk: {sorted(missing)}")
        return newly_applied

    async def _apply_migration(self, version: int, digest: str, script: str) -> None:
        """Apply one migration atomically (impl-plan §4.1).

        The script is split into individual statements (``executescript``
        implicitly COMMITs and cannot run inside an explicit transaction) and
        executed inside a single ``BEGIN IMMEDIATE`` together with the
        ``schema_migrations`` version-row insert — a crash mid-script rolls
        back completely, leaving no partial state.

        Self-heal for databases poisoned by the pre-transaction runner (which
        used ``executescript`` on an autocommit connection): if the version
        row is absent but a statement fails with ``duplicate column name``
        (i.e. a partial application survived a crash), roll back and re-apply
        skipping statements that fail with duplicate-column errors, logging
        each skip. Any other error hard-fails boot.
        """
        statements = _split_statements(script)
        skip: set[int] = set()
        while True:
            async with self._write_lock:
                await self.conn.execute("BEGIN IMMEDIATE")
                try:
                    for i, stmt in enumerate(statements):
                        if i in skip:
                            continue
                        try:
                            await self.conn.execute(stmt)
                        except aiosqlite.OperationalError as exc:
                            if "duplicate column name" not in str(exc).lower():
                                raise
                            log.warning(
                                "migration v%d: statement already applied by a crashed"
                                " pre-transaction run; skipping: %s",
                                version,
                                _first_line(stmt),
                            )
                            raise _DuplicateColumn(i) from exc
                    await self.conn.execute(
                        "INSERT INTO schema_migrations (version, sha256, applied_at)"
                        " VALUES (?, ?, ?)",
                        (version, digest, utcnow_iso()),
                    )
                except _DuplicateColumn as exc:
                    await self.conn.execute("ROLLBACK")
                    skip.add(exc.index)
                    continue
                except aiosqlite.Error:
                    await self.conn.execute("ROLLBACK")
                    raise
                await self.conn.execute("COMMIT")
            return

    @staticmethod
    def _version_of(f: Path) -> int:
        m = _VERSION_RE.match(f.name)
        if not m:
            raise MigrationError(f"migration file {f.name} must be named NNN_description.sql")
        return int(m.group(1))


class _DuplicateColumn(Exception):
    """Internal: a statement failed with 'duplicate column name' (self-heal)."""

    def __init__(self, index: int) -> None:
        super().__init__(index)
        self.index = index


def _split_statements(script: str) -> list[str]:
    """Split a SQL script into complete statements.

    ``executescript`` cannot run inside an explicit transaction (it implicitly
    COMMITs), so scripts are applied statement-by-statement instead.
    ``sqlite3.complete_statement`` handles comments and string literals.
    """
    statements: list[str] = []
    buf = ""
    for line in script.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            stmt = buf.strip()
            if stmt:
                statements.append(stmt)
            buf = ""
    tail = buf.strip()
    if tail:
        statements.append(tail)
    return statements


def _first_line(sql: str) -> str:
    """Compact one-line summary of a statement, leading comments stripped."""
    body = "\n".join(
        line for line in sql.splitlines() if not line.lstrip().startswith("--")
    ).strip()
    return " ".join(body.split())[:80]
