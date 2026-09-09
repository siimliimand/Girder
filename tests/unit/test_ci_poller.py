"""CiPoller unit tests — GitHubClient over httpx.MockTransport; the tmp repo
only needs a local `git remote add origin` so owner_repo() never touches the
network (WP 4.3 part 1)."""

from __future__ import annotations

import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from girder.config import Secrets, Settings
from girder.db import repo
from girder.db.engine import Database
from girder.db.models import Run
from girder.github.ci import CiPoller, CiTimeout, extract_test_ids
from girder.github.client import GitHubClient
from girder.guard.redact import Redactor

PLANTED = "ghp_abcdef1234567890abcdef1234567890ABC"


def check(name: str, status: str, conclusion: str | None = None, **output: str) -> dict[str, Any]:
    return {
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "html_url": f"https://github.com/acme/widget/actions/runs/1#{name}",
        "output": {"summary": output.get("summary", ""), "text": output.get("text", "")},
    }


class ChecksTransport(httpx.MockTransport):
    """Serves the check-runs endpoint with scripted payloads, any host."""

    def __init__(self, payloads: list[list[dict[str, Any]]]) -> None:
        self.payloads = payloads
        self.urls: list[str] = []
        super().__init__(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.urls.append(str(request.url))
        payload = self.payloads.pop(0) if len(self.payloads) > 1 else self.payloads[0]
        return httpx.Response(200, json={"check_runs": payload, "total_count": len(payload)})


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_dir)], check=True)
    subprocess.run(
        ["git", "-C", str(repo_dir), "remote", "add", "origin",
         "https://github.com/acme/widget.git"],
        check=True,
    )
    return repo_dir


@pytest.fixture
async def client(
    db: Database, git_repo: Path
) -> AsyncIterator[tuple[GitHubClient, ChecksTransport]]:
    transport = ChecksTransport([])
    settings = Settings()
    settings.github.api_url = "http://test"  # MockTransport ignores the base URL
    gh = GitHubClient(
        settings,
        Secrets(),
        Redactor(),
        db=db,
        repo_path=git_repo,
        transport=transport,
    )
    yield gh, transport
    await gh.aclose()


def _poller(db: Database, gh: GitHubClient, settings: Settings | None = None) -> CiPoller:
    return CiPoller(db=db, client=gh, settings=settings or Settings(), redactor=Redactor())


async def _run(db: Database) -> Run:
    project = await repo.create_project(db, "proj", "/tmp/nowhere")
    return await repo.create_run(db, project.id, "intent", "run/x", 5.0)


# ------------------------------------------------------------------ snapshots


async def test_empty_checks_is_pending_not_green(db: Database, client: Any) -> None:
    gh, transport = client
    transport.payloads = [[]]
    run = await _run(db)
    snap = await _poller(db, gh).observe(run=run, head_sha="abc")
    assert snap.pending == ["(no checks reported)"]
    assert snap.green is False
    assert snap.settled is False


async def test_completed_success_is_green_and_settled(db: Database, client: Any) -> None:
    gh, transport = client
    transport.payloads = [[check("lint", "completed", "success")]]
    run = await _run(db)
    snap = await _poller(db, gh).observe(run=run, head_sha="abc")
    assert snap.green is True
    assert snap.settled is True
    assert snap.failed == []


async def test_failure_persisted_with_redacted_excerpt(db: Database, client: Any) -> None:
    gh, transport = client
    transport.payloads = [
        [
            check(
                "tests",
                "completed",
                "failure",
                summary="1 failed",
                text=f"token {PLANTED} leaked\nFAILED tests/test_a.py::test_x",
            )
        ]
    ]
    run = await _run(db)
    snap = await _poller(db, gh).observe(run=run, head_sha="abc")
    assert snap.settled is True
    assert snap.green is False
    assert [c.name for c in snap.failed] == ["tests"]

    rows = await repo.list_ci_check_results_for_run(db, run.id)
    assert len(rows) == 1
    row = rows[0]
    assert row["check_name"] == "tests"
    assert row["conclusion"] == "failure"
    excerpt = row["log_excerpt_redacted"] or ""
    assert PLANTED not in excerpt
    assert "REDACTED" in excerpt
    assert "FAILED tests/test_a.py::test_x" in excerpt
    event = await repo.get_latest_event(db, run.id, "ci_observed")
    assert event is not None
    assert event["payload"]["failed"] == ["tests"]


async def test_pending_check_blocks_settled(db: Database, client: Any) -> None:
    gh, transport = client
    transport.payloads = [[check("build", "in_progress")]]
    run = await _run(db)
    snap = await _poller(db, gh).observe(run=run, head_sha="abc")
    assert snap.pending == ["build"]
    assert snap.settled is False
    assert snap.green is False


# --------------------------------------------------------------------- waiting


async def test_wait_for_settled_timeout_raises(db: Database, client: Any) -> None:
    gh, transport = client
    transport.payloads = [[check("build", "queued")]]
    run = await _run(db)
    with pytest.raises(CiTimeout):
        await _poller(db, gh).wait_for_settled(
            run=run, head_sha="abc", interval_s=0.01, timeout_s=0.05
        )


async def test_wait_for_settled_returns_when_green(db: Database, client: Any) -> None:
    gh, transport = client
    transport.payloads = [
        [check("build", "in_progress")],
        [check("build", "completed", "success")],
    ]
    run = await _run(db)
    snap = await _poller(db, gh).wait_for_settled(
        run=run, head_sha="abc", interval_s=0.01, timeout_s=5.0
    )
    assert snap.green is True
    assert len(transport.urls) == 2  # polled until it settled


# ------------------------------------------------------------- nodeid scraping


def test_extract_test_ids_basic() -> None:
    text = (
        "FAILED tests/test_a.py::test_x - assert 1 == 2\n"
        "also tests/pkg/test_b.py::TestB::test_y[1-True] failed\n"
    )
    assert extract_test_ids(text) == [
        "tests/test_a.py::test_x",
        "tests/pkg/test_b.py::TestB::test_y[1-True]",
    ]


def test_extract_test_ids_dedup_and_order() -> None:
    text = "tests/test_a.py::test_x then tests/test_a.py::test_x again"
    assert extract_test_ids(text) == ["tests/test_a.py::test_x"]


def test_extract_test_ids_ignores_plain_paths_and_none() -> None:
    assert extract_test_ids("see tests/test_a.py for the fixture", None) == []
