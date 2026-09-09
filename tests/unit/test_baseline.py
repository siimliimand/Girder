"""BaselineRunner unit tests — FakeSandbox scripts exec calls and writes the
junit XML "inside the container" directly onto the host-visible worktree."""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from girder.config import Settings
from girder.db import repo
from girder.db.engine import Database
from girder.db.models import Project, Run
from girder.guard.redact import Redactor
from girder.orchestrator.baseline import (
    BaselineOutcome,
    BaselineRunner,
    normalize_test_id,
    parse_junit_xml,
)
from girder.sandbox.engine import ContainerSpec, ExecResult
from girder.util import run_host_cmd

# --------------------------------------------------------------------- helpers


def junit(testcases: list[tuple[str, str, str, str]]) -> str:
    """(classname, name, file, status) -> junit xml; status '' means passed."""
    rows = []
    for classname, name, file, status in testcases:
        attrs = f'classname="{classname}" name="{name}"'
        if file:
            attrs += f' file="{file}"'
        if status == "failed":
            rows.append(f"<testcase {attrs}><failure>boom</failure></testcase>")
        elif status == "error":
            rows.append(f"<testcase {attrs}><error>kaput</error></testcase>")
        elif status == "skipped":
            rows.append(f"<testcase {attrs}><skipped/></testcase>")
        else:
            rows.append(f"<testcase {attrs}/>")
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<testsuite name="pytest" tests="{len(rows)}">{"".join(rows)}</testsuite>'
    )


def write_xml(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class FakeSandbox:
    """Scripts per-exec responses; effect callables receive (container, cmd)."""

    def __init__(
        self,
        responses: list[ExecResult | Callable[..., ExecResult]],
    ) -> None:
        self.responses = responses
        self.started: list[ContainerSpec] = []
        self.calls: list[tuple[str, list[str]]] = []
        self.killed: list[str] = []

    async def start(self, spec: ContainerSpec) -> str:
        self.started.append(spec)
        return spec.name

    async def exec(
        self, name: str, cmd: list[str], *, timeout_s: float = 120.0, user: str | None = None
    ) -> ExecResult:
        self.calls.append((name, cmd))
        if not self.responses:
            return ExecResult(exit_code=1, stdout="", stderr="no scripted response")
        item = self.responses.pop(0)
        if callable(item):
            return item(name, cmd)
        return item

    async def kill(self, name: str) -> None:
        self.killed.append(name)

    async def exists(self, name: str) -> bool:
        return name not in self.killed


def suite_effect(
    holder: list[Path],
    xml: str,
    *,
    exit_code: int = 0,
    timed_out: bool = False,
    stderr: str = "",
) -> Callable[[str, list[str]], ExecResult]:
    """Full-suite exec response; writes the junit report into the worktree."""

    def effect(name: str, cmd: list[str]) -> ExecResult:
        write_xml(holder[0] / ".girder-baseline.xml", xml)
        return ExecResult(exit_code=exit_code, stdout="", stderr=stderr, timed_out=timed_out)

    return effect


def rerun_effect(holder: list[Path], xml: str) -> Callable[[str, list[str]], ExecResult]:
    def effect(name: str, cmd: list[str]) -> ExecResult:
        write_xml(holder[0] / ".girder-rerun.xml", xml)
        return ExecResult(exit_code=0, stdout="", stderr="")

    return effect


def make_runner(db: Database, sandbox: FakeSandbox) -> BaselineRunner:
    return BaselineRunner(
        db=db,
        sandbox=sandbox,  # type: ignore[arg-type]
        settings=Settings(),
        redactor=Redactor(),
    )


def _init_git_repo(repo_dir: Path) -> str:
    """Sync helper — heavy blocking git setup, run via asyncio.to_thread."""
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo_dir)], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.name", "t"], check=True)
    (repo_dir / "README.md").write_text("hi\n")
    subprocess.run(["git", "-C", str(repo_dir), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "commit", "-qm", "init"], check=True)
    sha = subprocess.run(
        ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return sha


@pytest.fixture
async def seeded(db: Database, tmp_path: Path) -> tuple[Project, Run, str]:
    """A project whose repo_path is a real git repo with one commit."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    sha = await asyncio.to_thread(_init_git_repo, repo_dir)
    project = await repo.create_project(db, "proj", str(repo_dir))
    run = await repo.create_run(db, project.id, "baseline", "main", 5.0)
    return project, run, sha


@pytest.fixture
def worktree_holder() -> list[Path]:
    """Receives the runner's temp worktree path (normally only the container sees it)."""
    return []


@pytest.fixture
def spy_worktree(monkeypatch: pytest.MonkeyPatch, worktree_holder: list[Path]) -> None:
    """Intercept the git worktree add so effects can write the "in-container" xml."""

    async def spy(argv: list[str], **kwargs: Any) -> Any:
        if "worktree" in argv and "add" in argv:
            wt = Path(argv[argv.index("--detach") + 1])
            await asyncio.to_thread(wt.mkdir, parents=True)
            worktree_holder.append(wt)
            # A detached worktree of a commit also records metadata in the repo;
            # skip the real git call and clean up via the tempdir rmtree instead.
            return subprocess.CompletedProcess(argv, 0, "", "")
        return await run_host_cmd(argv, **kwargs)

    monkeypatch.setattr("girder.orchestrator.baseline.run_host_cmd", spy)


async def run_baseline(
    runner: BaselineRunner, project: Project, run: Run, sha: str
) -> BaselineOutcome:
    return await runner.run(
        project=project, run=run, repo_path=Path(project.repo_path), base_commit=sha
    )


# ------------------------------------------------------------------ pure parsing


def test_normalize_with_file_attr() -> None:
    assert (
        normalize_test_id("tests.test_a.TestA", "test_x[1-True]", "tests/test_a.py")
        == "tests/test_a.py::TestA::test_x[1-True]"
    )


def test_normalize_falls_back_to_classname() -> None:
    assert (
        normalize_test_id("tests.test_a.TestA", "test_x", None) == "tests/test_a.py::TestA::test_x"
    )


def test_normalize_flat_classname() -> None:
    assert normalize_test_id("test_flat", "test_y", None) == "test_flat.py::test_y"


def test_parse_bare_testsuite() -> None:
    results = parse_junit_xml(
        junit(
            [
                ("tests.test_a.TestA", "test_x", "tests/test_a.py", ""),
                ("tests.test_a.TestA", "test_bad", "tests/test_a.py", "failed"),
                ("tests.test_a.TestA", "test_skip", "tests/test_a.py", "skipped"),
            ]
        )
    )
    assert set(results) == {
        "tests/test_a.py::TestA::test_x",
        "tests/test_a.py::TestA::test_bad",
        "tests/test_a.py::TestA::test_skip",
    }
    assert results["tests/test_a.py::TestA::test_bad"].status == "failed"
    assert results["tests/test_a.py::TestA::test_skip"].status == "skipped"


def test_parse_testsuites_wrapper_and_classname_fallback() -> None:
    def bare(status: str, name: str) -> str:
        # junit() without its xml declaration, so both fit inside <testsuites>
        return junit([("pkg.mod.TestB", name, "", status)]).split("?>", 1)[1]

    text = "<testsuites>" + bare("error", "test_q") + bare("", "test_ok") + "</testsuites>"
    results = parse_junit_xml(text)
    assert results["pkg/mod.py::TestB::test_q"].status == "error"
    assert results["pkg/mod.py::TestB::test_ok"].status == "passed"


def test_parse_empty_returns_no_testcases() -> None:
    assert parse_junit_xml('<testsuite name="pytest" tests="0"></testsuite>') == {}


# ------------------------------------------------------------------- runner tests


async def test_happy_all_green(
    db: Database,
    seeded: tuple[Project, Run, str],
    spy_worktree: None,
    worktree_holder: list[Path],
) -> None:
    project, run, sha = seeded
    xml = junit(
        [
            ("tests.test_a", "test_one", "tests/test_a.py", ""),
            ("tests.test_a", "test_two", "tests/test_a.py", ""),
            ("tests.test_b", "test_three", "tests/test_b.py", ""),
        ]
    )
    sandbox = FakeSandbox([suite_effect(worktree_holder, xml)])
    runner = make_runner(db, sandbox)

    outcome = await run_baseline(runner, project, run, sha)

    assert outcome.broken is False
    assert outcome.failed == 0
    assert outcome.total == 3
    assert outcome.passed == 3
    assert outcome.infra_error is None
    assert len(sandbox.calls) == 1  # rerun never invoked
    row = await repo.get_baseline_run(db, outcome.baseline_run_id)
    assert row is not None and row.commit_sha == sha
    per_test = json.loads(row.per_test_json)
    assert per_test["tests/test_a.py::test_one"] == {
        "status": "passed",
        "rerun_status": None,
        "flaky": False,
    }


async def test_flaky_pass_on_rerun(
    db: Database,
    seeded: tuple[Project, Run, str],
    spy_worktree: None,
    worktree_holder: list[Path],
) -> None:
    project, run, sha = seeded
    suite_xml = junit(
        [
            ("tests.test_a", "test_a", "tests/test_a.py", "failed"),
            ("tests.test_a", "test_ok", "tests/test_a.py", ""),
        ]
    )
    rerun_xml = junit([("tests.test_a", "test_a", "tests/test_a.py", "")])
    sandbox = FakeSandbox(
        [
            suite_effect(worktree_holder, suite_xml, exit_code=1),
            rerun_effect(worktree_holder, rerun_xml),
        ]
    )
    runner = make_runner(db, sandbox)

    outcome = await run_baseline(runner, project, run, sha)

    assert outcome.flaky_ids == ["tests/test_a.py::test_a"]
    assert outcome.broken_ids == []
    assert outcome.broken is False
    assert len(sandbox.calls) == 2
    assert "tests/test_a.py::test_a" in sandbox.calls[1][1]  # rerun targeted the flake
    flaky = await repo.list_flaky_tests(db, project.id)
    assert [f.test_id for f in flaky] == ["tests/test_a.py::test_a"]
    assert flaky[0].last_seen_run == run.id


async def test_broken_fails_twice(
    db: Database,
    seeded: tuple[Project, Run, str],
    spy_worktree: None,
    worktree_holder: list[Path],
) -> None:
    project, run, sha = seeded
    fail_xml = junit([("tests.test_b.TestB", "test_b", "tests/test_b.py", "failed")])
    sandbox = FakeSandbox(
        [
            suite_effect(worktree_holder, fail_xml, exit_code=1),
            rerun_effect(worktree_holder, fail_xml),
        ]
    )
    runner = make_runner(db, sandbox)

    outcome = await run_baseline(runner, project, run, sha)

    assert outcome.broken is True
    assert outcome.broken_ids == ["tests/test_b.py::TestB::test_b"]
    assert outcome.flaky_ids == []
    assert await repo.list_flaky_tests(db, project.id) == []


async def test_mixed_flaky_and_broken(
    db: Database,
    seeded: tuple[Project, Run, str],
    spy_worktree: None,
    worktree_holder: list[Path],
) -> None:
    project, run, sha = seeded
    suite_xml = junit(
        [
            ("tests.test_a", "test_a", "tests/test_a.py", "failed"),
            ("tests.test_b", "test_b", "tests/test_b.py", "error"),
        ]
    )
    flaky_pass = junit([("tests.test_a", "test_a", "tests/test_a.py", "")])
    still_error = junit([("tests.test_b", "test_b", "tests/test_b.py", "error")])
    sandbox = FakeSandbox(
        [
            suite_effect(worktree_holder, suite_xml, exit_code=1),
            rerun_effect(worktree_holder, flaky_pass),
            rerun_effect(worktree_holder, still_error),
        ]
    )
    runner = make_runner(db, sandbox)

    outcome = await run_baseline(runner, project, run, sha)

    assert outcome.flaky_ids == ["tests/test_a.py::test_a"]
    assert outcome.broken_ids == ["tests/test_b.py::test_b"]
    assert outcome.broken is True
    assert outcome.failed == 2
    assert [f.test_id for f in await repo.list_flaky_tests(db, project.id)] == [
        "tests/test_a.py::test_a"
    ]


async def test_skipped_not_counted_failed(
    db: Database,
    seeded: tuple[Project, Run, str],
    spy_worktree: None,
    worktree_holder: list[Path],
) -> None:
    project, run, sha = seeded
    xml = junit([("tests.test_a", "test_skip", "tests/test_a.py", "skipped")])
    sandbox = FakeSandbox([suite_effect(worktree_holder, xml)])
    runner = make_runner(db, sandbox)

    outcome = await run_baseline(runner, project, run, sha)

    assert outcome.failed == 0
    assert outcome.broken is False
    assert len(sandbox.calls) == 1


async def test_infra_timeout_no_baseline_row(
    db: Database,
    seeded: tuple[Project, Run, str],
    spy_worktree: None,
    worktree_holder: list[Path],
) -> None:
    project, run, sha = seeded
    sandbox = FakeSandbox(
        [
            ExecResult(exit_code=-1, stdout="", stderr="killed", timed_out=True),
        ]
    )
    runner = make_runner(db, sandbox)

    outcome = await run_baseline(runner, project, run, sha)

    assert outcome.infra_error is not None
    assert "wall-clock" in outcome.infra_error
    assert outcome.baseline_run_id == ""
    assert sandbox.killed == [sandbox.started[0].name]  # container killed
    assert not worktree_holder[0].exists()  # worktree removed
    assert not worktree_holder[0].parent.exists()  # tempdir removed


async def test_infra_nonzero_without_xml(
    db: Database,
    seeded: tuple[Project, Run, str],
    spy_worktree: None,
    worktree_holder: list[Path],
) -> None:
    project, run, sha = seeded
    sandbox = FakeSandbox(
        [
            ExecResult(exit_code=127, stdout="", stderr="python: not found"),
        ]
    )
    runner = make_runner(db, sandbox)

    outcome = await run_baseline(runner, project, run, sha)

    assert outcome.infra_error is not None
    assert "127" in outcome.infra_error
    assert sandbox.killed  # container killed on infra failure too


async def test_output_tail_redacted(
    db: Database,
    seeded: tuple[Project, Run, str],
    spy_worktree: None,
    worktree_holder: list[Path],
) -> None:
    project, run, sha = seeded
    sandbox = FakeSandbox(
        [
            ExecResult(
                exit_code=127,
                stdout="",
                stderr="config had AKIAIOSFODNN7EXAMPLE in env\npython: not found",
            ),
        ]
    )
    runner = make_runner(db, sandbox)

    outcome = await run_baseline(runner, project, run, sha)

    assert outcome.infra_error is not None
    tail = outcome.output_tail_redacted or ""
    assert "AKIAIOSFODNN7EXAMPLE" not in tail
    assert "REDACTED" in tail
