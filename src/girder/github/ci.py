"""CI checks poller (impl-plan §6.11 / plan.md Phase 3 task 3, Sprint 4 WP 4.3).

Polls the Checks API for a run's head commit until every check settles.
Every observed check is persisted (status, conclusion, url and a redacted
tail of the log excerpt — §8.6) so the diagnostic agent and the post-mortem
views work from stored data, never from a live API. This module never
transitions run/task/attempt state: it returns snapshots and outcomes; the
delivery engine decides what a red or timed-out poll means.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field

from girder.config import Settings
from girder.db import repo
from girder.db.engine import Database
from girder.db.models import Run
from girder.github.client import CheckRun, GitHubClient
from girder.guard.redact import Redactor

log = logging.getLogger(__name__)

#: Reported as the sole pending "check" when the API returns zero check runs —
#: an empty report must never be mistaken for green.
NO_CHECKS_NAME = "(no checks reported)"

_TAIL_CHARS = 2000


class CiTimeout(RuntimeError):
    """Checks did not settle within the poll budget — callers escalate."""


@dataclass(frozen=True)
class CiSnapshot:
    """One observation of the Checks API for a head commit."""

    checks: list[CheckRun]
    pending: list[str] = field(default_factory=list)  # names of unsettled checks
    failed: list[CheckRun] = field(default_factory=list)  # completed AND failed

    @property
    def green(self) -> bool:
        return not self.pending and not self.failed

    @property
    def settled(self) -> bool:
        return not self.pending


# pytest nodeids in CI logs: tests/test_a.py::test_x, …::Class::test_y[param]
_NODEID_RE = re.compile(r"(?<![\w./-])[A-Za-z0-9_./-]+\.py(?:::[A-Za-z0-9_./\-[\]-]+)+")


def extract_test_ids(*texts: str | None) -> list[str]:
    """Pull pytest nodeids out of arbitrary CI log text.

    Deduplicated, order-preserving. Used to classify CI failures against the
    baseline and the flake registry.
    """
    ids: list[str] = []
    for text in texts:
        if not text:
            continue
        for match in _NODEID_RE.findall(text):
            if match not in ids:
                ids.append(match)
    return ids


class CiPoller:
    def __init__(
        self,
        *,
        db: Database,
        client: GitHubClient,
        settings: Settings,
        redactor: Redactor,
    ) -> None:
        self.db = db
        self.client = client
        self.settings = settings
        self.redactor = redactor

    # -------------------------------------------------------------- observation

    async def observe(self, *, run: Run, head_sha: str) -> CiSnapshot:
        """One poll: fetch, persist and classify the checks on *head_sha*."""
        checks = await self.client.fetch_checks(head_sha)
        failed = [c for c in checks if c.failed]
        if checks:
            pending = [c.name for c in checks if not c.completed]
        else:
            pending = [NO_CHECKS_NAME]
        snapshot = CiSnapshot(checks=checks, pending=pending, failed=failed)

        for check in checks:
            await repo.insert_ci_check_result(
                self.db,
                run.id,
                check.name,
                check.status,
                conclusion=check.conclusion,
                url=check.url,
                log_excerpt_redacted=self._excerpt(check),
            )
        await repo.insert_event(
            self.db,
            "ci_observed",
            {
                "head_sha": head_sha,
                "total": len(checks),
                "pending": list(pending),
                "failed": [c.name for c in failed],
            },
            run_id=run.id,
        )
        return snapshot

    def _excerpt(self, check: CheckRun) -> str | None:
        """Redacted tail of the check's output (§8.6 — excerpts only)."""
        combined = "\n".join(x for x in (check.output_summary, check.output_text) if x)
        if not combined.strip():
            return None
        tail = combined[-_TAIL_CHARS:]
        clean, _ = self.redactor.redact(tail)
        return clean

    # ------------------------------------------------------------------ waiting

    async def wait_for_settled(
        self,
        *,
        run: Run,
        head_sha: str,
        interval_s: float | None = None,
        timeout_s: float | None = None,
    ) -> CiSnapshot:
        """Poll :meth:`observe` until the snapshot settles; raise CiTimeout
        after the budget — the caller escalates, we never poll forever."""
        interval = interval_s if interval_s is not None else self.settings.github.poll_interval_s
        timeout = timeout_s if timeout_s is not None else self.settings.github.poll_timeout_s
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            snapshot = await self.observe(run=run, head_sha=head_sha)
            if snapshot.settled:
                return snapshot
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise CiTimeout(
                    f"CI checks on {head_sha[:12]} did not settle within {timeout:.0f}s"
                    f" (pending: {snapshot.pending})"
                )
            await asyncio.sleep(min(interval, remaining))
