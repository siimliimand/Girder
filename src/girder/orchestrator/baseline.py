"""Baseline pre-flight check (impl-plan Phase 2 task 1, §4.3, Sprint 3 WP 3.3).

Runs the full test suite on a pristine detached worktree of ``base_commit``
inside a sandbox before the agent loop starts. Failing tests are rerun once,
individually: pass-on-rerun ⇒ flaky (recorded in the flake registry),
fail-again (or never rerun) ⇒ broken — a broken baseline means the repo was
already red before the agent touched it, so the run must never enter the
agent loop.
"""

from __future__ import annotations

import json
import logging
import shutil
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from girder.config import Settings
from girder.db import repo
from girder.db.engine import Database
from girder.db.models import Project, Run
from girder.guard.redact import Redactor
from girder.sandbox.engine import ContainerSpec, SandboxEngine, SandboxTimeout
from girder.util import run_host_cmd

log = logging.getLogger(__name__)

SUITE_XML = ".girder-baseline.xml"
RERUN_XML = ".girder-rerun.xml"
_TAIL_CHARS = 2000

# JUnit failure/child element -> normalized status.
_CHILD_STATUS = {"failure": "failed", "error": "error", "skipped": "skipped"}


@dataclass(frozen=True)
class PerTestResult:
    test_id: str  # relative to the worktree, e.g. "tests/test_a.py::TestA::test_x"
    status: str  # "passed" | "failed" | "error" | "skipped"
    rerun_status: str | None = None
    flaky: bool = False


@dataclass(frozen=True)
class BaselineOutcome:
    baseline_run_id: str
    commit_sha: str
    total: int = 0
    passed: int = 0
    failed: int = 0  # failing (non-skipped) tests before rerun
    flaky_ids: list[str] = field(default_factory=list)
    broken_ids: list[str] = field(default_factory=list)
    broken: bool = False  # non-flaky failure ⇒ never enter the agent loop
    infra_error: str | None = None  # container/pytest-missing/timeout — baseline unavailable
    output_tail_redacted: str | None = None


def normalize_test_id(classname: str | None, name: str, file: str | None) -> str:
    """JUnit testcase attrs -> pytest nodeid ``path.py::Class::test[params]``.

    Without a ``file`` attribute the module path is reconstructed from the
    dotted classname: trailing components that look like classes (uppercase
    initial) become ``::`` segments, the rest is the module path.
    """
    class_parts: list[str] = []
    if classname:
        parts = classname.split(".")
        # Trailing components with an uppercase initial are the class chain;
        # the rest is the module path (used only when ``file`` is missing).
        while parts and parts[-1][:1].isupper():
            class_parts.insert(0, parts.pop())
        if file is None:
            file = "/".join(parts) + ".py" if parts else "unknown.py"
    node_id = file or "unknown.py"
    for part in class_parts:
        node_id += f"::{part}"
    return f"{node_id}::{name}"


def _testcase_status(tc: ET.Element) -> str:
    for child in tc:
        tag = child.tag.rsplit("}", 1)[-1]
        if tag in _CHILD_STATUS:
            return _CHILD_STATUS[tag]
    return "passed"


def parse_junit_xml(text: str) -> dict[str, PerTestResult]:
    """Parse a JUnit XML report into ``{test_id: PerTestResult}``.

    Handles both a bare ``<testsuite>`` root and the ``<testsuites>`` wrapper.
    Zero testcases (e.g. collection errors) yields an empty dict — callers
    treat that as an infrastructure failure.
    """
    root = ET.fromstring(text)
    suites = [root] if root.tag.rsplit("}", 1)[-1] == "testsuite" else list(root.iter("testsuite"))
    results: dict[str, PerTestResult] = {}
    for suite in suites:
        for tc in suite.iter("testcase"):
            name = tc.get("name")
            if not name:
                continue
            test_id = normalize_test_id(tc.get("classname"), name, tc.get("file"))
            results[test_id] = PerTestResult(test_id=test_id, status=_testcase_status(tc))
    return results


def _redacted_tail(redactor: Redactor, text: str | None) -> str | None:
    if not text:
        return None
    tail = text[-_TAIL_CHARS:]
    redacted, _ = redactor.redact(tail)
    return redacted


async def _cleanup_worktree(repo_path: Path, worktree: Path) -> None:
    try:
        await run_host_cmd(
            ["git", "-C", str(repo_path), "worktree", "remove", "--force", str(worktree)],
            timeout_s=60,
        )
        await run_host_cmd(["git", "-C", str(repo_path), "worktree", "prune"], timeout_s=60)
    except Exception as exc:  # cleanup best-effort; the tempdir rmtree is the backstop
        log.warning("baseline worktree cleanup failed for %s: %s", worktree, exc)


class BaselineRunner:
    def __init__(
        self,
        *,
        db: Database,
        sandbox: SandboxEngine,
        settings: Settings,
        redactor: Redactor,
        image: str | None = None,
        suite_cmd: list[str] | None = None,
    ) -> None:
        self.db = db
        self.sandbox = sandbox
        self.settings = settings
        self.redactor = redactor
        self.image = image or f"girder-runner:{settings.project.stack}"
        self.suite_cmd = suite_cmd or [
            "python",
            "-m",
            "pytest",
            "-q",
            f"--junitxml=/workspace/{SUITE_XML}",
            "-p",
            "no:cacheprovider",
        ]

    async def run(
        self, *, project: Project, run: Run, repo_path: Path, base_commit: str
    ) -> BaselineOutcome:
        await repo.insert_event(
            self.db,
            "baseline_started",
            {"project_id": project.id, "commit_sha": base_commit, "image": self.image},
            run_id=run.id,
        )
        tmp = Path(tempfile.mkdtemp(prefix="girder-baseline-"))
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
                    base_commit,
                ],
                timeout_s=60,
            )
            container = await self.sandbox.start(
                ContainerSpec(
                    name=f"girder-baseline-{run.id[:8]}",
                    image=self.image,
                    worktree=worktree,
                    network="none",
                )
            )
            return await self._run_suite(project, run, base_commit, container, worktree)
        except SandboxTimeout as exc:
            return await self._infra(project, run, base_commit, f"sandbox timeout: {exc}")
        except Exception as exc:  # any failure ⇒ baseline unavailable, caller escalates
            log.warning("baseline infra failure: %s", exc)
            return await self._infra(project, run, base_commit, f"{type(exc).__name__}: {exc}")
        finally:
            if container is not None:
                await self.sandbox.kill(container)
            await _cleanup_worktree(repo_path, worktree)
            shutil.rmtree(tmp, ignore_errors=True)

    # ------------------------------------------------------------------ phases

    async def _run_suite(
        self, project: Project, run: Run, base_commit: str, container: str, worktree: Path
    ) -> BaselineOutcome:
        exec_res = await self.sandbox.exec(
            container, self.suite_cmd, timeout_s=float(self.settings.limits.attempt_wallclock_s)
        )
        suite_xml = worktree / SUITE_XML
        if exec_res.timed_out:
            return await self._infra(
                project, run, base_commit, "full suite exceeded wall-clock limit", exec_res.stderr
            )
        if exec_res.exit_code != 0 and not suite_xml.exists():
            # No report ⇒ pytest never ran (missing interpreter/plugin, collection crash).
            return await self._infra(
                project,
                run,
                base_commit,
                f"suite exited {exec_res.exit_code} with no junit report",
                exec_res.stderr,
            )
        if not suite_xml.exists():
            return await self._infra(
                project, run, base_commit, "junit report missing after successful suite"
            )
        results = parse_junit_xml(suite_xml.read_text(errors="replace"))
        if not results:
            return await self._infra(
                project, run, base_commit, "junit report contained zero testcases", exec_res.stderr
            )

        # Rerun each failing test in isolation; pass-on-rerun ⇒ flaky.
        for test_id, result in results.items():
            if result.status == "passed" or result.status == "skipped":
                continue
            rerun_status = await self._rerun_one(container, worktree, test_id)
            flaky = rerun_status == "passed"
            results[test_id] = PerTestResult(
                test_id=test_id,
                status=result.status,
                rerun_status=rerun_status,
                flaky=flaky,
            )

        flaky_ids = sorted(t for t, r in results.items() if r.flaky)
        broken_ids = sorted(
            t for t, r in results.items() if r.status not in ("passed", "skipped") and not r.flaky
        )
        # A broken id already in the flake registry stays broken: baseline
        # truth on this exact commit wins over the historical registry.
        baseline = await repo.create_baseline_run(
            self.db,
            project.id,
            base_commit,
            json.dumps(
                {
                    t: {"status": r.status, "rerun_status": r.rerun_status, "flaky": r.flaky}
                    for t, r in results.items()
                }
            ),
        )
        for test_id in flaky_ids:
            await repo.upsert_flaky_test(self.db, project.id, test_id, run_id=run.id)
        passed = sum(1 for r in results.values() if r.status == "passed")
        failed = sum(1 for r in results.values() if r.status in ("failed", "error"))
        await repo.insert_event(
            self.db,
            "baseline_completed",
            {
                "project_id": project.id,
                "commit_sha": base_commit,
                "baseline_run_id": baseline.id,
                "total": len(results),
                "passed": passed,
                "failed": failed,
                "flaky": len(flaky_ids),
                "broken": len(broken_ids),
            },
            run_id=run.id,
        )
        return BaselineOutcome(
            baseline_run_id=baseline.id,
            commit_sha=base_commit,
            total=len(results),
            passed=passed,
            failed=failed,
            flaky_ids=flaky_ids,
            broken_ids=broken_ids,
            broken=bool(broken_ids),
        )

    async def _rerun_one(self, container: str, worktree: Path, test_id: str) -> str | None:
        """Rerun one test; returns its status, or None when unparseable (⇒ broken)."""
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
            return None  # conservative: no report ⇒ treat as broken
        parsed = parse_junit_xml(rerun_xml.read_text(errors="replace"))
        if test_id in parsed:
            return parsed[test_id].status
        # Same test, possibly a different id spelling (parametrize rendering):
        # any single passed testcase counts as passing the rerun.
        if len(parsed) == 1:
            return next(iter(parsed.values())).status
        return None

    async def _infra(
        self, project: Project, run: Run, base_commit: str, message: str, stderr: str | None = None
    ) -> BaselineOutcome:
        tail = _redacted_tail(self.redactor, stderr)
        await repo.insert_event(
            self.db,
            "baseline_completed",
            {"project_id": project.id, "commit_sha": base_commit, "infra_error": message},
            run_id=run.id,
        )
        return BaselineOutcome(
            baseline_run_id="",
            commit_sha=base_commit,
            infra_error=message,
            output_tail_redacted=tail,
        )
