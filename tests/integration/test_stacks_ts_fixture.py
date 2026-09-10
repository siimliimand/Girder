"""WS-07A integration: the TS fixture runs under NodePlugin's verify command.

SC-20 prerequisite (ws07 WP 11.2): ``NodePlugin.test_command()`` executed in
the ``tests/fixtures/e2e-target-ts`` tree produces JUnit XML at
``.girder-verify.xml`` that the orchestrator's ``parse_junit_xml`` accepts,
with all baseline tests passing. The full SC-20 e2e (live-model scripted run
to merged status) still needs the jest toolchain wired into the e2e harness
sandboxes; this covers the stack-plugin half of that scenario for real.

Requires ``npx`` on PATH and network for ``npm install`` — skipped otherwise.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from girder.orchestrator.baseline import parse_junit_xml
from girder.stacks import get_stack

pytestmark = pytest.mark.integration

FIXTURE_TS = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "e2e-target-ts"
VERIFY_XML = ".girder-verify.xml"


def _npx_usable() -> bool:
    if shutil.which("npx") is None:
        return False
    probe = subprocess.run(["npx", "--version"], capture_output=True, text=True, timeout=60)
    return probe.returncode == 0


@pytest.fixture
def ts_repo(tmp_path: Path) -> Path:
    if not _npx_usable():
        pytest.skip("host npx unavailable")
    repo_path = tmp_path / "repo"
    shutil.copytree(FIXTURE_TS, repo_path)
    install = subprocess.run(
        ["npm", "install", "--no-audit", "--no-fund"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if install.returncode != 0:
        pytest.skip(f"npm install failed (no network?): {install.stderr[-300:]}")
    return repo_path


def test_node_plugin_verify_suite_on_ts_fixture(ts_repo: Path) -> None:
    plugin = get_stack("node-20")
    env = dict(os.environ)
    # The runner image bakes this ENV (Dockerfile.node-20); the local harness
    # equivalent sets it explicitly for the same effect.
    env["JEST_JUNIT_OUTPUT_FILE"] = str(ts_repo / VERIFY_XML)
    proc = subprocess.run(
        plugin.test_command(),
        cwd=ts_repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    xml = (ts_repo / VERIFY_XML).read_text()
    results = parse_junit_xml(xml)
    assert len(results) == 3
    assert all(r.status == "passed" for r in results.values())
