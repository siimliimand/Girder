"""Module-contract tests for the WP 10.2 task_engine split.

The orchestrator's task engine is split across ``attempt_lifecycle``,
``verification``, and ``work_salvage``; ``task_engine`` re-exports every
pre-split public name so existing callers never change. These tests pin the
split's module contracts and the re-export surface.
"""

from __future__ import annotations

import inspect

from girder.db.models import Attempt, AttemptStatus
from girder.orchestrator.task_engine import _build_retry_brief


def test_new_modules_expose_split_contracts() -> None:
    from girder.orchestrator import attempt_lifecycle, verification, work_salvage

    # attempt_lifecycle: allocation / teardown / terminal bookkeeping.
    for name in (
        "AttemptContext",
        "allocate_attempt",
        "teardown_attempt",
        "kill_attempt_container",
        "worktree_row",
        "build_runtime",
        "record_engine_error",
        "close_attempt",
        "retry_or_fail",
        "fail_task",
        "standing_guidance",
    ):
        assert hasattr(attempt_lifecycle, name), name

    # AttemptContext carries the live attempt's identity.
    fields = attempt_lifecycle.AttemptContext.__dataclass_fields__
    assert set(fields) == {"attempt", "worktree", "manifest", "container_id"}

    # teardown keeps the cancellation semantics: force=False = kill + snapshots
    # only (the run pump owns the terminal transition).
    params = inspect.signature(attempt_lifecycle.teardown_attempt).parameters
    assert "force" in params and params["force"].kind is inspect.Parameter.KEYWORD_ONLY

    # verification: suite run via the stack plugin, audit, integration.
    for name in (
        "VerificationResult",
        "run_verification",
        "retry_step",
        "fail_without_retry",
        "integrity_violation",
        "redact_tail",
        "VERIFY_CMD",
        "VERIFY_XML",
        "_VERIFY_TIMEOUT_S",
    ):
        assert hasattr(verification, name), name

    # work_salvage: both the rich (sha + files) and sha-only variants.
    for name in ("salvage_worktree", "salvage_guidance", "salvage_dirty_worktree"):
        assert hasattr(work_salvage, name), name
    ret = inspect.signature(work_salvage.salvage_dirty_worktree).return_annotation
    assert "str" in ret


def test_task_engine_reexports_full_pre_split_surface() -> None:
    import girder.orchestrator.task_engine as te

    # Historical import surface (suites.py, orchestrator/__init__.py,
    # run_engine.py, github/diagnostic.py, tests).
    for name in (
        "TaskEngine",
        "TaskOutcome",
        "VERIFY_CMD",
        "VERIFY_XML",
        "_VERIFY_TIMEOUT_S",
        "AttemptContext",
        "allocate_attempt",
        "teardown_attempt",
        "VerificationResult",
        "run_verification",
        "salvage_dirty_worktree",
    ):
        assert hasattr(te, name), name

    # The verify command still routes through the stack plugin (7358bcc).
    from girder.stacks import STACK_REGISTRY

    assert te.VERIFY_CMD == STACK_REGISTRY["python-3.12"].test_command()
    assert te.VERIFY_XML == ".girder-verify.xml"

    # The engine-level compatibility shims tests rely on still exist.
    assert callable(te.TaskEngine._container_spec)
    assert callable(te.TaskEngine._redact_tail)


# ------------------------------------------------- WP 8.3 richer retry briefs


def _attempt(num: int) -> Attempt:
    return Attempt(
        id="att-1", task_id="t1", attempt_num=num, base_commit="abc", status=AttemptStatus.FAILED
    )


def test_retry_brief_turn_cap_with_salvaged_worktree() -> None:
    """A turn-cap death with a dirty (salvaged) worktree yields a brief that
    carries the failure reason, the turn spend, the salvage commit reference
    with its changed files, and the closing directive."""
    brief = _build_retry_brief(
        prev_attempt=_attempt(1),
        prev_failure_reason="turn budget exhausted (no terminal tool call)",
        prev_violations=[],
        turns_used=12,
        turn_budget=20,
        salvage=("deadbeefcafe", ["src/app.py", "src/util.py"]),
    )
    assert brief.startswith("[TRUSTED] Retry brief (attempt 1 failed):")
    assert "turn budget exhausted" in brief
    assert "Turns used: 12 of 20" in brief
    assert "src/app.py, src/util.py" in brief
    assert "deadbeefcafe" in brief  # the salvage commit reference
    assert "committed to the task branch" in brief and "do not redo it" in brief
    assert "Continue from the salvaged state." in brief


def test_retry_brief_without_salvage_and_with_violations() -> None:
    """No salvage => explicit no-changes line; scope violations are surfaced
    (kind + tool + path), capped at three."""
    violations = [
        {"kind": "out_of_scope_write", "detail": {"tool": "write_file", "args": {"path": "etc/x"}}},
        {"kind": "protected_read", "detail": {"tool": "read_file", "args": {"path": ".env"}}},
        {"kind": "scope_violation", "detail": {"tool": "run_command", "args": {"cmd": "curl"}}},
        {"kind": "scope_violation", "detail": {"tool": "run_command", "args": {}}},
    ]
    brief = _build_retry_brief(
        prev_attempt=_attempt(2),
        prev_failure_reason=None,
        prev_violations=violations,
        turns_used=4,
        turn_budget=10,
        salvage=None,
    )
    assert "Failure reason: turn budget exhausted" in brief  # None -> default reason
    assert "No changes were saved from the previous attempt." in brief
    assert "out_of_scope_write: write_file on etc/x" in brief
    assert "protected_read: read_file on .env" in brief
    assert "run_command on ?" in brief  # missing path degrades to '?'
    assert sum(1 for ln in brief.splitlines() if ln.startswith("    ")) == 3  # capped at 3
