"""Repository functions per aggregate — the only modules touching SQL are
``girder.db`` and ``girder.fsm`` (impl-plan §6.2).

Status columns are deliberately **not** settable here: every status change
funnels through :mod:`girder.fsm` so transitions are always legal, logged, and
atomic.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from aiosqlite import Row

from girder.db.engine import Database
from girder.db.models import (
    TERMINAL_ATTEMPT_STATUSES,
    TERMINAL_TASK_STATUSES,
    Attempt,
    AttemptDiff,
    AttemptPrompt,
    AttemptStatus,
    Project,
    Run,
    RunStatus,
    Task,
    TaskStatus,
    TaskType,
    Wave,
    Worktree,
    WorktreeState,
)
from girder.util import new_id, utcnow_iso

_TERMINAL_ATTEMPT_SQL = ",".join(f"'{s.value}'" for s in TERMINAL_ATTEMPT_STATUSES)
_TERMINAL_TASK_SQL = ",".join(f"'{s.value}'" for s in TERMINAL_TASK_STATUSES)

# Non-status columns each update_*_fields may write. Status is excluded on
# purpose: it is only mutable through girder.fsm.transition.
_RUN_WRITABLE_FIELDS = frozenset(
    {
        "spec_hash",
        "budget_cap_usd",
        "spend_usd",
        "projected_spend_usd",
        "baseline_run_id",
        "pr_number",
        "proposal_md",
        "branch",
        "integrity_violations",
        "paused",
    }
)
_TASK_WRITABLE_FIELDS = frozenset(
    {
        "test_content_hash",
        "attempts_used",
        "title",
        "scope_globs_json",
        "spec_slice_md",
        "depends_on_json",
        "wave_id",
    }
)
_ATTEMPT_WRITABLE_FIELDS = frozenset(
    {
        "exit_code",
        "turns_used",
        "worktree_path",
        "container_id",
        "failure_reason",
        "started_at",
        "ended_at",
    }
)


def _validate_fields(table: str, fields: dict[str, Any], whitelist: frozenset[str]) -> None:
    for key in fields:
        if key in whitelist:
            continue
        if key == "status":
            raise ValueError(
                f"{table} status changes must go through girder.fsm.transition"
            )
        raise ValueError(f"unknown or non-writable {table} column: {key}")


# --------------------------------------------------------------------- projects


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


# ------------------------------------------------------------------------- runs


async def create_run(
    db: Database, project_id: str, intent: str, branch: str, budget_cap_usd: float
) -> Run:
    run = Run(
        id=new_id(),
        project_id=project_id,
        intent=intent,
        branch=branch,
        status=RunStatus.DRAFT,
        budget_cap_usd=budget_cap_usd,
    )
    async with db.tx() as conn:
        await conn.execute(
            "INSERT INTO runs (id, project_id, intent, branch, status, budget_cap_usd,"
            " spend_usd, projected_spend_usd, integrity_violations, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 0, 0, 0, ?, ?)",
            (
                run.id,
                project_id,
                intent,
                branch,
                run.status.value,
                budget_cap_usd,
                utcnow_iso(),
                utcnow_iso(),
            ),
        )
    return run


def _row_to_run(r: Row) -> Run:
    return Run(
        id=r["id"],
        project_id=r["project_id"],
        intent=r["intent"],
        branch=r["branch"],
        status=RunStatus(r["status"]),
        spec_hash=r["spec_hash"],
        budget_cap_usd=r["budget_cap_usd"],
        spend_usd=r["spend_usd"],
        projected_spend_usd=r["projected_spend_usd"],
        baseline_run_id=r["baseline_run_id"],
        pr_number=r["pr_number"],
        proposal_md=r["proposal_md"],
        integrity_violations=r["integrity_violations"],
        paused=bool(r["paused"]),
    )


async def get_run(db: Database, run_id: str) -> Run | None:
    r = await db.fetchone("SELECT * FROM runs WHERE id = ?", (run_id,))
    return _row_to_run(r) if r else None


async def update_run_fields(
    db: Database, run_id: str, *, updated_at: str | None = None, **fields: Any
) -> None:
    if not fields:
        return
    _validate_fields("runs", fields, _RUN_WRITABLE_FIELDS)
    cols = ", ".join(f"{k} = ?" for k in fields)
    await db.execute(
        f"UPDATE runs SET {cols}, updated_at = ? WHERE id = ?",
        (*fields.values(), updated_at or utcnow_iso(), run_id),
    )


async def add_spend(db: Database, run_id: str, actual_delta: float, projected_total: float) -> None:
    """Reconcile spend: add actual cost, overwrite projected pre-flight figure."""
    async with db.tx() as conn:
        await conn.execute(
            "UPDATE runs SET spend_usd = spend_usd + ?, projected_spend_usd = ?,"
            " updated_at = ? WHERE id = ?",
            (actual_delta, projected_total, utcnow_iso(), run_id),
        )


# ------------------------------------------------------------------ token usage


async def insert_token_usage(
    db: Database,
    *,
    run_id: str | None,
    attempt_id: str | None,
    model_role: str,
    model_id: str,
    estimated_before_call: float,
) -> int:
    """Pre-dispatch estimate row (impl-plan §6.8 step 2): state is persisted
    before the model call goes out; actuals overwrite it in reconcile."""
    cur = await db.execute(
        "INSERT INTO token_usage (attempt_id, run_id, model_role, model_id,"
        " prompt_tokens, completion_tokens, cost_usd, estimated_before_call, created_at)"
        " VALUES (?, ?, ?, ?, 0, 0, 0.0, ?, ?)",
        (attempt_id, run_id, model_role, model_id, estimated_before_call, utcnow_iso()),
    )
    return int(cur.lastrowid or 0)


async def update_token_usage_actual(
    db: Database,
    usage_id: int,
    *,
    prompt_tokens: int,
    completion_tokens: int,
    cost_usd: float,
) -> None:
    """Reconcile a pre-dispatch estimate row with the call's actual usage."""
    await db.execute(
        "UPDATE token_usage SET prompt_tokens = ?, completion_tokens = ?, cost_usd = ?"
        " WHERE id = ?",
        (prompt_tokens, completion_tokens, cost_usd, usage_id),
    )


async def list_token_usage_for_run(db: Database, run_id: str) -> list[dict[str, Any]]:
    rows = await db.fetchall(
        "SELECT id, run_id, attempt_id, model_role, model_id, prompt_tokens,"
        " completion_tokens, cost_usd, estimated_before_call, created_at"
        " FROM token_usage WHERE run_id = ? ORDER BY id",
        (run_id,),
    )
    return [dict(r) for r in rows]


# ------------------------------------------------------------------------ waves


async def create_wave(db: Database, run_id: str, sequence_order: int) -> Wave:
    wave = Wave(id=new_id(), run_id=run_id, sequence_order=sequence_order)
    async with db.tx() as conn:
        await conn.execute(
            "INSERT INTO waves (id, run_id, sequence_order, status) VALUES (?, ?, ?, 'pending')",
            (wave.id, run_id, sequence_order),
        )
    return wave


async def get_or_create_wave0(db: Database, run_id: str) -> Wave:
    r = await db.fetchone(
        "SELECT * FROM waves WHERE run_id = ? ORDER BY sequence_order LIMIT 1", (run_id,)
    )
    if r:
        return Wave(
            id=r["id"], run_id=r["run_id"], sequence_order=r["sequence_order"], status=r["status"]
        )
    return await create_wave(db, run_id, 0)


def _row_to_wave(r: Row) -> Wave:
    return Wave(
        id=r["id"], run_id=r["run_id"], sequence_order=r["sequence_order"], status=r["status"]
    )


async def get_or_create_wave(db: Database, run_id: str, sequence_order: int) -> Wave:
    """Fetch the wave at (run_id, sequence_order), creating it if missing.

    Concurrent creators race on the UNIQUE(run_id, sequence_order) constraint;
    the loser re-selects instead of crashing.
    """
    r = await db.fetchone(
        "SELECT * FROM waves WHERE run_id = ? AND sequence_order = ?",
        (run_id, sequence_order),
    )
    if r:
        return _row_to_wave(r)
    wave = Wave(id=new_id(), run_id=run_id, sequence_order=sequence_order)
    try:
        async with db.tx() as conn:
            await conn.execute(
                "INSERT INTO waves (id, run_id, sequence_order, status)"
                " VALUES (?, ?, ?, 'pending')",
                (wave.id, run_id, sequence_order),
            )
    except sqlite3.IntegrityError:
        existing = await db.fetchone(
            "SELECT * FROM waves WHERE run_id = ? AND sequence_order = ?",
            (run_id, sequence_order),
        )
        assert existing is not None  # the constraint winner's row
        return _row_to_wave(existing)
    return wave


async def list_waves_for_run(db: Database, run_id: str) -> list[Wave]:
    rows = await db.fetchall(
        "SELECT * FROM waves WHERE run_id = ? ORDER BY sequence_order", (run_id,)
    )
    return [_row_to_wave(r) for r in rows]


async def set_wave_status(db: Database, wave_id: str, status: str) -> None:
    """Direct status write (waves intentionally have no FSM entity); callers
    write their own audit events."""
    await db.execute("UPDATE waves SET status = ? WHERE id = ?", (status, wave_id))


async def list_tasks_for_wave(db: Database, wave_id: str) -> list[Task]:
    rows = await db.fetchall(
        "SELECT * FROM tasks WHERE wave_id = ? ORDER BY seq", (wave_id,)
    )
    return [_row_to_task(r) for r in rows]


# ------------------------------------------------------------------------ tasks


async def create_task(
    db: Database,
    wave_id: str,
    seq: int,
    title: str,
    task_type: TaskType,
    *,
    scope_globs: list[str] | None = None,
    spec_slice_md: str = "",
    depends_on: list[str] | None = None,
) -> Task:
    task = Task(
        id=new_id(),
        wave_id=wave_id,
        seq=seq,
        title=title,
        task_type=task_type,
        status=TaskStatus.PENDING,
        scope_globs=scope_globs or [],
        spec_slice_md=spec_slice_md,
        depends_on=depends_on or [],
    )
    async with db.tx() as conn:
        await conn.execute(
            "INSERT INTO tasks (id, wave_id, seq, title, task_type, scope_globs_json,"
            " spec_slice_md, status, attempts_used, depends_on_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?)",
            (
                task.id,
                wave_id,
                seq,
                title,
                task_type.value,
                json.dumps(task.scope_globs),
                spec_slice_md,
                json.dumps(task.depends_on),
            ),
        )
    return task


def _row_to_task(r: Row) -> Task:
    return Task(
        id=r["id"],
        wave_id=r["wave_id"],
        seq=r["seq"],
        title=r["title"],
        task_type=TaskType(r["task_type"]),
        status=TaskStatus(r["status"]),
        scope_globs=json.loads(r["scope_globs_json"]),
        spec_slice_md=r["spec_slice_md"],
        test_content_hash=r["test_content_hash"],
        attempts_used=r["attempts_used"],
        depends_on=json.loads(r["depends_on_json"]),
    )


async def get_task(db: Database, task_id: str) -> Task | None:
    r = await db.fetchone("SELECT * FROM tasks WHERE id = ?", (task_id,))
    return _row_to_task(r) if r else None


async def list_tasks_for_run(db: Database, run_id: str) -> list[Task]:
    rows = await db.fetchall(
        "SELECT t.* FROM tasks t JOIN waves w ON t.wave_id = w.id"
        " WHERE w.run_id = ? ORDER BY w.sequence_order, t.seq",
        (run_id,),
    )
    return [_row_to_task(r) for r in rows]


async def update_task_fields(db: Database, task_id: str, **fields: Any) -> None:
    if not fields:
        return
    _validate_fields("tasks", fields, _TASK_WRITABLE_FIELDS)
    cols = ", ".join(f"{k} = ?" for k in fields)
    await db.execute(f"UPDATE tasks SET {cols} WHERE id = ?", (*fields.values(), task_id))


# --------------------------------------------------------------------- attempts


async def create_attempt(db: Database, task_id: str, base_commit: str) -> Attempt:
    """Create the next attempt for a task and bump attempts_used in one tx."""
    attempt_id = new_id()
    async with db.tx() as conn:
        async with conn.execute("SELECT attempts_used FROM tasks WHERE id = ?", (task_id,)) as cur:
            row = await cur.fetchone()
        if row is None:
            raise KeyError(f"task {task_id} not found")
        attempt_num = int(row["attempts_used"]) + 1
        await conn.execute(
            "UPDATE tasks SET attempts_used = ? WHERE id = ?", (attempt_num, task_id)
        )
        await conn.execute(
            "INSERT INTO attempts (id, task_id, attempt_num, base_commit, status)"
            " VALUES (?, ?, ?, ?, 'initialized')",
            (attempt_id, task_id, attempt_num, base_commit),
        )
    return Attempt(
        id=attempt_id,
        task_id=task_id,
        attempt_num=attempt_num,
        base_commit=base_commit,
        status=AttemptStatus.INITIALIZED,
    )


def _row_to_attempt(r: Row) -> Attempt:
    return Attempt(
        id=r["id"],
        task_id=r["task_id"],
        attempt_num=r["attempt_num"],
        base_commit=r["base_commit"],
        status=AttemptStatus(r["status"]),
        exit_code=r["exit_code"],
        turns_used=r["turns_used"],
        worktree_path=r["worktree_path"],
        container_id=r["container_id"],
        failure_reason=r["failure_reason"],
        started_at=r["started_at"],
        ended_at=r["ended_at"],
    )


async def get_attempt(db: Database, attempt_id: str) -> Attempt | None:
    r = await db.fetchone("SELECT * FROM attempts WHERE id = ?", (attempt_id,))
    return _row_to_attempt(r) if r else None


async def update_attempt_fields(db: Database, attempt_id: str, **fields: Any) -> None:
    if not fields:
        return
    _validate_fields("attempts", fields, _ATTEMPT_WRITABLE_FIELDS)
    cols = ", ".join(f"{k} = ?" for k in fields)
    await db.execute(f"UPDATE attempts SET {cols} WHERE id = ?", (*fields.values(), attempt_id))


async def list_attempts_for_task(db: Database, task_id: str) -> list[Attempt]:
    rows = await db.fetchall(
        "SELECT * FROM attempts WHERE task_id = ? ORDER BY attempt_num", (task_id,)
    )
    return [_row_to_attempt(r) for r in rows]


async def find_attempts_in_status(db: Database, status: AttemptStatus) -> list[Attempt]:
    rows = await db.fetchall("SELECT * FROM attempts WHERE status = ?", (status.value,))
    return [_row_to_attempt(r) for r in rows]


async def find_tasks_in_statuses(db: Database, statuses: list[str]) -> list[Task]:
    """Tasks whose status is in *statuses* (as raw strings, e.g. from TaskStatus)."""
    placeholders = ",".join("?" for _ in statuses)
    rows = await db.fetchall(
        f"SELECT * FROM tasks WHERE status IN ({placeholders})", tuple(statuses)
    )
    return [_row_to_task(r) for r in rows]


# -------------------------------------------------------------------- worktrees


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


# ------------------------------------------- events / redaction / notifications


async def insert_event(
    db: Database,
    event_type: str,
    payload: dict[str, Any],
    *,
    run_id: str | None = None,
    attempt_id: str | None = None,
) -> None:
    await db.execute(
        "INSERT INTO agent_events (ts, event_type, run_id, attempt_id, payload_json)"
        " VALUES (?, ?, ?, ?, ?)",
        (utcnow_iso(), event_type, run_id, attempt_id, json.dumps(payload)),
    )


async def insert_redaction(
    db: Database, source_field: str, pattern_matched: str, *, attempt_id: str | None = None
) -> None:
    """Record a redaction event for audit — never the redacted value (§8.6)."""
    await db.execute(
        "INSERT INTO redaction_log (attempt_id, source_field, pattern_matched, ts)"
        " VALUES (?, ?, ?, ?)",
        (attempt_id, source_field, pattern_matched, utcnow_iso()),
    )


async def insert_notification(
    db: Database,
    channel: str,
    payload_redacted: str,
    status: str,
    *,
    run_id: str | None = None,
) -> None:
    await db.execute(
        "INSERT INTO notifications_log (channel, payload_redacted, status, run_id, ts)"
        " VALUES (?, ?, ?, ?, ?)",
        (channel, payload_redacted, status, run_id, utcnow_iso()),
    )


async def insert_integrity_violation(
    db: Database,
    run_id: str,
    kind: str,
    detail: dict[str, Any],
    *,
    task_id: str | None = None,
    attempt_id: str | None = None,
) -> None:
    """Record an integrity violation and bump the run's counter.

    Integrity violations are a distinct class from ordinary test failures
    (impl-plan §4.3): they block merge at every autonomy tier and reset the
    project's clean-merge streak.
    """
    async with db.tx() as conn:
        await conn.execute(
            "INSERT INTO integrity_violations (run_id, task_id, attempt_id, kind,"
            " detail_json, ts) VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, task_id, attempt_id, kind, json.dumps(detail), utcnow_iso()),
        )
        await conn.execute(
            "UPDATE runs SET integrity_violations = integrity_violations + 1,"
            " updated_at = ? WHERE id = ?",
            (utcnow_iso(), run_id),
        )
        await conn.execute(
            "UPDATE projects SET clean_merge_streak = 0, updated_at = ? WHERE id ="
            " (SELECT project_id FROM runs WHERE id = ?)",
            (utcnow_iso(), run_id),
        )


# ------------------------------------------------------- web console (§10)


async def list_runs_for_project(db: Database, project_id: str) -> list[Run]:
    rows = await db.fetchall(
        "SELECT * FROM runs WHERE project_id = ? ORDER BY created_at DESC", (project_id,)
    )
    return [_row_to_run(r) for r in rows]


async def get_latest_event(
    db: Database, run_id: str, event_type: str
) -> dict[str, Any] | None:
    """Most recent event of *event_type* for a run, as {payload, ts}."""
    r = await db.fetchone(
        "SELECT payload_json, ts FROM agent_events WHERE run_id = ? AND event_type = ?"
        " ORDER BY id DESC LIMIT 1",
        (run_id, event_type),
    )
    if r is None:
        return None
    return {"payload": json.loads(r["payload_json"]), "ts": r["ts"]}


async def get_first_transition_ts_to(
    db: Database, run_id: str, to_state: str
) -> str | None:
    """Timestamp of the FIRST ``state_transition`` event that moved the run
    into *to_state* (TEXT ISO, as stored), or ``None`` if it never did.

    Used for the CI poll deadline (impl-plan §6.11): the clock starts when the
    run first entered ``ci_running`` and is deliberately NOT reset by later
    ``ci_fixing → ci_running`` re-entries — the failure mode being guarded
    against is "CI stuck pending forever", not "fix cycles take long".
    """
    r = await db.fetchone(
        "SELECT ts FROM agent_events WHERE run_id = ? AND event_type = 'state_transition'"
        " AND json_extract(payload_json, '$.to') = ?"
        " ORDER BY id ASC LIMIT 1",
        (run_id, to_state),
    )
    return None if r is None else str(r["ts"])


# ------------------------------------------------- sprint 3 aggregate row types
# NOTE: existing aggregates keep their dataclasses in girder.db.models; these
# five live here because models.py is owned by another workstream this sprint.


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


@dataclass
class SpecAmendment:
    id: str
    run_id: str
    task_id: str | None
    reason: str
    suggested_change: str
    status: str = "pending"
    guidance: str | None = None
    new_spec_hash: str | None = None
    resolved_at: str | None = None


# ---------------------------------------------------------------- baseline runs


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


def _row_to_baseline_run(r: Row) -> BaselineRun:
    return BaselineRun(
        id=r["id"],
        project_id=r["project_id"],
        commit_sha=r["commit_sha"],
        created_at=r["created_at"],
        per_test_json=r["per_test_json"],
    )


async def get_baseline_run(db: Database, baseline_run_id: str) -> BaselineRun | None:
    r = await db.fetchone("SELECT * FROM baseline_runs WHERE id = ?", (baseline_run_id,))
    return _row_to_baseline_run(r) if r else None


# ------------------------------------------------------------------ flaky tests


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


# -------------------------------------------------------------- spec amendments


async def create_spec_amendment(
    db: Database, run_id: str, reason: str, suggested_change: str, *,
    task_id: str | None = None,
) -> SpecAmendment:
    amendment = SpecAmendment(
        id=new_id(), run_id=run_id, task_id=task_id, reason=reason,
        suggested_change=suggested_change,
    )
    async with db.tx() as conn:
        await conn.execute(
            "INSERT INTO spec_amendments (id, run_id, task_id, reason, suggested_change, status)"
            " VALUES (?, ?, ?, ?, ?, 'pending')",
            (amendment.id, run_id, task_id, reason, suggested_change),
        )
    return amendment


def _row_to_spec_amendment(r: Row) -> SpecAmendment:
    return SpecAmendment(
        id=r["id"],
        run_id=r["run_id"],
        task_id=r["task_id"],
        reason=r["reason"],
        suggested_change=r["suggested_change"],
        status=r["status"],
        guidance=r["guidance"],
        new_spec_hash=r["new_spec_hash"],
        resolved_at=r["resolved_at"],
    )


async def get_spec_amendment(db: Database, amendment_id: str) -> SpecAmendment | None:
    r = await db.fetchone("SELECT * FROM spec_amendments WHERE id = ?", (amendment_id,))
    return _row_to_spec_amendment(r) if r else None


async def list_amendments_for_run(db: Database, run_id: str) -> list[SpecAmendment]:
    rows = await db.fetchall(
        "SELECT * FROM spec_amendments WHERE run_id = ? ORDER BY rowid", (run_id,)
    )
    return [_row_to_spec_amendment(r) for r in rows]


async def get_pending_amendment(db: Database, run_id: str) -> SpecAmendment | None:
    """Newest still-pending amendment for a run, if any."""
    r = await db.fetchone(
        "SELECT * FROM spec_amendments WHERE run_id = ? AND status = 'pending'"
        " ORDER BY rowid DESC LIMIT 1",
        (run_id,),
    )
    return _row_to_spec_amendment(r) if r else None


async def list_pending_amendments(db: Database) -> list[SpecAmendment]:
    """All still-pending amendments across every run, oldest first (§10
    Amendments inbox page)."""
    rows = await db.fetchall(
        "SELECT * FROM spec_amendments WHERE status = 'pending' ORDER BY rowid"
    )
    return [_row_to_spec_amendment(r) for r in rows]


async def get_rejected_guidance(db: Database, run_id: str, task_id: str) -> str | None:
    """Guidance from the most recent rejected amendment for this run+task
    (falling back to a run-level amendment with ``task_id IS NULL``).

    Returns None when no rejected amendment carried guidance. Used by the
    TaskEngine to relay user-authored rejection guidance into the resumed
    attempt's trusted steering (impl-plan §6.9)."""
    r = await db.fetchone(
        "SELECT guidance FROM spec_amendments"
        " WHERE run_id = ? AND status = 'rejected' AND guidance IS NOT NULL"
        " AND (task_id = ? OR task_id IS NULL)"
        " ORDER BY (task_id = ?) DESC, rowid DESC LIMIT 1",
        (run_id, task_id, task_id),
    )
    return r["guidance"] if r else None


_RESOLVED_AMENDMENT_STATUSES = frozenset({"approved", "rejected", "aborted"})


async def resolve_spec_amendment(
    db: Database,
    amendment_id: str,
    *,
    status: str,
    guidance: str | None = None,
    new_spec_hash: str | None = None,
) -> None:
    """Resolve a pending amendment. spec_amendments.status is a plain CHECK
    column (not fsm-guarded), so this is the sole writer of its terminal states."""
    if status not in _RESOLVED_AMENDMENT_STATUSES:
        raise ValueError(
            f"amendment status must be one of {sorted(_RESOLVED_AMENDMENT_STATUSES)},"
            f" got {status!r}"
        )
    async with db.tx() as conn:
        await conn.execute(
            "UPDATE spec_amendments SET status = ?, guidance = ?, new_spec_hash = ?,"
            " resolved_at = ? WHERE id = ?",
            (status, guidance, new_spec_hash, utcnow_iso(), amendment_id),
        )


# ------------------------------------------------------------------ tool calls


async def insert_tool_call(
    db: Database,
    *,
    attempt_id: str,
    tool_name: str,
    input_json: str,
    output_redacted: str | None = None,
    duration_ms: int | None = None,
    scope_violation: bool = False,
    held: bool = False,
    verdict: str | None = None,
) -> int:
    """Insert one tool-call audit row. ``verdict`` (migration 011) is the
    ScopeGuard decision — "allow", "allow_logged" or "violation" — persisted
    for post-mortem auditability."""
    cur = await db.execute(
        "INSERT INTO tool_calls (attempt_id, ts, tool_name, input_json,"
        " output_blob_redacted, duration_ms, scope_violation, held, verdict)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            attempt_id,
            utcnow_iso(),
            tool_name,
            input_json,
            output_redacted,
            duration_ms,
            int(scope_violation),
            int(held),
            verdict,
        ),
    )
    return int(cur.lastrowid or 0)


async def list_tool_calls_for_attempt(db: Database, attempt_id: str) -> list[dict[str, Any]]:
    rows = await db.fetchall(
        "SELECT id, attempt_id, ts, tool_name, input_json, output_blob_redacted,"
        " duration_ms, scope_violation, held, verdict"
        " FROM tool_calls WHERE attempt_id = ? ORDER BY id",
        (attempt_id,),
    )
    return [dict(r) for r in rows]


# -------------------------------------------------------------- attempt prompts


async def record_attempt_prompt(
    db: Database,
    attempt_id: str,
    turn: int,
    role: str,
    content: str,
    run_id: str | None = None,
) -> None:
    """Persist a per-turn prompt snapshot for the post-mortem explorer
    (plan.md Phase 5 task 5).

    ``content`` is stored exactly as given in ``attempt_prompts.content_redacted``
    — the CALLER is responsible for redacting secrets before calling this
    (the column name documents the expectation, enforcement lives upstream).
    """
    await db.execute(
        "INSERT INTO attempt_prompts (attempt_id, run_id, turn, role,"
        " content_redacted, ts) VALUES (?, ?, ?, ?, ?, ?)",
        (attempt_id, run_id, turn, role, content, utcnow_iso()),
    )


async def list_attempt_prompts(db: Database, attempt_id: str) -> list[AttemptPrompt]:
    """Return one attempt's prompt snapshots ordered by (turn, id)."""
    rows = await db.fetchall(
        "SELECT attempt_id, run_id, turn, role, content_redacted, ts"
        " FROM attempt_prompts WHERE attempt_id = ? ORDER BY turn, id",
        (attempt_id,),
    )
    return [
        AttemptPrompt(
            attempt_id=r["attempt_id"],
            run_id=r["run_id"],
            turn=int(r["turn"]),
            role=r["role"],
            content_redacted=r["content_redacted"],
            ts=r["ts"],
        )
        for r in rows
    ]


# --------------------------------------------------------------- attempt diffs


async def insert_attempt_diff(
    db: Database,
    *,
    attempt_id: str,
    run_id: str,
    base_commit: str,
    head_commit: str,
    diff_redacted: str,
    task_id: str | None = None,
) -> int:
    """Persist one attempt diff (migration 012, §10 diff viewer / plan.md §2.1).

    ``diff_redacted`` is stored exactly as given — the CALLER is responsible
    for redacting secrets before calling this (same contract as
    ``record_attempt_prompt``; enforcement lives upstream via the run's
    Redactor, typically ``redact_and_log(..., source_field="attempt_diff")``).
    """
    cur = await db.execute(
        "INSERT INTO attempt_diffs (attempt_id, task_id, run_id, base_commit,"
        " head_commit, diff_redacted, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (attempt_id, task_id, run_id, base_commit, head_commit, diff_redacted, utcnow_iso()),
    )
    return int(cur.lastrowid or 0)


def _row_to_attempt_diff(r: Row) -> AttemptDiff:
    return AttemptDiff(
        attempt_id=r["attempt_id"],
        task_id=r["task_id"],
        run_id=r["run_id"],
        base_commit=r["base_commit"],
        head_commit=r["head_commit"],
        diff_redacted=r["diff_redacted"],
        created_at=r["created_at"],
    )


async def list_attempt_diffs_for_run(db: Database, run_id: str) -> list[AttemptDiff]:
    """All persisted diffs of a run, oldest first."""
    rows = await db.fetchall(
        "SELECT attempt_id, task_id, run_id, base_commit, head_commit, diff_redacted,"
        " created_at FROM attempt_diffs WHERE run_id = ? ORDER BY id",
        (run_id,),
    )
    return [_row_to_attempt_diff(r) for r in rows]


async def latest_attempt_diff_for_task(db: Database, task_id: str) -> AttemptDiff | None:
    """Newest diff across a task's attempts (the current state of its branch)."""
    r = await db.fetchone(
        "SELECT attempt_id, task_id, run_id, base_commit, head_commit, diff_redacted,"
        " created_at FROM attempt_diffs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
        (task_id,),
    )
    return _row_to_attempt_diff(r) if r else None


# -------------------------------------------------------------- steering events


async def insert_steering_event(
    db: Database, run_id: str, kind: str, payload: dict[str, Any]
) -> int:
    cur = await db.execute(
        "INSERT INTO steering_events (run_id, kind, payload_json, created_at)"
        " VALUES (?, ?, ?, ?)",
        (run_id, kind, json.dumps(payload), utcnow_iso()),
    )
    return int(cur.lastrowid or 0)


async def consume_steering_events(
    db: Database, run_id: str, *, kinds: list[str] | None = None
) -> list[dict[str, Any]]:
    """Return unconsumed events (oldest first, optional kind filter) and mark
    them consumed in one transaction — each event is delivered exactly once."""
    async with db.tx() as conn:
        sql = "SELECT * FROM steering_events WHERE run_id = ? AND consumed_at IS NULL"
        params: list[Any] = [run_id]
        if kinds is not None:
            sql += f" AND kind IN ({','.join('?' for _ in kinds)})"
            params.extend(kinds)
        sql += " ORDER BY id"
        async with conn.execute(sql, tuple(params)) as cur:
            rows = await cur.fetchall()
        for r in rows:
            await conn.execute(
                "UPDATE steering_events SET consumed_at = ? WHERE id = ?",
                (utcnow_iso(), r["id"]),
            )
    return [
        {
            "id": r["id"],
            "run_id": r["run_id"],
            "kind": r["kind"],
            "payload": json.loads(r["payload_json"]),
            "created_at": r["created_at"],
        }
        for r in rows
    ]


# --------------------------------------------- CI checks & merge (Sprint 4)


async def insert_ci_check_result(
    db: Database,
    run_id: str,
    check_name: str,
    status: str,
    *,
    conclusion: str | None = None,
    url: str | None = None,
    log_excerpt_redacted: str | None = None,
) -> int:
    """Persist one observed CI check (impl-plan §6.11: redacted excerpts only)."""
    async with db.tx() as conn:
        cur = await conn.execute(
            "INSERT INTO ci_check_results (run_id, check_name, status, conclusion, url,"
            " log_excerpt_redacted, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, check_name, status, conclusion, url, log_excerpt_redacted, utcnow_iso()),
        )
        lastrowid: Any = cur.lastrowid
        row_id = int(lastrowid)
    return row_id


async def list_ci_check_results_for_run(db: Database, run_id: str) -> list[dict[str, Any]]:
    rows = await db.fetchall(
        "SELECT * FROM ci_check_results WHERE run_id = ? ORDER BY id", (run_id,)
    )
    return [dict(r) for r in rows]


async def bump_clean_merge_streak(db: Database, project_id: str) -> int:
    """Increment and return the project's clean-merge streak (§2.3 T2 gate).

    Zeroing on integrity violations already happens inside
    :func:`insert_integrity_violation`; escalated/clean bookkeeping lives here.
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
    :func:`insert_integrity_violation`, with no reusable helper.
    """
    await db.execute(
        "UPDATE projects SET clean_merge_streak = 0, updated_at = ? WHERE id = ?",
        (utcnow_iso(), project_id),
    )


async def count_unreviewed_merges(db: Database, project_id: str) -> int:
    """Merged runs of *project* with no ``merge_reviewed`` event (§2.3 T1
    rolling review window: "pause new merges if I haven't reviewed the last N")."""
    row = await db.fetchone(
        "SELECT COUNT(*) AS n FROM runs r WHERE r.project_id = ? AND r.status = 'merged'"
        " AND NOT EXISTS (SELECT 1 FROM agent_events e WHERE e.run_id = r.id"
        " AND e.event_type = 'merge_reviewed')",
        (project_id,),
    )
    n: Any = row["n"] if row else 0
    return int(n)


# ------------------------------------------------------- Sprint 6: console queries


async def list_events_for_run(
    db: Database, run_id: str, *, after_id: int = 0, limit: int = 500
) -> list[dict[str, Any]]:
    """Agent events of *run* ordered by id, tail-pollable via ``after_id`` (§10 SSE)."""
    rows = await db.fetchall(
        "SELECT id, ts, event_type, run_id, attempt_id, payload_json FROM agent_events"
        " WHERE run_id = ? AND id > ? ORDER BY id LIMIT ?",
        (run_id, after_id, limit),
    )
    return [dict(r) for r in rows]


async def sum_token_usage_for_run(db: Database, run_id: str) -> list[dict[str, Any]]:
    """Per-model-role usage totals behind the spend dashboard (§10)."""
    rows = await db.fetchall(
        "SELECT model_role, model_id, COUNT(*) AS calls,"
        " SUM(prompt_tokens) AS prompt_tokens, SUM(completion_tokens) AS completion_tokens,"
        " SUM(cost_usd) AS cost_usd, SUM(estimated_before_call) AS estimated_usd"
        " FROM token_usage WHERE run_id = ? GROUP BY model_role, model_id ORDER BY model_role",
        (run_id,),
    )
    return [dict(r) for r in rows]


async def list_token_usage_for_attempt(db: Database, attempt_id: str) -> list[dict[str, Any]]:
    rows = await db.fetchall(
        "SELECT id, model_role, model_id, prompt_tokens, completion_tokens,"
        " cost_usd, estimated_before_call, created_at FROM token_usage"
        " WHERE attempt_id = ? ORDER BY id",
        (attempt_id,),
    )
    return [dict(r) for r in rows]


async def list_runs_in_status(
    db: Database, status: str, *, project_id: str | None = None
) -> list[Run]:
    sql = "SELECT * FROM runs WHERE status = ?"
    params: list[Any] = [status]
    if project_id is not None:
        sql += " AND project_id = ?"
        params.append(project_id)
    sql += " ORDER BY updated_at"
    rows = await db.fetchall(sql, tuple(params))
    return [_row_to_run(r) for r in rows]


async def list_notifications_for_run(db: Database, run_id: str) -> list[dict[str, Any]]:
    rows = await db.fetchall(
        "SELECT id, channel, payload_redacted, status, run_id, ts FROM notifications_log"
        " WHERE run_id = ? ORDER BY id",
        (run_id,),
    )
    return [dict(r) for r in rows]


async def list_integrity_violations_for_run(db: Database, run_id: str) -> list[dict[str, Any]]:
    rows = await db.fetchall(
        "SELECT id, run_id, task_id, attempt_id, kind, detail_json, ts"
        " FROM integrity_violations WHERE run_id = ? ORDER BY id",
        (run_id,),
    )
    return [dict(r) for r in rows]


async def set_run_paused(db: Database, run_id: str, paused: bool) -> None:
    """Persist the pump-level suspend flag (Phase 5 pause/resume steering)."""
    await update_run_fields(db, run_id, paused=int(paused))


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


async def count_held_tool_calls(db: Database, attempt_id: str) -> int:
    """Count held or scope-violating tool calls for one attempt (impl-plan §8).

    A ``held=1`` row means the registry refused to execute the call; any such
    row taints the whole attempt — completion may never rest on work the
    orchestrator never ran. Used by the verify step to fail the attempt
    without retry before any merge can happen."""
    row = await db.fetchone(
        "SELECT COUNT(*) AS n FROM tool_calls WHERE attempt_id = ?"
        " AND (scope_violation = 1 OR held = 1)",
        (attempt_id,),
    )
    return int(row["n"]) if row is not None else 0
