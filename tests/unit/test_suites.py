"""Unit tests for the shared in-container suite gate (orchestrator.suites).

FakeSandbox scripts the suite exec and materializes the junit report on the
host-visible worktree path — same style as the baseline unit tests."""

from __future__ import annotations

from pathlib import Path

from girder.guard.redact import Redactor
from girder.orchestrator.suites import run_suite_in_container
from girder.sandbox.engine import ExecResult

GREEN_XML = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<testsuite name="pytest" tests="1">'
    '<testcase classname="tests.test_a" name="test_one" file="tests/test_a.py"/>'
    "</testsuite>"
)
EMPTY_XML = '<?xml version="1.0" encoding="utf-8"?><testsuite name="pytest" tests="0"></testsuite>'


class FakeSandbox:
    """One scripted exec response; writes the suite xml into the worktree."""

    def __init__(self, result: ExecResult, xml: str, worktree: Path) -> None:
        self.result = result
        self.xml = xml
        self.worktree = worktree

    async def exec(
        self, name: str, cmd: list[str], *, timeout_s: float = 120.0, user: str | None = None
    ) -> ExecResult:
        report = next((a.split("=", 1)[1] for a in cmd if a.startswith("--junitxml=")), None)
        assert report is not None
        # the in-container report path is absolute (/workspace/...); translate
        # to the host-visible worktree like ScriptSandbox does
        (self.worktree / report.removeprefix("/workspace/")).write_text(self.xml)
        return self.result


async def _run(sandbox: FakeSandbox, tmp_path: Path, *, allow_empty: bool):
    return await run_suite_in_container(
        sandbox,  # type: ignore[arg-type]
        "ctr",
        tmp_path,
        Redactor(),
        allow_empty_baseline=allow_empty,
    )


async def test_zero_testcase_suite_is_red_by_default(tmp_path: Path) -> None:
    sandbox = FakeSandbox(ExecResult(5, "", ""), EMPTY_XML, tmp_path)
    result = await _run(sandbox, tmp_path, allow_empty=False)
    assert result.green is False
    assert result.test_count == 0


async def test_allow_empty_makes_exit5_empty_suite_green(tmp_path: Path) -> None:
    sandbox = FakeSandbox(ExecResult(5, "", ""), EMPTY_XML, tmp_path)
    result = await _run(sandbox, tmp_path, allow_empty=True)
    assert result.green is True
    assert result.test_count == 0


async def test_broken_collection_exit_stays_red_with_flag(tmp_path: Path) -> None:
    sandbox = FakeSandbox(ExecResult(2, "", "conftest exploded"), EMPTY_XML, tmp_path)
    result = await _run(sandbox, tmp_path, allow_empty=True)
    assert result.green is False


async def test_normal_green_suite_unaffected_by_flag(tmp_path: Path) -> None:
    sandbox = FakeSandbox(ExecResult(0, "", ""), GREEN_XML, tmp_path)
    result = await _run(sandbox, tmp_path, allow_empty=True)
    assert result.green is True
    assert result.test_count == 1


async def test_report_file_removed_after_run(tmp_path: Path) -> None:
    """The junit report is orchestrator plumbing — it must not linger in the
    tree later audits observe."""
    sandbox = FakeSandbox(ExecResult(0, "", ""), GREEN_XML, tmp_path)
    await _run(sandbox, tmp_path, allow_empty=False)
    assert not list(tmp_path.glob(".girder-*.xml"))  # noqa: ASYNC240 - tmp_path probe
