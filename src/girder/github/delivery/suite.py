"""Delivery suite execution + PR-body criteria helpers (split from the
original ``girder.github.delivery`` module, SHA 5ff969586361fac1ce28ade40cc8d7dabf2d62d9).

Everything here is a ``DeliveryEngine`` mixin: the methods read engine
state (``self.settings``/``self.sandbox``/``self.redactor``) exactly as
they did before the split.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from girder.config import project_allows_empty_baseline
from girder.db.models import Run
from girder.specs.validator import SpecValidationError, parse_spec
from girder.gitops.branch import BranchOps
from girder.orchestrator.baseline import empty_suite_accepted, parse_junit_xml
from girder.sandbox.engine import ContainerSpec, SandboxTimeout
from girder.util import run_host_cmd
log = logging.getLogger("girder.github.delivery")

DELIVERY_XML = ".girder-delivery.xml"
SUITE_CMD = [
    "python",
    "-m",
    "pytest",
    "-q",
    f"--junitxml=/workspace/{DELIVERY_XML}",
    "-p",
    "no:cacheprovider",
]
_TAIL_CHARS = 2000
_EXCERPT_CHARS = 4000
_PROPOSAL_PATH = "openspec/proposals/{run_id}.md"


class SuiteMixin:
    """Final-suite execution and proposal-criteria extraction (D4 check 1)."""

    if TYPE_CHECKING:  # attributes provided by DeliveryEngine
        from girder.config import Settings
        from girder.guard.redact import Redactor
        from girder.sandbox.engine import SandboxEngine

        settings: Settings
        sandbox: SandboxEngine
        redactor: Redactor

    async def _final_suite_green(
        self, run: Run, repo_path: Path
    ) -> tuple[bool, str | None, list[str]]:
        """Run the full suite on the run branch in a sandbox (D4 check 1).

        Returns ``(green, redacted tail when red, failing nodeids)`` — the
        nodeids feed baseline/flaky classification when the CI log itself
        yields none (impl-plan §6.11).
        """
        branch_ops = BranchOps(repo_path)
        tip = await branch_ops.run_branch_tip(run.branch)
        if tip is None:
            return False, f"run branch missing: {run.branch}", []
        tmp = Path(tempfile.mkdtemp(prefix="girder-delivery-"))
        worktree = tmp / "wt"
        container: str | None = None
        try:
            await run_host_cmd(
                ["git", "-C", str(repo_path), "worktree", "add", "--detach", str(worktree), tip],
                timeout_s=60,
            )
            # Forward the operator's [sandbox] policy (cf. task_engine._container_spec).
            container = await self.sandbox.start(
                ContainerSpec(
                    name=f"girder-delivery-{run.id[:8]}",
                    image=f"girder-runner:{self.settings.project.stack}",
                    worktree=worktree,
                    network=self.settings.sandbox.network,
                    memory=self.settings.sandbox.memory,
                    cpus=self.settings.sandbox.cpus,
                    pids_limit=self.settings.sandbox.pids_limit,
                )
            )
            exec_res = await self.sandbox.exec(
                container,
                SUITE_CMD,
                timeout_s=float(self.settings.limits.attempt_wallclock_s),
            )
            xml = worktree / DELIVERY_XML
            if exec_res.timed_out:
                return False, "delivery suite exceeded wall-clock limit", []
            if exec_res.exit_code != 0 and not xml.exists():
                return False, self._tail(exec_res.stderr), []
            if not xml.exists():
                return False, "junit report missing after delivery suite", []
            results = parse_junit_xml(xml.read_text(errors="replace"))
            if not results and not empty_suite_accepted(
                exec_res.exit_code, project_allows_empty_baseline(repo_path)
            ):
                return False, "delivery junit report contained zero testcases", []
            red = sorted(
                t for t, r in results.items() if r.status not in ("passed", "skipped")
            )
            tail = self._tail(exec_res.stdout + exec_res.stderr) if red else None
            return (not red), tail, red
        except SandboxTimeout:
            return False, "delivery suite sandbox timeout", []
        finally:
            if container is not None:
                await self.sandbox.kill(container)
            await self._cleanup_worktree(repo_path, worktree)
            shutil.rmtree(tmp, ignore_errors=True)

    def _tail(self, text: str | None) -> str | None:
        if not text:
            return None
        clean, _ = self.redactor.redact(text[-_TAIL_CHARS:])
        return clean

    async def _criteria_of(self, run: Run, repo_path: Path) -> list[str]:
        """Success criteria from the frozen proposal (best effort, PR body only)."""
        path = _PROPOSAL_PATH.format(run_id=run.id)
        proc = await run_host_cmd(
            ["git", "-C", str(repo_path), "show", f"{run.branch}:{path}"],
            check=False,
            timeout_s=30,
        )
        if proc.returncode != 0:
            return []
        try:
            spec = parse_spec(proc.stdout)
        except SpecValidationError:
            return []
        return [c for t in spec.tasks for c in t.success_criteria]

    async def _cleanup_worktree(self, repo_path: Path, worktree: Path) -> None:
        try:
            await run_host_cmd(
                ["git", "-C", str(repo_path), "worktree", "remove", "--force", str(worktree)],
                timeout_s=60,
            )
            await run_host_cmd(["git", "-C", str(repo_path), "worktree", "prune"], timeout_s=60)
        except Exception as exc:  # backstop: tempdir rmtree
            log.warning("delivery worktree cleanup failed for %s: %s", worktree, exc)
