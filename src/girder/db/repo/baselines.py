"""Baseline runs and known-flaky tests (sprint 3 aggregate row types).

NOTE: these dataclasses live here rather than in girder.db.models because
models.py is owned by another workstream this sprint.
"""

from __future__ import annotations

from dataclasses import dataclass

from girder.db.engine import Database
from girder.util import new_id, utcnow_iso


@dataclass
class BaselineRun:
    id: str
    project_id: str
    commit_sha: str
    created_at: str
    per_test_json: str = "{}"


@dataclass
class FlakyTest:
    project_id: str
    test_id: str
    first_seen_run: str | None = None
    last_seen_run: str | None = None
    status: str = "known_flaky"


async def create_baseline_run(
    db: Database, project_id: str, commit_sha: str, per_test_json: str
) -> BaselineRun:
    baseline = BaselineRun(
        id=new_id(), project_id=project_id, commit_sha=commit_sha,
        created_at=utcnow_iso(), per_test_json=per_test_json,
    )
    async with db.tx() as conn:
        await conn.execute(
            "INSERT INTO baseline_runs (id, project_id, commit_sha, created_at, per_test_json)"
            " VALUES (?, ?, ?, ?, ?)",
            (baseline.id, project_id, commit_sha, baseline.created_at, per_test_json),
        )
    return baseline


async def get_baseline_run(db: Database, baseline_run_id: str) -> BaselineRun | None:
    r = await db.fetchone("SELECT * FROM baseline_runs WHERE id = ?", (baseline_run_id,))
    if r is None:
        return None
    return BaselineRun(
        id=r["id"],
        project_id=r["project_id"],
        commit_sha=r["commit_sha"],
        created_at=r["created_at"],
        per_test_json=r["per_test_json"],
    )


async def upsert_flaky_test(
    db: Database, project_id: str, test_id: str, *, run_id: str, status: str = "known_flaky"
) -> None:
    """Insert or refresh a flaky-test record; first_seen_run is preserved."""
    async with db.tx() as conn:
        await conn.execute(
            "INSERT INTO flaky_tests (project_id, test_id, first_seen_run, last_seen_run, status)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT (project_id, test_id) DO UPDATE SET"
            " last_seen_run = excluded.last_seen_run, status = excluded.status",
            (project_id, test_id, run_id, run_id, status),
        )


async def list_flaky_tests(db: Database, project_id: str) -> list[FlakyTest]:
    rows = await db.fetchall(
        "SELECT * FROM flaky_tests WHERE project_id = ? ORDER BY test_id", (project_id,)
    )
    return [
        FlakyTest(
            project_id=r["project_id"],
            test_id=r["test_id"],
            first_seen_run=r["first_seen_run"],
            last_seen_run=r["last_seen_run"],
            status=r["status"],
        )
        for r in rows
    ]
