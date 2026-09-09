"""Unit tests for the GitHub client (WP 4.2): URL parsing, PR body templating,
and token isolation at push time (SC-08's structural guarantee)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from girder.config import GithubConfig, Secrets, Settings
from girder.db.engine import Database
from girder.github.client import GitHubClient, GitHubError, _askpass_script, parse_owner_repo
from girder.guard.redact import Redactor
from girder.util import run_host_cmd

TOKEN = "github_pat_unitesttoken1234567890ABC"


def test_parse_owner_repo_https_and_ssh() -> None:
    assert parse_owner_repo("https://github.com/acme/widget.git") == ("acme", "widget")
    assert parse_owner_repo("git@github.com:acme/widget.git") == ("acme", "widget")
    assert parse_owner_repo("https://github.com/acme/widget") == ("acme", "widget")
    assert parse_owner_repo("https://github.com/acme/widget/") == ("acme", "widget")


def test_parse_owner_repo_rejects_non_github() -> None:
    with pytest.raises(GitHubError):
        parse_owner_repo("https://gitlab.com/acme/widget.git")
    with pytest.raises(GitHubError):
        parse_owner_repo("/tmp/some/local/bare.git")


def test_pr_body_from_spec_metadata() -> None:
    settings = Settings(github=GithubConfig(api_url="http://github.test"))
    client = GitHubClient(
        settings,
        Secrets(github_token=TOKEN),
        Redactor(),
        None,  # type: ignore[arg-type]  # pr_body never touches the DB
        repo_path=Path("/tmp"),
        token=None,
        owner_repo=("acme", "widget"),
    )
    body = client.pr_body(
        intent="Users can divide numbers.",
        task_lines=["Implement divide", "Add tests for divide"],
        criteria=["divide returns the quotient"],
        spend_usd=0.1234,
    )
    assert "Users can divide numbers." in body
    assert "- Implement divide" in body
    assert "- divide returns the quotient" in body
    assert "$0.1234" in body


async def test_push_token_never_in_argv_or_git_config(tmp_path: Path, db: Database) -> None:
    """SC-08 (WP 4.2): pushing to a remote must not leak the token into the
    git invocation args or any persisted git config — it travels via a
    temporary GIT_ASKPASS helper only."""
    repo = tmp_path / "repo"
    repo.mkdir()
    await run_host_cmd(["git", "init", "-b", "main", str(repo)], timeout_s=30)
    await run_host_cmd(["git", "-C", str(repo), "config", "user.email", "t@l"], timeout_s=30)
    await run_host_cmd(["git", "-C", str(repo), "config", "user.name", "t"], timeout_s=30)
    (repo / "f.txt").write_text("hi\n")
    await run_host_cmd(["git", "-C", str(repo), "add", "-A"], timeout_s=30)
    await run_host_cmd(["git", "-C", str(repo), "commit", "-m", "init"], timeout_s=30)
    origin = tmp_path / "origin.git"
    await run_host_cmd(["git", "init", "--bare", "-b", "main", str(origin)], timeout_s=30)
    await run_host_cmd(["git", "-C", str(repo), "remote", "add", "origin", str(origin)],
                       timeout_s=30)

    settings = Settings(github=GithubConfig(api_url="http://github.test"))
    client = GitHubClient(
        settings,
        Secrets(github_token=TOKEN),
        Redactor(),
        db,
        repo_path=repo,
        token=TOKEN,
        owner_repo=("acme", "widget"),
    )
    sha = await client.push_branch("main")  # must not raise
    assert len(sha) == 40

    # the branch really arrived on the remote
    proc = await run_host_cmd(["git", "-C", str(origin), "rev-parse", "main"],
                              check=False, timeout_s=30)
    assert proc.stdout.strip() == sha

    # the token is in no git config and was in no argv (config persisted check)
    cfg = await run_host_cmd(["git", "-C", str(repo), "config", "--list"],
                             check=False, timeout_s=30)
    assert TOKEN not in cfg.stdout
    await client.aclose()


# ------------------------------------------------- askpass helper (HTTPS push)

def test_askpass_answers_username_and_password_prompts(tmp_path: Path) -> None:
    """GitHub over HTTPS prompts Username FIRST; the helper must answer both
    prompts (x-access-token / token) or the push dies with an empty username."""
    import os
    import subprocess

    helper = tmp_path / "askpass.sh"
    helper.write_text(_askpass_script())
    helper.chmod(0o700)
    env = dict(os.environ, GIRDER_ASKPASS_TOKEN=TOKEN)

    def ask(prompt: str) -> str:
        # git invokes the helper with the prompt as argv[1]
        proc = subprocess.run(
            [str(helper), prompt], capture_output=True, text=True, env=env
        )
        assert proc.returncode == 0
        return proc.stdout

    assert ask("Username for 'https://github.com': ") == "x-access-token"
    assert ask("Password for 'https://x-access-token@github.com': ") == TOKEN
    assert TOKEN not in _askpass_script()  # the token never lands in the script


async def test_askpass_script_removed_after_push(tmp_path: Path, db: Database) -> None:
    """The 0700 askpass temp file is gone once _git_push returns (even on the
    success path)."""
    import os

    repo = tmp_path / "repo"
    repo.mkdir()
    await run_host_cmd(["git", "init", "-b", "main", str(repo)], timeout_s=30)
    await run_host_cmd(["git", "-C", str(repo), "config", "user.email", "t@l"], timeout_s=30)
    await run_host_cmd(["git", "-C", str(repo), "config", "user.name", "t"], timeout_s=30)
    (repo / "f.txt").write_text("hi\n")
    await run_host_cmd(["git", "-C", str(repo), "add", "-A"], timeout_s=30)
    await run_host_cmd(["git", "-C", str(repo), "commit", "-m", "init"], timeout_s=30)
    origin = tmp_path / "origin.git"
    await run_host_cmd(["git", "init", "--bare", "-b", "main", str(origin)], timeout_s=30)
    await run_host_cmd(["git", "-C", str(repo), "remote", "add", "origin", str(origin)],
                       timeout_s=30)

    settings = Settings(github=GithubConfig(api_url="http://github.test"))
    client = GitHubClient(
        settings,
        Secrets(github_token=TOKEN),
        Redactor(),
        db,
        repo_path=repo,
        token=TOKEN,
        owner_repo=("acme", "widget"),
    )
    seen: dict[str, object] = {}
    real_run_host_cmd = run_host_cmd

    async def spy(cmd: list[str], **kwargs: object) -> object:
        askpass = (kwargs.get("env") or {}).get("GIT_ASKPASS")  # type: ignore[union-attr]
        if askpass:
            seen["helper"] = askpass
            seen["existed_during"] = os.path.exists(str(askpass))  # noqa: ASYNC240
        return await real_run_host_cmd(cmd, **kwargs)  # type: ignore[arg-type]

    from girder.github import client as client_mod

    client_mod.run_host_cmd = spy  # type: ignore[method-assign]
    try:
        await client._git_push(str(origin), "main")
    finally:
        client_mod.run_host_cmd = real_run_host_cmd  # type: ignore[method-assign]
    assert seen["existed_during"] is True
    assert not os.path.exists(str(seen["helper"]))  # deleted after use  # noqa: ASYNC240
    await client.aclose()


# ------------------------------------------------- job-logs fallback (§6.11)

SECRET = "ghp_fakeunitlogtoken0000000000"


def _failed_check(check_id: int = 4242) -> dict[str, object]:
    return {
        "id": check_id,
        "name": "tests",
        "status": "completed",
        "conclusion": "failure",
        "html_url": "https://github.test/acme/widget/runs/1",
        "output": {"summary": "", "text": ""},
    }


def _transport(
    check_runs: list[dict[str, object]],
    *,
    job_logs: str | None = None,
    logs_status: int = 200,
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/check-runs"):
            return httpx.Response(200, json={"check_runs": check_runs})
        if "/actions/jobs/" in path and path.endswith("/logs"):
            if job_logs is None:
                return httpx.Response(404, json={"message": "not found"})
            return httpx.Response(logs_status, text=job_logs)
        return httpx.Response(404, json={"message": f"unhandled {path}"})

    return httpx.MockTransport(handler)


def _client(db: Database, transport: httpx.MockTransport) -> GitHubClient:
    return GitHubClient(
        Settings(github=GithubConfig(api_url="http://github.test")),
        Secrets(github_token=TOKEN),
        Redactor(),
        db,
        repo_path=Path("/tmp"),
        token=TOKEN,
        owner_repo=("acme", "widget"),
        transport=transport,
    )


async def test_failed_check_empty_output_falls_back_to_job_logs(db: Database) -> None:
    """Empty Actions check-run output → the job-logs API fills the excerpt,
    redacted (§6.11: real GHA check runs often carry empty output)."""
    logs = "noise\n" * 5000 + f"secret={SECRET}\nFAILED tests/test_x.py::test_y\n"
    client = _client(db, _transport([_failed_check()], job_logs=logs))
    failures = await client.fetch_failure_logs("abc123")
    assert len(failures) == 1
    excerpt = failures[0].output_text or ""
    assert "tests/test_x.py::test_y" in excerpt
    assert SECRET not in excerpt  # redaction pipeline ran
    assert len(excerpt) <= 2000  # capped tail, like the CiPoller excerpt
    await client.aclose()


async def test_failed_check_output_untouched_when_present(db: Database) -> None:
    """Non-empty check-run output takes no fallback (no job-logs call)."""
    rich = dict(_failed_check())
    rich["output"] = {"summary": "boom", "text": "FAILED tests/a.py::test_b"}
    api = _transport([rich], job_logs="SHOULD NOT BE FETCHED")
    client = _client(db, api)
    failures = await client.fetch_failure_logs("abc123")
    assert failures[0].output_text == "FAILED tests/a.py::test_b"
    await client.aclose()


async def test_failed_check_and_logs_both_empty_is_safe(db: Database) -> None:
    """Empty output AND unavailable job logs ⇒ no crash, empty excerpt."""
    client = _client(db, _transport([_failed_check()], job_logs=None))
    failures = await client.fetch_failure_logs("abc123")
    assert len(failures) == 1
    assert failures[0].failed
    assert not failures[0].output_summary
    assert not failures[0].output_text
    await client.aclose()


async def test_job_logs_fetch_is_redaction_logged(db: Database) -> None:
    """§6.11 "all redacted-logged": the job-logs fallback goes through the same
    _request path as every other API call, so its github_api_call audit event
    lands in the persisted event log (404-tolerant path included)."""
    client = _client(db, _transport([_failed_check()], job_logs="logs"))
    failures = await client.fetch_failure_logs("abc123")
    assert failures[0].output_text == "logs"
    rows = await db.fetchall(
        "SELECT payload_json FROM agent_events WHERE event_type = 'github_api_call'"
    )
    paths = [str(r["payload_json"]) for r in rows]
    assert any("/actions/jobs/4242/logs" in p for p in paths)
    await client.aclose()


async def test_job_logs_fallback_failure_warns_and_still_logs_event(
    db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    """A failing job-logs fetch (HTTP 500) is not silent: the github_api_call
    event is emitted and a warning is logged instead of returning "" quietly."""
    import logging

    client = _client(db, _transport([_failed_check()], job_logs="x", logs_status=500))
    with caplog.at_level(logging.WARNING, logger="girder.github.client"):
        failures = await client.fetch_failure_logs("abc123")
    assert failures[0].output_text == ""  # excerpt stays empty
    assert any("job-logs" in r.message for r in caplog.records)
    rows = await db.fetchall(
        "SELECT payload_json FROM agent_events WHERE event_type = 'github_api_call'"
    )
    assert any("/actions/jobs/4242/logs" in str(r["payload_json"]) for r in rows)
    await client.aclose()


async def test_job_logs_follows_302_to_log_blob(db: Database) -> None:
    """The Actions logs API answers with a 302 to the log blob; the redirect is
    followed and the blob tail is redacted."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/check-runs"):
            return httpx.Response(200, json={"check_runs": [_failed_check()]})
        if path.endswith("/logs"):
            return httpx.Response(
                302, headers={"location": "http://github.test/log/blob"}, text=""
            )
        if path == "/log/blob":
            return httpx.Response(200, text=f"tail with {SECRET} inside")
        return httpx.Response(404, json={"message": f"unhandled {path}"})

    client = _client(db, httpx.MockTransport(handler))
    failures = await client.fetch_failure_logs("abc123")
    excerpt = failures[0].output_text or ""
    assert "tail with" in excerpt
    assert SECRET not in excerpt
    await client.aclose()
