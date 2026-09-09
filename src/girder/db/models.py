"""Typed enums and row models mirroring the SQLite schema (impl-plan §4).

Enums double as the FSM state vocabularies (§5); their string values are the
CHECK-constraint literals in migrations/001-007.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class AutonomyTier(StrEnum):
    T0_SUPERVISED = "0"
    T1_AUTO_MERGE_NOTIFY = "1"
    T2_FULL_AUTONOMY = "2"


class RunStatus(StrEnum):
    DRAFT = "draft"
    SPEC_PENDING = "spec_pending"
    SPEC_APPROVED = "spec_approved"
    BASELINE_RUNNING = "baseline_running"
    ACTIVE = "active"
    AWAITING_AMENDMENT = "awaiting_amendment"
    PR_OPEN = "pr_open"
    CI_RUNNING = "ci_running"
    CI_FIXING = "ci_fixing"
    CONFORMANCE_REVIEW = "conformance_review"
    MERGE_PENDING_HUMAN = "merge_pending_human"
    MERGED = "merged"
    FAILED = "failed"
    ABORTED = "aborted"
    BUDGET_EXHAUSTED = "budget_exhausted"
    ESCALATED = "escalated"


class TaskStatus(StrEnum):
    PENDING = "pending"
    SCHEDULED = "scheduled"
    RUNNING = "running"
    VERIFYING = "verifying"
    VERIFY_PASSED = "verify_passed"
    RETRY_SCHEDULED = "retry_scheduled"
    AWAITING_AMENDMENT = "awaiting_amendment"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    FORCE_PASSED = "force_passed"
    DROPPED = "dropped"


class TaskType(StrEnum):
    CODE_CHANGE = "code_change"
    TEST_CHANGE = "test_change"
    FIX = "fix"
    REFACTOR = "refactor"
    DOCUMENTATION = "documentation"


class AttemptStatus(StrEnum):
    INITIALIZED = "initialized"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CRASHED = "crashed"
    BUDGET_FROZEN = "budget_frozen"
    AMENDMENT_REQUESTED = "amendment_requested"
    INTEGRITY_VIOLATION = "integrity_violation"


class WorktreeState(StrEnum):
    ACTIVE = "active"
    PRUNED = "pruned"
    QUARANTINED = "quarantined"


class IntegrityKind(StrEnum):
    TEST_PATH_MODIFIED = "test_path_modified"
    CONTENT_HASH_MISMATCH = "content_hash_mismatch"
    OUT_OF_SCOPE_WRITE = "out_of_scope_write"
    PROTECTED_READ = "protected_read"
    SCOPE_VIOLATION = "scope_violation"


class SteeringKind(StrEnum):
    PAUSE = "pause"
    RESUME = "resume"
    ABORT = "abort"
    INJECT = "inject"
    SKIP = "skip"
    FORCE_PASS = "force_pass"


# Terminal-state sets used by recovery and the GC daemon.

TERMINAL_RUN_STATUSES = frozenset(
    {
        RunStatus.MERGED,
        RunStatus.FAILED,
        RunStatus.ABORTED,
    }
)

TERMINAL_TASK_STATUSES = frozenset(
    {
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.SKIPPED,
        TaskStatus.FORCE_PASSED,
        TaskStatus.DROPPED,
    }
)

TERMINAL_ATTEMPT_STATUSES = frozenset(
    {
        AttemptStatus.SUCCEEDED,
        AttemptStatus.FAILED,
        AttemptStatus.TIMEOUT,
        AttemptStatus.CRASHED,
        AttemptStatus.BUDGET_FROZEN,
        AttemptStatus.AMENDMENT_REQUESTED,
        AttemptStatus.INTEGRITY_VIOLATION,
    }
)


@dataclass
class Project:
    id: str
    name: str
    repo_path: str
    autonomy_tier: int = 0
    clean_merge_streak: int = 0
    config_json: str = "{}"


@dataclass
class Run:
    id: str
    project_id: str
    intent: str
    branch: str
    status: RunStatus
    spec_hash: str | None = None
    budget_cap_usd: float = 5.0
    spend_usd: float = 0.0
    projected_spend_usd: float = 0.0
    baseline_run_id: str | None = None
    pr_number: int | None = None
    proposal_md: str | None = None
    integrity_violations: int = 0
    # Sprint 6 steering: pump-level suspend flag (migration 010). The run
    # stays `active`; a paused run is pumped only to observe resume/abort.
    paused: bool = False


@dataclass
class Wave:
    id: str
    run_id: str
    sequence_order: int
    status: str = "pending"


@dataclass
class Task:
    id: str
    wave_id: str
    seq: int
    title: str
    task_type: TaskType
    status: TaskStatus
    scope_globs: list[str] = field(default_factory=list)
    spec_slice_md: str = ""
    test_content_hash: str | None = None
    attempts_used: int = 0
    depends_on: list[str] = field(default_factory=list)


@dataclass
class Attempt:
    id: str
    task_id: str
    attempt_num: int
    base_commit: str
    status: AttemptStatus
    exit_code: int | None = None
    turns_used: int = 0
    worktree_path: str | None = None
    container_id: str | None = None
    failure_reason: str | None = None
    started_at: str | None = None
    ended_at: str | None = None


@dataclass
class Worktree:
    id: str
    attempt_id: str
    path: str
    branch: str
    state: WorktreeState = WorktreeState.ACTIVE
    created_at: str | None = None
    removed_at: str | None = None


@dataclass
class AttemptDiff:
    """One persisted attempt diff (migration 012, §10 diff viewer).

    ``diff_redacted`` is stored as given — callers redact before persisting.
    """

    attempt_id: str
    run_id: str
    base_commit: str
    head_commit: str
    diff_redacted: str
    created_at: str
    task_id: str | None = None


@dataclass
class AttemptPrompt:
    """One per-turn prompt snapshot (migration 011, plan.md Phase 5 task 5).

    ``content_redacted`` is stored as given — callers redact before persisting.
    """

    attempt_id: str
    turn: int
    role: str
    content_redacted: str
    ts: str
    run_id: str | None = None
