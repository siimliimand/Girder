"""FlakyBreaker unit tests — FakeSandbox scripts exec calls and writes the
junit XML "inside the container" directly onto the host-visible worktree,
mirroring test_baseline.py (WP 4.3 part 1)."""

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
from girder.github.flaky import (
    RERUN_XML,
    VERDICT_BASELINE_BROKEN,
    VERDICT_KNOWN_FLAKY,
    VERDICT_REAL_FAILURE,
    VERDICT_REGRESSION,
    FlakeClassification,
    FlakyBreaker,
)
from girder.guard.redact import Redactor
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
        else:
            rows.append(f"<testcase {attrs}/>")
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<testsuite name="pytest" tests="{len(rows)}">{"".join(rows)}</testsuite>'
    )


def write_xml(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


PASS_XML = junit([("tests.test_a", "test_a", "tests/test_a.py", "")])
FAIL_XML = junit([("tests.test_a", "test_a", "tests/test_a.py", "failed")])

TEST_ID = "tests/test_a.py::test_a"


class FakeSandbox:
    """Scripts per-exec responses; effect callables receive (container, cmd)."""

    def __init__(self, responses: list[ExecResult | Callable[..., ExecResult]]) -> None:
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


def rerun_effect(holder: list[Path], xml: str) -> Callable[[str, list[str]], ExecResult]:
    def effect(name: str, cmd: list[str]) -> ExecResult:
        write_xml(holder[0] / RERUN_XML, xml)
        return ExecResult(exit_code=0, stdout="", stderr="")

    return effect


def make_breaker(db: Database, sandbox: FakeSandbox) -> FlakyBreaker:
    return FlakyBreaker(db=db, sandbox=sandbox, settings=Settings(), redactor=Redactor())  # type: ignore[arg-type]


def _init_git_repo(repo_dir: Path) -> str:
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo_dir)], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.name", "t"], check=True)
    (repo_dir / "README.md").write_text("hi\n")
    subprocess.run(["git", "-C", str(repo_dir), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "commit", "-qm", "init"], check=True)
    return subprocess.run(
        ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
async def seeded(db: Database, tmp_path: Path) -> tuple[Project, Run, str]:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    sha = await asyncio.to_thread(_init_git_repo, repo_dir)
    project = await repo.create_project(db, "proj", str(repo_dir))
    run = await repo.create_run(db, project.id, "feature", "run/x", 5.0)
    return project, run, sha


async def seed_baseline(db: Database, run: Run, per_test: dict[str, Any]) -> str:
    baseline = await repo.create_baseline_run(db, run.project_id, "basesha", json.dumps(per_test))
    await repo.update_run_fields(db, run.id, baseline_run_id=baseline.id)
    run.baseline_run_id = baseline.id  # keep the in-memory aggregate in sync
    return baseline.id


@pytest.fixture
def worktree_holder() -> list[Path]:
    return []


@pytest.fixture
def spy_worktree(monkeypatch: pytest.MonkeyPatch, worktree_holder: list[Path]) -> None:
    async def spy(argv: list[str], **kwargs: Any) -> Any:
        if "worktree" in argv and "add" in argv:
            wt = Path(argv[argv.index("--detach") + 1])
            await asyncio.to_thread(wt.mkdir, parents=True)
            worktree_holder.append(wt)
            return subprocess.CompletedProcess(argv, 0, "", "")
        return await run_host_cmd(argv, **kwargs)

    monkeypatch.setattr("girder.github.flaky.run_host_cmd", spy)


# ------------------------------------------------------------------- verdicts


async def test_green_rerun_baseline_flaky_known_flaky(
    db: Database,
    seeded: tuple[Project, Run, str],
    spy_worktree: None,
    worktree_holder: list[Path],
) -> None:
    project, run, sha = seeded
    await seed_baseline(db, run, {TEST_ID: {"status": "failed", "rerun_status": "passed",
                                            "flaky": True}})
    sandbox = FakeSandbox([rerun_effect(worktree_holder, PASS_XML)])
    breaker = make_breaker(db, sandbox)

    out = await breaker.classify(
        project=project, run=run, test_ids=[TEST_ID], commit_sha=sha,
        repo_path=Path(project.repo_path),
    )

    assert out == [FlakeClassification(TEST_ID, VERDICT_KNOWN_FLAKY, out[0].detail)]
    assert sandbox.killed == [sandbox.started[0].name]  # container torn down
    flaky = await repo.list_flaky_tests(db, project.id)
    assert [f.test_id for f in flaky] == [TEST_ID]
    assert flaky[0].last_seen_run == run.id
    event = await repo.get_latest_event(db, run.id, "flaky_classification")
    assert event is not None
    assert event["payload"]["classifications"][0]["verdict"] == VERDICT_KNOWN_FLAKY


async def test_green_rerun_registry_only_known_flaky(
    db: Database,
    seeded: tuple[Project, Run, str],
    spy_worktree: None,
    worktree_holder: list[Path],
) -> None:
    project, run, sha = seeded
    await repo.upsert_flaky_test(db, project.id, TEST_ID, run_id="old-run")
    sandbox = FakeSandbox([rerun_effect(worktree_holder, PASS_XML)])
    breaker = make_breaker(db, sandbox)

    out = await breaker.classify(
        project=project, run=run, test_ids=[TEST_ID], commit_sha=sha,
        repo_path=Path(project.repo_path),
    )

    assert out[0].verdict == VERDICT_KNOWN_FLAKY
    flaky = await repo.list_flaky_tests(db, project.id)
    assert flaky[0].last_seen_run == run.id  # registry refreshed


async def test_green_rerun_not_flaky_is_regression(
    db: Database,
    seeded: tuple[Project, Run, str],
    spy_worktree: None,
    worktree_holder: list[Path],
) -> None:
    project, run, sha = seeded
    sandbox = FakeSandbox([rerun_effect(worktree_holder, PASS_XML)])
    breaker = make_breaker(db, sandbox)

    out = await breaker.classify(
        project=project, run=run, test_ids=[TEST_ID], commit_sha=sha,
        repo_path=Path(project.repo_path),
    )

    assert out[0].verdict == VERDICT_REGRESSION
    assert out[0].detail is not None and "nondeterminism" in out[0].detail
    assert await repo.list_flaky_tests(db, project.id) == []  # never silently tagged


async def test_red_rerun_baseline_broken(
    db: Database,
    seeded: tuple[Project, Run, str],
    spy_worktree: None,
    worktree_holder: list[Path],
) -> None:
    project, run, sha = seeded
    await seed_baseline(db, run, {TEST_ID: {"status": "failed", "rerun_status": "failed",
                                            "flaky": False}})
    sandbox = FakeSandbox([rerun_effect(worktree_holder, FAIL_XML)])
    breaker = make_breaker(db, sandbox)

    out = await breaker.classify(
        project=project, run=run, test_ids=[TEST_ID], commit_sha=sha,
        repo_path=Path(project.repo_path),
    )

    assert out == [FlakeClassification(TEST_ID, VERDICT_BASELINE_BROKEN, out[0].detail)]
    assert await repo.list_flaky_tests(db, project.id) == []


async def test_red_rerun_not_on_baseline_real_failure(
    db: Database,
    seeded: tuple[Project, Run, str],
    spy_worktree: None,
    worktree_holder: list[Path],
) -> None:
    project, run, sha = seeded
    sandbox = FakeSandbox([rerun_effect(worktree_holder, FAIL_XML)])
    breaker = make_breaker(db, sandbox)

    out = await breaker.classify(
        project=project, run=run, test_ids=[TEST_ID], commit_sha=sha,
        repo_path=Path(project.repo_path),
    )

    assert out[0].verdict == VERDICT_REAL_FAILURE


# ------------------------------------------------------- conservative / no-ops


async def test_missing_report_counts_red(
    db: Database,
    seeded: tuple[Project, Run, str],
    spy_worktree: None,
    worktree_holder: list[Path],
) -> None:
    project, run, sha = seeded
    sandbox = FakeSandbox([ExecResult(exit_code=1, stdout="", stderr="collection error")])
    breaker = make_breaker(db, sandbox)

    out = await breaker.classify(
        project=project, run=run, test_ids=[TEST_ID], commit_sha=sha,
        repo_path=Path(project.repo_path),
    )

    assert out[0].verdict == VERDICT_REAL_FAILURE
    assert "conservative" in (out[0].detail or "")


async def test_empty_test_ids_skips_container(
    db: Database,
    seeded: tuple[Project, Run, str],
    spy_worktree: None,
) -> None:
    project, run, sha = seeded
    sandbox = FakeSandbox([])
    breaker = make_breaker(db, sandbox)

    out = await breaker.classify(
        project=project, run=run, test_ids=[], commit_sha=sha,
        repo_path=Path(project.repo_path),
    )

    assert out == []
    assert sandbox.started == []  # no worktree, no container, no reruns


async def test_worktree_and_tmpdir_cleaned_up(
    db: Database,
    seeded: tuple[Project, Run, str],
    spy_worktree: None,
    worktree_holder: list[Path],
) -> None:
    project, run, sha = seeded
    sandbox = FakeSandbox([rerun_effect(worktree_holder, PASS_XML)])
    breaker = make_breaker(db, sandbox)
    await breaker.classify(
        project=project, run=run, test_ids=[TEST_ID], commit_sha=sha,
        repo_path=Path(project.repo_path),
    )
    assert not worktree_holder[0].parent.exists()
