"""Repository functions per aggregate — the only modules touching SQL are
``girder.db`` and ``girder.fsm`` (impl-plan §6.2).

Status columns are deliberately **not** settable here: every status change
funnels through :mod:`girder.fsm` so transitions are always legal, logged, and
atomic.

This package re-exports the complete public surface that used to live in the
single ``girder/db/repo.py`` module, so every existing import
(``from girder.db import repo`` … ``repo.create_run(...)``) keeps working
unchanged (resolution R-SP10-1: zero call-site changes).
"""

from girder.db.repo.amendments import (
    SpecAmendment as SpecAmendment,
    create_spec_amendment as create_spec_amendment,
    get_pending_amendment as get_pending_amendment,
    get_rejected_guidance as get_rejected_guidance,
    get_spec_amendment as get_spec_amendment,
    list_amendments_for_run as list_amendments_for_run,
    list_pending_amendments as list_pending_amendments,
    resolve_spec_amendment as resolve_spec_amendment,
    update_spec_amendment_scope_globs as update_spec_amendment_scope_globs,
)
from girder.db.repo.attempts import (
    create_attempt as create_attempt,
    find_attempts_in_status as find_attempts_in_status,
    get_attempt as get_attempt,
    insert_attempt_diff as insert_attempt_diff,
    latest_attempt_diff_for_task as latest_attempt_diff_for_task,
    list_attempt_diffs_for_run as list_attempt_diffs_for_run,
    list_attempt_prompts as list_attempt_prompts,
    list_attempts_for_task as list_attempts_for_task,
    record_attempt_prompt as record_attempt_prompt,
    update_attempt_fields as update_attempt_fields,
)
from girder.db.repo.baselines import (
    BaselineRun as BaselineRun,
    FlakyTest as FlakyTest,
    create_baseline_run as create_baseline_run,
    get_baseline_run as get_baseline_run,
    list_flaky_tests as list_flaky_tests,
    upsert_flaky_test as upsert_flaky_test,
)
from girder.db.repo.ci import (
    count_unreviewed_merges as count_unreviewed_merges,
    insert_ci_check_result as insert_ci_check_result,
    list_ci_check_results_for_run as list_ci_check_results_for_run,
)
from girder.db.repo.events import (
    get_first_transition_ts_to as get_first_transition_ts_to,
    get_latest_event as get_latest_event,
    insert_event as insert_event,
    insert_notification as insert_notification,
    insert_redaction as insert_redaction,
    list_events_for_run as list_events_for_run,
    list_notifications_for_run as list_notifications_for_run,
)
from girder.db.repo.integrity import (
    insert_integrity_violation as insert_integrity_violation,
    list_integrity_violations_for_run as list_integrity_violations_for_run,
)
from girder.db.repo.projects import (
    bump_clean_merge_streak as bump_clean_merge_streak,
    create_project as create_project,
    get_project as get_project,
    get_project_by_name as get_project_by_name,
    list_projects as list_projects,
    reset_clean_merge_streak as reset_clean_merge_streak,
    set_project_tier as set_project_tier,
)
from girder.db.repo.runs import (
    add_spend as add_spend,
    create_run as create_run,
    get_run as get_run,
    list_runs_for_project as list_runs_for_project,
    list_runs_in_status as list_runs_in_status,
    set_run_paused as set_run_paused,
    update_run_fields as update_run_fields,
)
from girder.db.repo.steering import (
    consume_steering_events as consume_steering_events,
    insert_steering_event as insert_steering_event,
)
from girder.db.repo.tasks import (
    create_task as create_task,
    find_tasks_in_statuses as find_tasks_in_statuses,
    get_task as get_task,
    list_tasks_for_run as list_tasks_for_run,
    list_tasks_for_wave as list_tasks_for_wave,
    update_task_fields as update_task_fields,
    update_task_scope_globs as update_task_scope_globs,
)
from girder.db.repo.tool_calls import (
    count_held_tool_calls as count_held_tool_calls,
    insert_tool_call as insert_tool_call,
    list_tool_calls_for_attempt as list_tool_calls_for_attempt,
)
from girder.db.repo.usage import (
    insert_token_usage as insert_token_usage,
    list_token_usage_for_attempt as list_token_usage_for_attempt,
    list_token_usage_for_run as list_token_usage_for_run,
    sum_token_usage_by_task_for_run as sum_token_usage_by_task_for_run,
    sum_token_usage_for_run as sum_token_usage_for_run,
    update_token_usage_actual as update_token_usage_actual,
)
from girder.db.repo.waves import (
    create_wave as create_wave,
    get_or_create_wave as get_or_create_wave,
    get_or_create_wave0 as get_or_create_wave0,
    list_waves_for_run as list_waves_for_run,
    set_wave_status as set_wave_status,
)
from girder.db.repo.worktrees import (
    create_worktree as create_worktree,
    find_active_worktrees_with_context as find_active_worktrees_with_context,
    find_stale_worktrees as find_stale_worktrees,
    get_worktree as get_worktree,
    list_worktrees as list_worktrees,
    set_worktree_state as set_worktree_state,
)

__all__ = [
    "BaselineRun",
    "FlakyTest",
    "SpecAmendment",
    "add_spend",
    "bump_clean_merge_streak",
    "consume_steering_events",
    "count_held_tool_calls",
    "count_unreviewed_merges",
    "create_attempt",
    "create_baseline_run",
    "create_project",
    "create_run",
    "create_spec_amendment",
    "create_task",
    "create_wave",
    "create_worktree",
    "find_active_worktrees_with_context",
    "find_attempts_in_status",
    "find_stale_worktrees",
    "find_tasks_in_statuses",
    "get_attempt",
    "get_baseline_run",
    "get_first_transition_ts_to",
    "get_latest_event",
    "get_or_create_wave",
    "get_or_create_wave0",
    "get_pending_amendment",
    "get_project",
    "get_project_by_name",
    "get_rejected_guidance",
    "get_run",
    "get_spec_amendment",
    "get_task",
    "get_worktree",
    "insert_attempt_diff",
    "insert_ci_check_result",
    "insert_event",
    "insert_integrity_violation",
    "insert_notification",
    "insert_redaction",
    "insert_steering_event",
    "insert_token_usage",
    "insert_tool_call",
    "latest_attempt_diff_for_task",
    "list_amendments_for_run",
    "list_attempt_diffs_for_run",
    "list_attempt_prompts",
    "list_attempts_for_task",
    "list_ci_check_results_for_run",
    "list_events_for_run",
    "list_flaky_tests",
    "list_integrity_violations_for_run",
    "list_notifications_for_run",
    "list_pending_amendments",
    "list_projects",
    "list_runs_for_project",
    "list_runs_in_status",
    "list_tasks_for_run",
    "list_tasks_for_wave",
    "list_token_usage_for_attempt",
    "list_token_usage_for_run",
    "list_tool_calls_for_attempt",
    "list_waves_for_run",
    "list_worktrees",
    "record_attempt_prompt",
    "reset_clean_merge_streak",
    "resolve_spec_amendment",
    "set_project_tier",
    "set_run_paused",
    "set_wave_status",
    "set_worktree_state",
    "sum_token_usage_by_task_for_run",
    "sum_token_usage_for_run",
    "update_attempt_fields",
    "update_run_fields",
    "update_spec_amendment_scope_globs",
    "update_task_fields",
    "update_task_scope_globs",
    "update_token_usage_actual",
    "upsert_flaky_test",
]
