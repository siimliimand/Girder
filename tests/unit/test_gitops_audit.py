"""DiffAudit — mechanical test protection layers 2 & 3 (§8.2), real git repos."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from girder.db.models import TaskType
from girder.gitops.audit import DiffAudit, GitOpsError
from girder.gitops.audit import TestManifest as Manifest
from girder.util import run_host_cmd

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")

SIGNALS = ["tests/**", "**/test_*.py", "**/conftest.py", "**/mocks/**"]
PROTECTED = [".github/**"]


async def _git(cwd: Path, *args: str) -> str:
    result = await run_host_cmd(["git", "-C", str(cwd), *args], timeout_s=30)
    return result.stdout


@pytest.fixture
async def repo(tmp_path: Path) -> Path:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    await _git(repo_dir, "init", "-b", "main")
    await _git(repo_dir, "config", "user.email", "t@t")
    await _git(repo_dir, "config", "user.name", "t")
    (repo_dir / "app.py").write_text("x = 1\n")
    (repo_dir / "tests").mkdir()
    (repo_dir / "tests" / "test_app.py").write_text("def test_a(): pass\n")
    (repo_dir / ".github" / "workflows").mkdir(parents=True)
    (repo_dir / ".github" / "workflows" / "ci.yml").write_text("on: push\n")
    await _git(repo_dir, "add", "-A")
    await _git(repo_dir, "commit", "-m", "initial")
    return repo_dir


def _audit() -> DiffAudit:
    return DiffAudit(test_signal_patterns=SIGNALS, protected_read_paths=PROTECTED)


async def _commit_all(repo_dir: Path, msg: str) -> str:
    await _git(repo_dir, "add", "-A")
    await _git(repo_dir, "commit", "-m", msg)
    return (await _git(repo_dir, "rev-parse", "HEAD")).strip()


async def test_manifest_deterministic_and_sensitive(tmp_path: Path, repo: Path) -> None:
    audit = _audit()
    m1 = await audit.capture_test_manifest(repo)
    m2 = await audit.capture_test_manifest(repo)
    assert m1.root_hash == m2.root_hash
    assert set(m1.files) == {"tests/test_app.py"}
    (repo / "tests" / "test_app.py").write_text("def test_b(): pass\n")
    m3 = await audit.capture_test_manifest(repo)
    assert m3.root_hash != m1.root_hash
    assert m3.files["tests/test_app.py"] != m1.files["tests/test_app.py"]


async def test_manifest_ignores_non_test_files(tmp_path: Path, repo: Path) -> None:
    (repo / "app.py").write_text("x = 2\n")
    manifest = await _audit().capture_test_manifest(repo)
    assert "app.py" not in manifest.files


async def test_happy_path_scoped_change(repo: Path) -> None:
    base = (await _git(repo, "rev-parse", "HEAD")).strip()
    (repo / "app.py").write_text("x = 3\n")
    await _commit_all(repo, "change app")
    result = await _audit().audit_attempt(
        repo, task_type=TaskType.CODE_CHANGE, scope_globs=["src/**", "app.py"], base_commit=base
    )
    assert result.passed, result


async def test_test_path_violation_on_code_change(repo: Path) -> None:
    base = (await _git(repo, "rev-parse", "HEAD")).strip()
    (repo / "tests" / "test_app.py").write_text("def test_a(): assert False\n")
    await _commit_all(repo, "weaken test")
    result = await _audit().audit_attempt(
        repo, task_type=TaskType.CODE_CHANGE, scope_globs=["**"], base_commit=base
    )
    assert not result.passed
    assert result.test_path_violations == ["tests/test_app.py"]


async def test_content_hash_mismatch_outside_test_dirs(repo: Path) -> None:
    """SC-03: a new test-like file matching a signal pattern but outside tests/."""
    base = (await _git(repo, "rev-parse", "HEAD")).strip()
    manifest = await _audit().capture_test_manifest(repo)
    (repo / "conftest.py").write_text("# sneaky fixture override\n")
    await _commit_all(repo, "add root conftest")
    result = await _audit().audit_attempt(
        repo,
        task_type=TaskType.CODE_CHANGE,
        scope_globs=["**"],
        base_commit=base,
        start_manifest=manifest,
    )
    assert not result.passed
    assert result.content_hash_mismatches == ["conftest.py"]


async def test_out_of_scope_write(repo: Path) -> None:
    base = (await _git(repo, "rev-parse", "HEAD")).strip()
    (repo / "app.py").write_text("x = 4\n")
    await _commit_all(repo, "change app")
    result = await _audit().audit_attempt(
        repo, task_type=TaskType.CODE_CHANGE, scope_globs=["tests/**"], base_commit=base
    )
    assert not result.passed
    assert result.out_of_scope_writes == ["app.py"]


async def test_protected_path_flagged_even_on_test_change(repo: Path) -> None:
    base = (await _git(repo, "rev-parse", "HEAD")).strip()
    (repo / ".github" / "workflows" / "ci.yml").write_text("on: [push, pull_request]\n")
    (repo / "tests" / "test_app.py").write_text("def test_c(): pass\n")
    await _commit_all(repo, "touch ci + test")
    result = await _audit().audit_attempt(
        repo, task_type=TaskType.TEST_CHANGE, scope_globs=["**"], base_commit=base
    )
    assert not result.passed
    assert result.protected_path_touches == [".github/workflows/ci.yml"]
    assert result.test_path_violations == []


async def test_uncommitted_leftovers_fail_audit(repo: Path) -> None:
    base = (await _git(repo, "rev-parse", "HEAD")).strip()
    (repo / "app.py").write_text("x = 5\n")
    (repo / "stray.txt").write_text("untracked\n")
    result = await _audit().audit_attempt(
        repo, task_type=TaskType.CODE_CHANGE, scope_globs=["**"], base_commit=base
    )
    assert not result.passed
    assert sorted(result.uncommitted_leftovers) == ["app.py", "stray.txt"]


async def test_snapshot_immune_to_later_edits(repo: Path, tmp_path: Path) -> None:
    dest = tmp_path / "snapshot"
    out = await _audit().materialize_test_snapshot(repo, "HEAD", ["tests/test_app.py"], dest)
    assert out == dest
    assert (dest / "tests" / "test_app.py").read_text() == "def test_a(): pass\n"
    (repo / "tests" / "test_app.py").write_text("def test_hacked(): pass\n")
    assert (dest / "tests" / "test_app.py").read_text() == "def test_a(): pass\n"


async def test_snapshot_invalid_path_raises(repo: Path, tmp_path: Path) -> None:
    with pytest.raises(GitOpsError):
        await _audit().materialize_test_snapshot(
            repo, "HEAD", ["no/such/path.py"], tmp_path / "dest"
        )


# --- commit-level audit (Sprint 5: integrator gate, no worktree) ---


async def test_manifest_from_commit_matches_capture(repo: Path, tmp_path: Path) -> None:
    commit = (await _git(repo, "rev-parse", "HEAD")).strip()
    from_commit = await _audit().manifest_from_commit(repo, commit)
    from_worktree = await _audit().capture_test_manifest(repo)
    assert from_commit.root_hash == from_worktree.root_hash
    assert from_commit.files == from_worktree.files


async def test_audit_commit_test_path_violation(repo: Path) -> None:
    base = (await _git(repo, "rev-parse", "HEAD")).strip()
    (repo / "tests" / "test_app.py").write_text("def test_a(): assert False\n")
    head = await _commit_all(repo, "weaken test")
    result = await _audit().audit_commit(
        repo,
        task_type=TaskType.CODE_CHANGE,
        scope_globs=["**"],
        base_commit=base,
        commit=head,
    )
    assert not result.passed
    assert result.test_path_violations == ["tests/test_app.py"]
    assert result.uncommitted_leftovers == []


async def test_audit_commit_out_of_scope(repo: Path) -> None:
    base = (await _git(repo, "rev-parse", "HEAD")).strip()
    (repo / "app.py").write_text("x = 12\n")
    head = await _commit_all(repo, "change app")
    result = await _audit().audit_commit(
        repo,
        task_type=TaskType.CODE_CHANGE,
        scope_globs=["tests/**"],
        base_commit=base,
        commit=head,
    )
    assert not result.passed
    assert result.out_of_scope_writes == ["app.py"]


async def test_audit_commit_content_hash_mismatch_with_files(repo: Path) -> None:
    base = (await _git(repo, "rev-parse", "HEAD")).strip()
    start = await _audit().capture_test_manifest(repo)
    (repo / "conftest.py").write_text("# sneaky root conftest\n")
    head = await _commit_all(repo, "add root conftest")
    result = await _audit().audit_commit(
        repo,
        task_type=TaskType.CODE_CHANGE,
        scope_globs=["**"],
        base_commit=base,
        commit=head,
        start_manifest=start,
    )
    assert not result.passed
    assert result.content_hash_mismatches == ["conftest.py"]


async def test_audit_commit_content_hash_mismatch_root_hash_only(repo: Path) -> None:
    """start_manifest carries only the root hash (files={}) — best-effort path
    attribution: every changed test-signal path is reported."""
    base = (await _git(repo, "rev-parse", "HEAD")).strip()
    start = await _audit().capture_test_manifest(repo)
    assert start.files  # sanity: populated before stripping
    stripped = Manifest(root_hash=start.root_hash, files={})
    (repo / "tests" / "test_app.py").write_text("def test_hacked(): pass\n")
    head = await _commit_all(repo, "weaken test")
    result = await _audit().audit_commit(
        repo,
        task_type=TaskType.CODE_CHANGE,
        scope_globs=["**"],
        base_commit=base,
        commit=head,
        start_manifest=stripped,
    )
    assert not result.passed
    assert result.content_hash_mismatches == ["tests/test_app.py"]


async def test_audit_commit_clean_when_no_manifest(repo: Path) -> None:
    base = (await _git(repo, "rev-parse", "HEAD")).strip()
    (repo / "app.py").write_text("x = 13\n")
    head = await _commit_all(repo, "change app")
    result = await _audit().audit_commit(
        repo,
        task_type=TaskType.CODE_CHANGE,
        scope_globs=["**"],
        base_commit=base,
        commit=head,
    )
    assert result.passed, result
