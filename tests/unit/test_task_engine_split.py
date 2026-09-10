"""Module-contract tests for the WP 10.2 task_engine split.

The orchestrator's task engine is split across ``attempt_lifecycle``,
``verification``, and ``work_salvage``; ``task_engine`` re-exports every
pre-split public name so existing callers never change. These tests pin the
split's module contracts and the re-export surface.
"""

from __future__ import annotations

import inspect


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
