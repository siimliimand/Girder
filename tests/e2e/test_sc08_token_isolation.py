"""SC-08: zero host secrets reach the agent surface — even on a full merge.

Runs the entire pipeline (spec → tasks → PR → CI → conformance → merge) at
tier 1 with a distinguishable fake GitHub token, then sweeps every database
table for the token substring and checks the structural guarantee:
:class:`~girder.sandbox.engine.ContainerSpec` has no env/secret field at all.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from girder.db import repo
from girder.sandbox.engine import ContainerSpec
from tests.e2e.conftest import (
    E2E_GITHUB_TOKEN,
    FakeGateway,
    MakeDeliveryCtx,
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


async def test_sc08_full_merge_leaks_no_token(make_delivery_ctx: MakeDeliveryCtx) -> None:
    ctx = await make_delivery_ctx(PROPOSAL, autonomy_tier=1)
    gateway = FakeGateway(
        responses=[
            *write_commit_of("calculator.py", CALC_WITH_DIVIDE, tc_id="1"),
            *write_commit_of("tests/test_divide.py", DIVIDE_TEST, tc_id="2"),
            *write_commit_of("README.md", README_DIVIDE, tc_id="3"),
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
    assert descriptor == "merged"

    # 1. The token substring appears NOWHERE in the database: every user
    #    table, every row, stringified.
    tables = await ctx.db.fetchall(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name != 'schema_migrations'"
    )
    assert tables  # the database is not empty — the scan is real
    for row in tables:
        name = row["name"]
        rows = await ctx.db.fetchall(f'SELECT * FROM "{name}"')
        for data in rows:
            assert E2E_GITHUB_TOKEN not in str(data), f"token leaked into {name}"

    # 2. Structural guarantee: ContainerSpec is constructed explicitly with
    #    zero host secrets — no env, no secret field exists to fill.
    field_names = {f.name for f in dataclasses.fields(ContainerSpec)}
    assert not any("env" in n or "secret" in n for n in field_names), field_names

    # 3. Explicit re-check of the tool-call output blobs (subset of (1)).
    outputs = await ctx.db.fetchall("SELECT output_blob_redacted FROM tool_calls")
    assert outputs  # the scripted agents produced tool calls
    for row in outputs:
        blob = row["output_blob_redacted"]
        if blob is not None:
            assert E2E_GITHUB_TOKEN not in blob

    fresh = await repo.get_run(ctx.db, ctx.run.id)
    assert fresh is not None and fresh.status.value == "merged"
