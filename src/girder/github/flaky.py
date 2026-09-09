"""Baseline-aware flaky circuit breaker (impl-plan §6.11 / plan.md Phase 3 task 4,
Sprint 4 WP 4.3).

When CI is red, each failing test id is rerun individually in a sandboxed
container against a pristine detached worktree of the head commit:

* **red-on-rerun** — pre-existing on the baseline ⇒ ``baseline_broken``
  (never "fixed"; escalate), otherwise ``real_failure`` (diagnose).
* **green-on-rerun** — flaky in ``baseline_runs``/``flaky_tests`` ⇒
  ``known_flaky`` (registry upserted); *not* flaky anywhere ⇒
  ``regression``: the agent's diff introduced nondeterminism — escalate at
  every tier, never silently tag flaky (SC-10).

Uncertainty is conservative: a timeout or an unparseable junit report counts
as red-on-rerun. This module never transitions run state; it returns
classifications and persists an event — escalation is the caller's job.
"""

from __future__ import annotations

import json
import logging
import shutil
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from girder.config import Settings
from girder.db import repo
from girder.db.engine import Database
from girder.db.models import Project, Run
from girder.guard.redact import Redactor
from girder.orchestrator.baseline import parse_junit_xml
from girder.sandbox.engine import ContainerSpec, SandboxEngine, SandboxTimeout
from girder.util import run_host_cmd

log = logging.getLogger(__name__)

RERUN_XML = ".girder-flaky-rerun.xml"

VERDICT_REAL_FAILURE = "real_failure"
VERDICT_KNOWN_FLAKY = "known_flaky"
VERDICT_REGRESSION = "regression"
VERDICT_BASELINE_BROKEN = "baseline_broken"


@dataclass(frozen=True)
class FlakeClassification:
    test_id: str
    verdict: str
    detail: str | None = None


class FlakyBreaker:
    def __init__(
        self,
        *,
        db: Database,
        sandbox: SandboxEngine,
        settings: Settings,
        redactor: Redactor,
        image: str | None = None,
    ) -> None:
        self.db = db
        self.sandbox = sandbox
        self.settings = settings
        self.redactor = redactor
        self.image = image or f"girder-runner:{settings.project.stack}"

    async def classify(
        self,
        *,
        project: Project,
        run: Run,
        test_ids: list[str],
        commit_sha: str,
        repo_path: Path,
    ) -> list[FlakeClassification]:
        """Classify each failing *test_id* by rerunning it on a pristine
        worktree of *commit_sha* (impl-plan §6.11)."""
        if not test_ids:
            return []

        registry = {f.test_id for f in await repo.list_flaky_tests(self.db, project.id)}
        baseline_entry: dict[str, dict[str, object]] = {}
        if run.baseline_run_id:
            baseline = await repo.get_baseline_run(self.db, run.baseline_run_id)
            if baseline is not None:
                baseline_entry = json.loads(baseline.per_test_json)

        tmp = Path(tempfile.mkdtemp(prefix="girder-flaky-"))
        worktree = tmp / "wt"
        container: str | None = None
        try:
            await run_host_cmd(
                [
                    "git",
                    "-C",
                    str(repo_path),
                    "worktree",
                    "add",
                    "--detach",
                    str(worktree),
                    commit_sha,
                ],
                timeout_s=60,
            )
            container = await self.sandbox.start(
                ContainerSpec(
                    name=f"girder-flaky-{run.id[:8]}",
                    image=self.image,
                    worktree=worktree,
                    network="none",
                )
            )
            classifications: list[FlakeClassification] = []
            for test_id in test_ids:
                rerun_status = await self._rerun_one(container, worktree, test_id)
                classification = self._verdict(
                    test_id,
                    rerun_status,
                    baseline_entry.get(test_id),
                    registry,
                )
                if classification.verdict == VERDICT_KNOWN_FLAKY:
                    await repo.upsert_flaky_test(self.db, project.id, test_id, run_id=run.id)
                classifications.append(classification)
            await repo.insert_event(
                self.db,
                "flaky_classification",
                {
                    "commit_sha": commit_sha,
                    "classifications": [
                        {
                            "test_id": c.test_id,
                            "verdict": c.verdict,
                            "detail": c.detail,
                        }
                        for c in classifications
                    ],
                },
                run_id=run.id,
            )
            return classifications
        finally:
            if container is not None:
                await self.sandbox.kill(container)
            await _cleanup_worktree(repo_path, worktree)
            shutil.rmtree(tmp, ignore_errors=True)

    # ------------------------------------------------------------- classification

    def _verdict(
        self,
        test_id: str,
        rerun_status: str | None,
        entry: dict[str, object] | None,
        registry: set[str],
    ) -> FlakeClassification:
        green = rerun_status == "passed"
        if green:
            if bool(entry and entry.get("flaky")) or test_id in registry:
                return FlakeClassification(
                    test_id,
                    VERDICT_KNOWN_FLAKY,
                    "green on rerun; flaky in baseline or registry",
                )
            return FlakeClassification(
                test_id,
                VERDICT_REGRESSION,
                "green on rerun but not known flaky — diff introduced nondeterminism",
            )
        pre_existing = bool(
            entry
            and entry.get("status") in ("failed", "error")
            and not entry.get("flaky")
        )
        if pre_existing:
            return FlakeClassification(
                test_id, VERDICT_BASELINE_BROKEN, "red on rerun; already failing on baseline"
            )
        if green is False and rerun_status is None:
            detail = "red on rerun (conservative: timeout or unparseable report)"
        else:
            detail = "red on rerun; not failing on baseline"
        return FlakeClassification(test_id, VERDICT_REAL_FAILURE, detail)

    # -------------------------------------------------------------------- reruns

    async def _rerun_one(self, container: str, worktree: Path, test_id: str) -> str | None:
        """Rerun one test; its junit status, or None when uncertain (⇒ red)."""
        rerun_xml = worktree / RERUN_XML
        rerun_xml.unlink(missing_ok=True)
        try:
            await self.sandbox.exec(
                container,
                [
                    "python",
                    "-m",
                    "pytest",
                    "-q",
                    test_id,
                    "-p",
                    "no:cacheprovider",
                    f"--junitxml=/workspace/{RERUN_XML}",
                ],
                timeout_s=float(self.settings.limits.attempt_wallclock_s),
            )
        except SandboxTimeout:
            return None
        if not rerun_xml.exists():
            return None  # conservative: no report ⇒ treat as red
        try:
            parsed = parse_junit_xml(rerun_xml.read_text(errors="replace"))
        except ET.ParseError:
            return None
        if test_id in parsed:
            return parsed[test_id].status
        if len(parsed) == 1:
            # Same test, different id spelling (parametrize rendering).
            return next(iter(parsed.values())).status
        return None


async def _cleanup_worktree(repo_path: Path, worktree: Path) -> None:
    try:
        await run_host_cmd(
            ["git", "-C", str(repo_path), "worktree", "remove", "--force", str(worktree)],
            timeout_s=60,
        )
        await run_host_cmd(["git", "-C", str(repo_path), "worktree", "prune"], timeout_s=60)
    except Exception as exc:  # cleanup best-effort; the tempdir rmtree is the backstop
        log.warning("flaky worktree cleanup failed for %s: %s", worktree, exc)
