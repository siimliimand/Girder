"""SC-10: a new flake is a REGRESSION — rerun green + not flaky on the
baseline ⇒ escalate; the test is never tagged known-flaky, nothing merges.

The failing check names a REAL passing test id from the fixture suite, so the
FlakyBreaker genuinely reruns it (HostSuiteSandbox, fresh worktree of the run
branch tip): it passes, and the baseline registry has no entry ⇒ regression.
"""

from __future__ import annotations

import json

import pytest

from girder.db import repo
from girder.db.models import RunStatus
from tests.e2e.conftest import (
    FakeGateway,
    MakeDeliveryCtx,
    gh_check,
    resp,
    write_commit_of,
)
from tests.e2e.test_sc01_local_green import (
    CALC_WITH_DIVIDE,
    DIVIDE_TEST,
    PROPOSAL,
    README_DIVIDE,
)

pytestmark = pytest.mark.e2e


async def test_sc10_new_flake_is_regression_and_escalates(
    make_delivery_ctx: MakeDeliveryCtx,
) -> None:
    ctx = await make_delivery_ctx(PROPOSAL, autonomy_tier=1)
    ctx.api.checks = [
        gh_check(
            "tests",
            "failure",
            summary="FAILED tests/test_arithmetic.py::test_add — intermittent",
            text="tests/test_arithmetic.py::test_add passed locally",
        )
    ]
    gateway = FakeGateway(
        responses=[
            *write_commit_of("calculator.py", CALC_WITH_DIVIDE, tc_id="1"),
            *write_commit_of("tests/test_divide.py", DIVIDE_TEST, tc_id="2"),
            *write_commit_of("README.md", README_DIVIDE, tc_id="3"),
            # conformance is never reached: escalation happens at CI classify
            resp(
                content=json.dumps(
                    {
                        "requirements_complete": True,
                        "undeclared_changes": [],
                        "severity": "none",
                        "summary": "ok",
                    }
                )
            ),
        ]
    )

    descriptor = await ctx.delivery_engine(gateway).run_to_completion(ctx.run.id)
    assert descriptor == "escalated"

    fresh = await repo.get_run(ctx.db, ctx.run.id)
    assert fresh is not None and fresh.status is RunStatus.ESCALATED

    # SC-10 point: NOT tagged flaky
    assert await repo.list_flaky_tests(ctx.db, ctx.project.id) == []

    assert ctx.api.merge_calls == 0
    assert any("regression" in c[2].lower() for c in ctx.notifier.calls)

    events = await ctx.db.fetchall(
        "SELECT event_type FROM agent_events WHERE run_id = ?", (ctx.run.id,)
    )
    assert any(e["event_type"] == "flaky_classification" for e in events)
