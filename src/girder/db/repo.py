"""Repository functions per aggregate — the only modules touching SQL are
``girder.db`` and ``girder.fsm`` (impl-plan §6.2).

Status columns are deliberately **not** settable here: every status change
funnels through :mod:`girder.fsm` so transitions are always legal, logged, and
atomic.
"""

from __future__ import annotations

import json
from typing import Any

from aiosqlite import Row

from girder.db.engine import Database
from girder.db.models import (
    TERMINAL_ATTEMPT_STATUSES,
    TERMINAL_TASK_STATUSES,
    Attempt,
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
    await db.conn.commit()


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
    await db.conn.commit()
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
    await db.conn.commit()


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
    await db.conn.commit()


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
    await db.conn.commit()


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
    await db.conn.commit()


async def insert_redaction(
    db: Database, source_field: str, pattern_matched: str, *, attempt_id: str | None = None
) -> None:
    """Record a redaction event for audit — never the redacted value (§8.6)."""
    await db.execute(
        "INSERT INTO redaction_log (attempt_id, source_field, pattern_matched, ts)"
        " VALUES (?, ?, ?, ?)",
        (attempt_id, source_field, pattern_matched, utcnow_iso()),
    )
    await db.conn.commit()


async def insert_notification(
    db: Database, channel: str, payload_redacted: str, status: str
) -> None:
    await db.execute(
        "INSERT INTO notifications_log (channel, payload_redacted, status, ts) VALUES (?, ?, ?, ?)",
        (channel, payload_redacted, status, utcnow_iso()),
    )
    await db.conn.commit()


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
