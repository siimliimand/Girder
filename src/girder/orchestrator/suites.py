"""Shared verification-suite plumbing for the orchestrator.

Both the wave integrator (per-merge semantic gate) and the conflict resolver
(mandatory full re-test) run the same junit-parsed suite inside a container —
WP 5.3's "full local suite after each merge" is one code path, not two.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from girder.guard.redact import Redactor
from girder.orchestrator.baseline import parse_junit_xml
from girder.orchestrator.task_engine import _VERIFY_TIMEOUT_S, VERIFY_CMD, VERIFY_XML
from girder.sandbox.engine import SandboxEngine

_TAIL_CHARS = 2000


@dataclass(frozen=True)
class SuiteResult:
    green: bool
    tail: str
    test_count: int
    exit_code: int


async def run_suite_in_container(
    sandbox: SandboxEngine,
    container: str,
    worktree_path: Path,
    redactor: Redactor,
) -> SuiteResult:
    """Run the full project suite inside *container* and parse its junit report."""
    exec_res = await sandbox.exec(container, VERIFY_CMD, timeout_s=_VERIFY_TIMEOUT_S)
    xml_path = worktree_path / VERIFY_XML
    green = exec_res.exit_code == 0 and xml_path.is_file()
    test_count = 0
    if xml_path.is_file():
        try:
            test_count = len(parse_junit_xml(xml_path.read_text()))
            green = green and test_count > 0
        except Exception:
            green = False
        # Orchestrator plumbing, not task output: remove so later audits see
        # a clean tree.
        xml_path.unlink(missing_ok=True)
    tail = redactor.redact((exec_res.stdout + "\n" + exec_res.stderr)[-_TAIL_CHARS:])[0]
    return SuiteResult(green=green, tail=tail, test_count=test_count, exit_code=exec_res.exit_code)
