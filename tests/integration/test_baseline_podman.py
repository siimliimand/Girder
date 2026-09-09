"""WP-3.3 — BaselineRunner against a real container runtime.

Requires podman or docker plus ``alpine:3.20``; skipped otherwise. Uses a
``sh`` suite_cmd override (alpine has no python/pytest) that emits a junit
report directly — this exercises the real container/worktree/xml-on-host
plumbing, not pytest itself.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from girder.config import Settings
from girder.db import repo
from girder.db.engine import Database
from girder.guard.redact import Redactor
from girder.orchestrator.baseline import BaselineRunner
from girder.sandbox.podman import PodmanEngine, detect_runtime

pytestmark = pytest.mark.integration

try:
    RUNTIME = detect_runtime()
    ENGINE = PodmanEngine(RUNTIME)
except RuntimeError:  # pragma: no cover - CI without a runtime
    pytest.skip("no container runtime available", allow_module_level=True)

IMAGE = "alpine:3.20"

SUITE_CMD = [
    "sh",
    "-c",
    'printf \'%s\' \'<testsuite name="t" tests="1">'
    '<testcase classname="tests.test_smoke" name="test_ok" file="tests/test_smoke.py"/>'
    "</testsuite>' > /workspace/.girder-baseline.xml",
]


def _git(repo_dir: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo_dir), *args], check=True, capture_output=True, text=True
    )
    return out.stdout.strip()


@pytest.fixture
def git_repo(tmp_path: Path) -> tuple[Path, str]:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    _git(repo_dir, "init", "-q", "-b", "main")
    _git(repo_dir, "config", "user.email", "t@t")
    _git(repo_dir, "config", "user.name", "t")
    (repo_dir / "tests").mkdir()
    (repo_dir / "tests" / "test_smoke.py").write_text("def test_ok() -> None:\n    assert True\n")
    _git(repo_dir, "add", "-A")
    _git(repo_dir, "commit", "-qm", "init")
    sha = _git(repo_dir, "rev-parse", "HEAD")
    return repo_dir, sha


async def test_baseline_records_green_suite(db: Database, git_repo: tuple[Path, str]) -> None:
    repo_dir, sha = git_repo
    project = await repo.create_project(db, "baseline-it", str(repo_dir))
    run = await repo.create_run(db, project.id, "baseline smoke", "main", 5.0)
    runner = BaselineRunner(
        db=db,
        sandbox=ENGINE,
        settings=Settings(),
        redactor=Redactor(),
        image=IMAGE,
        suite_cmd=SUITE_CMD,
    )

    outcome = await runner.run(project=project, run=run, repo_path=repo_dir, base_commit=sha)

    assert outcome.infra_error is None
    assert outcome.broken is False
    assert outcome.total == 1
    assert outcome.passed == 1
    row = await repo.get_baseline_run(db, outcome.baseline_run_id)
    assert row is not None
    assert row.per_test_json == (
        '{"tests/test_smoke.py::test_ok":'
        ' {"status": "passed", "rerun_status": null, "flaky": false}}'
    )
