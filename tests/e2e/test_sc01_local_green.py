"""SC-01 (lite): a two-plus-one task run drives to local green.

The scripted agent genuinely implements ``divide`` on calculator.py via the
real ToolRegistry + LocalExecSandbox; the TaskEngine verify step runs the REAL
pytest suite in the worktree. Baseline runs real pytest too.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

from girder.db import repo
from girder.db.models import TaskStatus
from tests.e2e.conftest import MakeCtx, git, write_commit_of

pytestmark = pytest.mark.e2e

CALC_WITH_DIVIDE = (
    '"""Calculator with division."""\n\n\n'
    "class Calculator:\n"
    "    def add(self, a: int, b: int) -> int:\n"
    '        """Return the sum."""\n'
    "        return a + b\n\n"
    "    def subtract(self, a: int, b: int) -> int:\n"
    '        """Return a minus b."""\n'
    "        return a - b\n\n"
    "    def multiply(self, a: int, b: int) -> int:\n"
    '        """Return the product."""\n'
    "        return a * b\n\n"
    "    def divide(self, a: int, b: int) -> float:\n"
    '        """Return a divided by b."""\n'
    "        return a / b\n"
)

DIVIDE_TEST = (
    "import sys\n"
    "from pathlib import Path\n\n"
    "sys.path.insert(0, str(Path(__file__).resolve().parents[1]))\n\n"
    "from calculator import Calculator\n\n\n"
    "def test_divide() -> None:\n"
    "    assert Calculator().divide(6, 3) == 2.0\n\n\n"
    "def test_divide_fraction() -> None:\n"
    "    assert Calculator().divide(1, 4) == 0.25\n"
)

README_DIVIDE = (
    "# e2e-target\n\n## Usage\n\n"
    "```python\nfrom calculator import Calculator\nc = Calculator()\n"
    "c.divide(6, 3)  # 2.0\n```\n"
)

PROPOSAL = """\
---
schema: girder.openspec/v1
title: Add division to the calculator
intent: Users can divide numbers.
tasks:
  - id: implement-divide
    title: Implement divide
    type: code_change
    scope_globs: ["calculator.py"]
    success_criteria: ["divide returns the quotient"]
    depends_on: []
  - id: divide-tests
    title: Add tests for divide
    type: test_change
    scope_globs: ["tests/**"]
    success_criteria: ["divide is covered by tests"]
    depends_on: []
  - id: readme-usage
    title: Document divide in the README
    type: documentation
    scope_globs: ["README.md"]
    success_criteria: ["README documents divide"]
    depends_on: []
---
Narrative.
"""


async def test_sc01_two_task_run_reaches_local_green(make_ctx: MakeCtx) -> None:
    ctx = await make_ctx(PROPOSAL)
    gateway_responses = [
        *write_commit_of("calculator.py", CALC_WITH_DIVIDE, tc_id="1"),
        *write_commit_of("tests/test_divide.py", DIVIDE_TEST, tc_id="2"),
        *write_commit_of("README.md", README_DIVIDE, tc_id="3"),
    ]
    from tests.e2e.conftest import FakeGateway

    gateway = FakeGateway(responses=gateway_responses)
    tip_before = await git(ctx.repo_path, "rev-parse", "main")

    descriptor = await ctx.engine(gateway).run_to_completion(ctx.run.id)
    assert descriptor == "local_green"

    fresh = await repo.get_run(ctx.db, ctx.run.id)
    assert fresh is not None
    tasks = await repo.list_tasks_for_run(ctx.db, ctx.run.id)
    assert [t.status for t in tasks] == [TaskStatus.COMPLETED] * 3

    # three task commits, in order, on the run branch
    branch = fresh.branch
    log = await git(ctx.repo_path, "log", "--reverse", "--format=%s", f"main..{branch}")
    subjects = log.splitlines()
    # spec freeze commit + one commit per task attempt
    assert subjects[0].startswith("spec: freeze openspec proposal")
    assert subjects[1:] == ["work", "work", "work"]
    assert (await git(ctx.repo_path, "rev-parse", branch)) != tip_before.strip()

    local_green = await repo.get_latest_event(ctx.db, ctx.run.id, "run_local_green")
    assert local_green is not None
    assert local_green["payload"]["task_count"] == 3

    # the REAL merged suite (fixture tests + the agent's new divide tests) is
    # green when run by us at the end on the run branch content
    await git(ctx.repo_path, "checkout", "-q", branch)
    try:
        proc = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "pytest", "-v", "-p", "no:cacheprovider"],
            cwd=ctx.repo_path,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "test_divide.py" in proc.stdout
    finally:
        await git(ctx.repo_path, "checkout", "-q", "main")


def test_fixture_repo_has_no_divide_yet() -> None:
    source = (Path(__file__).parents[1] / "fixtures" / "e2e-target" / "calculator.py").read_text()
    assert "def divide" not in source
    assert "def multiply" in source
