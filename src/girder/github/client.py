"""GitHub delivery client (impl-plan §6.11, Sprint 4 WP 4.2).

A thin ``httpx`` client over the GitHub REST API — the same idiom as
:class:`~girder.models.gateway.ModelGateway` and the notifier (deviation from
the impl-plan's PyGithub pick, resolution R10: the codebase's transport,
retry and redaction plumbing is already httpx-based, and a wrapper around a
second HTTP stack would only thicken the surface this module is supposed to
keep thin). Every persisted log line passes through the redactor; the token
never reaches a container, a worktree, git config, or argv (pushes use a
temporary ``GIT_ASKPASS`` helper).

Host-side git operations (push) run in the orchestrator process only — the
agent has no path to this module.
"""

from __future__ import annotations

import logging
import stat
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import httpx

from girder.config import Secrets, Settings
from girder.db import repo
from girder.db.engine import Database
from girder.guard.redact import Redactor
from girder.util import run_host_cmd

log = logging.getLogger(__name__)

DEFAULT_API_URL = "https://api.github.com"

#: Cap on the Actions job-log tail kept as a check excerpt (§8.6: excerpts only).
_LOG_TAIL_CHARS = 2000


class GitHubError(RuntimeError):
    """A GitHub API call failed after its retry budget."""


@dataclass(frozen=True)
class CheckRun:
    """Normalized view of one check run on a commit."""

    name: str
    status: str  # queued | in_progress | completed
    conclusion: str | None  # success | failure | neutral | cancelled | timed_out | …
    url: str | None = None
    output_summary: str | None = None
    output_text: str | None = None
    # check-run id (== the Actions job id for GHA runs); used for the
    # job-logs fallback when the check-run output is empty (impl-plan §6.11)
    id: str | None = None

    @property
    def completed(self) -> bool:
        return self.status == "completed"

    @property
    def failed(self) -> bool:
        return self.completed and self.conclusion not in (None, "success", "neutral", "skipped")


@dataclass(frozen=True)
class MergeOutcome:
    merged: bool
    sha: str | None = None
    reason: str | None = None


def parse_owner_repo(remote_url: str) -> tuple[str, str]:
    """Extract ``(owner, repo)`` from an https or ssh GitHub remote URL."""
    url = remote_url.strip().removesuffix(".git").removesuffix("/")
    if url.startswith("git@"):
        url = url.replace(":", "/", 1)  # git@github.com:owner/repo -> git@github.com/owner/repo
    for marker in ("github.com/", "github.com:"):
        idx = url.find(marker)
        if idx != -1:
            rest = url[idx + len(marker) :]
            parts = rest.split("/")
            if len(parts) >= 2:
                return parts[0], parts[1]
    raise GitHubError(f"not a GitHub remote URL: {remote_url!r}")


class GitHubClient:
    """Push / PR / checks / comments / merge — all calls redacted-logged."""

    def __init__(
        self,
        settings: Settings,
        secrets: Secrets,
        redactor: Redactor,
        db: Database,
        *,
        repo_path: Path,
        transport: httpx.AsyncBaseTransport | None = None,
        token: str | None = None,
        owner_repo: tuple[str, str] | None = None,
    ) -> None:
        self.settings = settings
        self.redactor = redactor
        self.db = db
        self.repo_path = Path(repo_path)
        self.token = token if token is not None else secrets.github_token
        # Explicit (owner, repo) override for non-GitHub remotes (local bare
        # remotes in tests, API proxies); parsed from the remote URL otherwise.
        self._owner_repo = owner_repo
        self.api_url = settings.github.api_url.rstrip("/") or DEFAULT_API_URL
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        self._client = httpx.AsyncClient(
            base_url=self.api_url, headers=headers, transport=transport, timeout=60.0
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------- primitives

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        ok: frozenset[int] | None = None,
    ) -> httpx.Response:
        """One API call; raises :class:`GitHubError` on transport/HTTP failure.

        ``ok`` lists extra HTTP status codes to return instead of raising
        (e.g. the 405 a refused merge returns — the caller wants the reason).
        """
        try:
            resp = await self._client.request(method, path, json=json_body)
        except httpx.TransportError as exc:
            raise GitHubError(f"{method} {path}: transport error: {exc}") from exc
        if resp.status_code >= 400 and (ok is None or resp.status_code not in ok):
            body = self._redact_sync(resp.text)
            raise GitHubError(f"{method} {path}: HTTP {resp.status_code}: {body[:300]}")
        await repo.insert_event(
            self.db,
            "github_api_call",
            {"method": method, "path": path, "status": resp.status_code},
        )
        return resp

    def _redact_sync(self, text: str) -> str:
        clean, _ = self.redactor.redact(text)
        return clean

    # -------------------------------------------------------------- remote url

    async def remote_url(self) -> str:
        """The configured remote's URL (``git remote get-url <remote>``)."""
        proc = await run_host_cmd(
            ["git", "-C", str(self.repo_path), "remote", "get-url", self.settings.github.remote],
            timeout_s=30,
        )
        return proc.stdout.strip()

    async def owner_repo(self) -> tuple[str, str]:
        if self._owner_repo is not None:
            return self._owner_repo
        return parse_owner_repo(await self.remote_url())

    # -------------------------------------------------------------------- push

    async def push_branch(self, branch: str) -> str:
        """Push *branch* to the remote; returns the pushed commit sha.

        Credentials travel via a 0700 ``GIT_ASKPASS`` helper script — never in
        argv, never in git config, deleted before this call returns. The
        orchestrator process performs the push; container environments are
        constructed without any of this (zero host secrets, plan §9).
        """
        url = await self.remote_url()
        await self._git_push(url, branch)
        proc = await run_host_cmd(
            ["git", "-C", str(self.repo_path), "rev-parse", f"refs/heads/{branch}"],
            timeout_s=30,
        )
        return proc.stdout.strip()

    async def _git_push(self, remote_url: str, branch: str) -> None:
        if not self.token:
            # No token: rely on the host's existing credential setup (ssh remotes).
            await run_host_cmd(
                ["git", "-C", str(self.repo_path), "push", "-u", remote_url, branch],
                timeout_s=300,
            )
            return
        with tempfile.TemporaryDirectory(prefix="girder-askpass-") as tmp:
            helper = Path(tmp) / "askpass.sh"
            helper.write_text(
                "#!/bin/sh\n"
                'case "$1" in\n'
                '  *Password*) printf \'%s\' "$GIRDER_ASKPASS_TOKEN" ;;\n'
                "esac\n"
            )
            helper.chmod(stat.S_IRWXU)  # 0700: owner-only
            try:
                await run_host_cmd(
                    ["git", "-C", str(self.repo_path), "push", "-u", remote_url, branch],
                    timeout_s=300,
                    env={
                        "GIT_ASKPASS": str(helper),
                        "GIRDER_ASKPASS_TOKEN": self.token,
                        "GIT_TERMINAL_PROMPT": "0",
                    },
                )
            finally:
                helper.unlink(missing_ok=True)

    # ---------------------------------------------------------------------- PR

    def pr_body(self, *, intent: str, task_lines: list[str], criteria: list[str],
                spend_usd: float) -> str:
        """PR body templated from the frozen spec metadata (§ Phase 3 task 2)."""
        lines = [
            "## Intent",
            "",
            intent.strip(),
            "",
            "## Tasks",
            "",
            *(f"- {t}" for t in task_lines),
            "",
            "## Success criteria",
            "",
            *(f"- {c}" for c in criteria),
            "",
            "---",
            f"*Generated by Girder · run spend: ${spend_usd:.4f}*",
        ]
        return "\n".join(lines)

    async def open_pr(
        self, *, head: str, base: str, title: str, body: str
    ) -> int:
        """Open a PR ``head`` → ``base``; returns the PR number (idempotent-ish:
        an existing PR for the same head/base is returned instead of duplicated)."""
        owner, name = await self.owner_repo()
        existing = await self._request("GET", f"/repos/{owner}/{name}/pulls?head={owner}:{head}")
        for pr in existing.json():
            if pr.get("state") == "open":
                return int(pr["number"])
        resp = await self._request(
            "POST",
            f"/repos/{owner}/{name}/pulls",
            json_body={"title": self._redact_sync(title), "head": head, "base": base,
                       "body": self._redact_sync(body)},
        )
        return int(resp.json()["number"])

    async def get_pr(self, pr_number: int) -> dict[str, Any]:
        owner, name = await self.owner_repo()
        resp = await self._request("GET", f"/repos/{owner}/{name}/pulls/{pr_number}")
        return dict(resp.json())

    async def pr_is_mergeable(self, pr_number: int) -> bool:
        pr = await self.get_pr(pr_number)
        return bool(pr.get("mergeable")) and str(pr.get("state")) == "open"

    async def comment_on_pr(self, pr_number: int, body: str) -> None:
        owner, name = await self.owner_repo()
        await self._request(
            "POST",
            f"/repos/{owner}/{name}/issues/{pr_number}/comments",
            json_body={"body": self._redact_sync(body)},
        )

    # ------------------------------------------------------------------ checks

    async def fetch_checks(self, commit_sha: str) -> list[CheckRun]:
        """All check runs on *commit_sha* (Checks API, impl-plan §6.11).

        Real Actions check runs often carry an empty ``output.summary/text``;
        when a *failed* check's output is empty we fall back to the Actions
        job-logs API (the Actions check-run id doubles as the job id) so the
        persisted excerpt and downstream classification keep their input.
        """
        owner, name = await self.owner_repo()
        resp = await self._request(
            "GET", f"/repos/{owner}/{name}/commits/{commit_sha}/check-runs"
        )
        runs: list[CheckRun] = []
        for item in resp.json().get("check_runs", []):
            output = item.get("output") or {}
            summary = self._redact_sync(output.get("summary") or "")
            text = self._redact_sync(output.get("text") or "")
            check = CheckRun(
                name=str(item.get("name", "unknown")),
                id=str(item["id"]) if item.get("id") is not None else None,
                status=str(item.get("status", "queued")),
                conclusion=item.get("conclusion"),
                url=item.get("html_url"),
                output_summary=summary,
                output_text=text,
            )
            if check.failed and not summary and not text:
                logs = await self._job_logs(check)
                if logs:
                    check = replace(check, output_text=logs)
            runs.append(check)
        return runs

    async def _job_logs(self, check: CheckRun) -> str:
        """Redacted tail of the Actions job logs for *check* ("" when unavailable)."""
        if check.id is None:
            return ""
        owner, name = await self.owner_repo()
        try:
            resp = await self._client.get(
                f"/repos/{owner}/{name}/actions/jobs/{check.id}/logs",
                follow_redirects=True,  # the API 302s to the log blob
            )
        except httpx.TransportError:
            return ""
        if resp.status_code >= 400:
            return ""
        return self._redact_sync(resp.text[-_LOG_TAIL_CHARS:])

    async def fetch_failure_logs(self, commit_sha: str) -> list[CheckRun]:
        """Completed-and-failed checks with their (redacted) output excerpts."""
        return [c for c in await self.fetch_checks(commit_sha) if c.failed]

    # ------------------------------------------------------------------- merge

    async def merge_pr(
        self, pr_number: int, *, method: str | None = None, commit_title: str | None = None
    ) -> MergeOutcome:
        owner, name = await self.owner_repo()
        body: dict[str, Any] = {"merge_method": method or self.settings.github.merge_method}
        if commit_title:
            body["commit_title"] = commit_title
        resp = await self._request(
            "PUT",
            f"/repos/{owner}/{name}/pulls/{pr_number}/merge",
            json_body=body,
            ok=frozenset({405}),  # "not mergeable" — a refusal, not an error
        )
        if resp.status_code == 405:
            return MergeOutcome(False, None, str(resp.json().get("message", "merge refused")))
        payload = resp.json()
        if payload.get("merged"):
            return MergeOutcome(True, payload.get("sha"))
        return MergeOutcome(False, None, str(payload.get("message", "merge rejected")))
