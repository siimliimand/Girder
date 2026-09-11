"""SC-20: the node-20/TS stack drives a full run to local green (WS-07).

The SC-01 shape, replayed against ``tests/fixtures/e2e-target-ts`` with
``project.stack = "node-20"``: the scripted agent genuinely implements
``divide`` on ``src/calculator.ts`` via the real ToolRegistry +
LocalExecSandbox, and the TaskEngine verify step runs the REAL jest suite
through the NodePlugin's test command (``npx jest --ci`` with the jest-junit
reporter writing ``.girder-verify.xml``). Baseline runs real jest too.

Wiring notes: :class:`TsHostSuiteSandbox` translates the orchestrator's
pytest-shaped baseline argv onto the node stack's jest command (in the
container deployment the runner image's stack determines what actually
executes; the baseline runner hardcodes the python suite command), maps the
``--junitxml=/workspace/...`` flag onto jest-junit's ``JEST_JUNIT_OUTPUT_FILE``
environment variable, and symlinks ``node_modules`` into each sandbox
worktree (git worktrees can't carry the untracked install). Requires ``npm``
on PATH and network for ``npm install`` — skipped otherwise.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from girder.config import LimitsConfig, ProjectConfig, SandboxNetwork, Settings
from girder.db import repo
from girder.db.models import RunStatus, TaskStatus
from girder.sandbox.engine import ContainerSpec, ExecResult
from girder.stacks import VERIFY_XML, get_stack
from tests.e2e.conftest import HostSuiteSandbox, MakeCtx, git, write_commit_of

pytestmark = pytest.mark.e2e

NODE_CMD = get_stack("node-20").test_command()

CALC_WITH_DIVIDE = """\
/**
 * Calculator with division.
 */
export class Calculator {
  /** Return the sum of `a` and `b`. */
  add(a: number, b: number): number {
    return a + b;
  }

  /** Return `a` minus `b`. */
  subtract(a: number, b: number): number {
    return a - b;
  }

  /** Return the product of `a` and `b`. */
  multiply(a: number, b: number): number {
    return a * b;
  }

  /** Return `a` divided by `b`. */
  divide(a: number, b: number): number {
    return a / b;
  }
}
"""

DIVIDE_TEST = """\
/** Divide coverage for the SC-20 scripted agent's change. */
import { Calculator } from "../src/calculator";

describe("Calculator", () => {
  it("divides", () => {
    expect(new Calculator().divide(6, 3)).toBe(2);
  });

  it("divides fractions", () => {
    expect(new Calculator().divide(1, 4)).toBe(0.25);
  });
});
"""

README_DIVIDE = """\
# e2e-target-ts

## Usage

```ts
import { Calculator } from "./src/calculator";
const c = new Calculator();
c.divide(6, 3); // 2
```
"""

PROPOSAL = """\
---
schema: girder.openspec/v1
title: Add division to the TypeScript calculator
intent: Users can divide numbers.
tasks:
  - id: implement-divide
    title: Implement divide
    type: code_change
    scope_globs: ["src/calculator.ts"]
    success_criteria: ["divide returns the quotient"]
    depends_on: []
  - id: divide-tests
    title: Add tests for divide
    type: test_change
    scope_globs: ["tests/**"]
    success_criteria: ["divide is covered by tests"]
    depends_on: [implement-divide]
  - id: readme-usage
    title: Document divide in the README
    type: documentation
    scope_globs: ["README.md"]
    success_criteria: ["README documents divide"]
    depends_on: [divide-tests]
---
Narrative.
"""


class TsHostSuiteSandbox(HostSuiteSandbox):
    """HostSuiteSandbox adapted to the node-20 stack (see module docstring)."""

    def __init__(self, node_modules: Path) -> None:
        super().__init__()
        self._node_modules = node_modules

    async def start(self, spec: ContainerSpec) -> str:
        name = await super().start(spec)
        worktree = self.worktree_of(name)
        if worktree.is_dir() and self._node_modules.is_dir():
            link = worktree / "node_modules"
            if not link.exists():
                link.symlink_to(self._node_modules)
        return name

    async def exec(
        self, name: str, cmd: list[str], *, timeout_s: float = 120.0, user: str | None = None
    ) -> ExecResult:
        spec = self._specs[name]
        rewritten = list(cmd)
        if len(rewritten) >= 3 and rewritten[1:3] == ["-m", "pytest"]:
            # Baseline suite command (hardcoded pytest): swap in the node
            # stack's jest argv; --junitxml=/workspace/... becomes the
            # jest-junit env var, pointing at the same (rewritten) path.
            xml_flag = next(a for a in rewritten if a.startswith("--junitxml="))
            target = self._rewrite(xml_flag.partition("=")[2], spec)
            rewritten = list(NODE_CMD)
        else:
            # Every other exec (the verify suite included) still needs the
            # jest-junit report at the orchestrator's expected worktree path.
            target = self._rewrite(f"/workspace/{VERIFY_XML}", spec)
        # /usr/bin/env carries JEST_JUNIT_OUTPUT_FILE without touching the
        # inherited process environment (LocalExecSandbox passes no env=).
        prefixed = ["env", f"JEST_JUNIT_OUTPUT_FILE={target}", *rewritten]
        return await super().exec(name, prefixed, timeout_s=timeout_s, user=user)


def _npm_usable() -> bool:
    if shutil.which("npm") is None or shutil.which("npx") is None:
        return False
    probe = subprocess.run(["npm", "--version"], capture_output=True, text=True, timeout=60)
    return probe.returncode == 0


@pytest.fixture
async def e2e_repo(tmp_path: Path) -> Path:  # shadows the conftest fixture on purpose
    """The TS fixture copied to tmp, node_modules installed, on a real main."""
    if not _npm_usable():
        pytest.skip("node toolchain not installed (npm/npx) — SC-20 requires it")
    repo_path = tmp_path / "repo"
    shutil.copytree(Path(__file__).parents[1] / "fixtures" / "e2e-target-ts", repo_path)
    install = await asyncio.to_thread(
        subprocess.run,
        ["npm", "install", "--no-audit", "--no-fund"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if install.returncode != 0:
        pytest.skip(f"npm install failed (no network?): {install.stderr[-300:]}")
    # Keep the vendored toolchain out of the agent's `git add -A` commits and
    # the uncommitted-leftover audit; sandboxes symlink it into worktrees.
    (repo_path / ".gitignore").write_text("node_modules\n")
    await git(repo_path, "init", "-b", "main")
    await git(repo_path, "config", "user.email", "e2e@girder.local")
    await git(repo_path, "config", "user.name", "girder-e2e")
    await git(repo_path, "add", "-A")
    await git(repo_path, "commit", "-m", "initial: TS calculator without divide")
    return repo_path


@pytest.fixture
def settings() -> Settings:
    # Shadow of the conftest fixture selecting the node-20 stack.
    return Settings(
        project=ProjectConfig(test_directories=["tests"], stack="node-20"),
        limits=LimitsConfig(
            task_max_attempts=2,
            attempt_max_turns=8,
            attempt_wallclock_s=120,
            planning_turns=0,
        ),
        sandbox=SandboxNetwork(),
    )


async def test_sc20_ts_run_reaches_local_green(make_ctx: MakeCtx) -> None:
    ctx = await make_ctx(PROPOSAL, branch="run/e2e-ts")
    gateway_responses = [
        *write_commit_of("src/calculator.ts", CALC_WITH_DIVIDE, tc_id="1"),
        *write_commit_of("tests/divide.test.ts", DIVIDE_TEST, tc_id="2"),
        *write_commit_of("README.md", README_DIVIDE, tc_id="3"),
    ]
    from tests.e2e.conftest import FakeGateway

    gateway = FakeGateway(responses=gateway_responses)
    tip_before = await git(ctx.repo_path, "rev-parse", "main")

    engine = ctx.engine(gateway, sandbox=TsHostSuiteSandbox(ctx.repo_path / "node_modules"))
    descriptor = await engine.run_to_completion(ctx.run.id)
    assert descriptor == "local_green"

    fresh = await repo.get_run(ctx.db, ctx.run.id)
    assert fresh is not None and fresh.status is RunStatus.ACTIVE
    tasks = await repo.list_tasks_for_run(ctx.db, ctx.run.id)
    assert [t.status for t in tasks] == [TaskStatus.COMPLETED] * 3

    branch = fresh.branch
    log = await git(ctx.repo_path, "log", "--reverse", "--format=%s", f"main..{branch}")
    subjects = log.splitlines()
    assert subjects[0].startswith("spec: freeze openspec proposal")
    assert subjects[1:] == ["work", "work", "work"]
    assert (await git(ctx.repo_path, "rev-parse", branch)) != tip_before.strip()

    local_green = await repo.get_latest_event(ctx.db, ctx.run.id, "run_local_green")
    assert local_green is not None
    assert local_green["payload"]["task_count"] == 3

    # the REAL merged jest suite (fixture tests + the agent's divide tests)
    # is green when run by us at the end on the run branch content
    await git(ctx.repo_path, "checkout", "-q", branch)
    try:
        proc = await asyncio.to_thread(
            subprocess.run,
            NODE_CMD,
            cwd=ctx.repo_path,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
        # jest prints its summary on stderr
        assert "Tests:       5 passed, 5 total" in proc.stderr
    finally:
        await git(ctx.repo_path, "checkout", "-q", "main")


def test_ts_fixture_repo_has_no_divide_yet() -> None:
    source = (
        Path(__file__).parents[1] / "fixtures" / "e2e-target-ts" / "src" / "calculator.ts"
    ).read_text()
    # the docstring mentions it; the method itself must be absent
    assert "divide(" not in source
    assert "multiply" in source
