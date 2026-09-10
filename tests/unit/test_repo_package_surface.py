"""WP 10.3 import-surface parity tests.

The old single-module ``girder/db/repo.py`` was split into a package; every
name in the old public surface must remain importable both from the package
``girder.db.repo`` and from the owning submodule, with zero call-site changes
(resolution R-SP10-1).
"""

from __future__ import annotations

import asyncio

import girder.db.repo as repo
from girder.db.repo import amendments, attempts, baselines, ci, events
from girder.db.repo import integrity, projects, runs, steering, tasks, tool_calls
from girder.db.repo import usage, waves, worktrees

# The complete public surface of the pre-split repo.py (all top-level,
# non-underscore functions and public dataclasses; the old module had no
# ``__all__``, so its public surface was exactly these).
OLD_PUBLIC_SURFACE: list[str] = [
    # dataclasses (sprint-3 aggregate row types)
    "BaselineRun",
    "FlakyTest",
    "SpecAmendment",
    # projects
    "create_project",
    "get_project",
    "get_project_by_name",
    "list_projects",
    "set_project_tier",
    "bump_clean_merge_streak",
    "reset_clean_merge_streak",
    # runs
    "create_run",
    "get_run",
    "update_run_fields",
    "add_spend",
    "set_run_paused",
    "list_runs_for_project",
    "list_runs_in_status",
    # waves
    "create_wave",
    "get_or_create_wave0",
    "get_or_create_wave",
    "list_waves_for_run",
    "set_wave_status",
    # tasks
    "create_task",
    "get_task",
    "list_tasks_for_run",
    "list_tasks_for_wave",
    "update_task_fields",
    "find_tasks_in_statuses",
    "update_task_scope_globs",
    # attempts (+ prompts + diffs)
    "create_attempt",
    "get_attempt",
    "update_attempt_fields",
    "list_attempts_for_task",
    "find_attempts_in_status",
    "record_attempt_prompt",
    "list_attempt_prompts",
    "insert_attempt_diff",
    "list_attempt_diffs_for_run",
    "latest_attempt_diff_for_task",
    # worktrees
    "create_worktree",
    "get_worktree",
    "list_worktrees",
    "set_worktree_state",
    "find_stale_worktrees",
    "find_active_worktrees_with_context",
    # events / redaction / notifications
    "insert_event",
    "insert_redaction",
    "insert_notification",
    "list_notifications_for_run",
    "get_latest_event",
    "get_first_transition_ts_to",
    "list_events_for_run",
    # integrity
    "insert_integrity_violation",
    "list_integrity_violations_for_run",
    # steering
    "insert_steering_event",
    "consume_steering_events",
    # token usage
    "insert_token_usage",
    "update_token_usage_actual",
    "list_token_usage_for_run",
    "list_token_usage_for_attempt",
    "sum_token_usage_for_run",
    "sum_token_usage_by_task_for_run",
    # baseline runs / flaky tests
    "create_baseline_run",
    "get_baseline_run",
    "upsert_flaky_test",
    "list_flaky_tests",
    # spec amendments
    "create_spec_amendment",
    "update_spec_amendment_scope_globs",
    "get_spec_amendment",
    "list_amendments_for_run",
    "get_pending_amendment",
    "list_pending_amendments",
    "get_rejected_guidance",
    "resolve_spec_amendment",
    # tool calls
    "insert_tool_call",
    "list_tool_calls_for_attempt",
    "count_held_tool_calls",
    # CI checks & merge
    "insert_ci_check_result",
    "list_ci_check_results_for_run",
    "count_unreviewed_merges",
]

_SUBMODULES = [
    amendments,
    attempts,
    baselines,
    ci,
    events,
    integrity,
    projects,
    runs,
    steering,
    tasks,
    tool_calls,
    usage,
    waves,
    worktrees,
]


def test_package_reexports_full_old_surface() -> None:
    for name in OLD_PUBLIC_SURFACE:
        assert hasattr(repo, name), f"girder.db.repo lost {name!r} in the split"
        obj = getattr(repo, name)
        # Re-exports must be the same objects the submodules define, not
        # wrappers — attribute identity proves zero behavior change.
        assert any(
            obj is getattr(mod, name, None) for mod in _SUBMODULES
        ), f"repo.{name} is not re-exported from a submodule"


def test_package_all_matches_surface_and_everything_importable() -> None:
    assert set(repo.__all__) == set(OLD_PUBLIC_SURFACE)
    for name in repo.__all__:
        obj = getattr(repo, name)
        assert asyncio.iscoroutinefunction(obj) or isinstance(obj, type), name
