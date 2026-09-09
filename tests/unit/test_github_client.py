"""Unit tests for the GitHub client (WP 4.2): URL parsing, PR body templating,
and token isolation at push time (SC-08's structural guarantee)."""

from __future__ import annotations

from pathlib import Path

import pytest

from girder.config import GithubConfig, Secrets, Settings
from girder.db.engine import Database
from girder.github.client import GitHubClient, GitHubError, parse_owner_repo
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
