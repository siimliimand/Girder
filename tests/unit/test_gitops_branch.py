"""BranchOps — run branches and the audit-gated ff-only merge, real git repos."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from girder.gitops.branch import BranchOps
from girder.gitops.worktree import WorktreeManager
from girder.util import run_host_cmd

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


async def _git(cwd: Path, *args: str) -> str:
    result = await run_host_cmd(["git", "-C", str(cwd), *args], timeout_s=30)
    return result.stdout


async def _commit(repo_dir: Path, msg: str) -> str:
    await _git(repo_dir, "add", "-A")
    await _git(repo_dir, "commit", "-m", msg)
    return (await _git(repo_dir, "rev-parse", "HEAD")).strip()


@pytest.fixture
async def repo(tmp_path: Path) -> Path:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    await _git(repo_dir, "init", "-b", "main")
    await _git(repo_dir, "config", "user.email", "t@t")
    await _git(repo_dir, "config", "user.name", "t")
    (repo_dir / "app.py").write_text("x = 1\n")
    await _git(repo_dir, "add", "-A")
    await _git(repo_dir, "commit", "-m", "initial")
    return repo_dir


async def test_ensure_run_branch_idempotent(repo: Path) -> None:
    base = (await _git(repo, "rev-parse", "HEAD")).strip()
    ops = BranchOps(repo)
    tip1 = await ops.ensure_run_branch("run/abc", base_commit=base)
    tip2 = await ops.ensure_run_branch("run/abc", base_commit=base)
    assert tip1 == tip2 == base
    (repo / "other.py").write_text("y\n")
    await _commit(repo, "second")
    tip3 = await ops.ensure_run_branch("run/abc", base_commit="HEAD")
    assert tip3 == tip1  # does not move an existing branch


async def test_run_branch_tip_missing(repo: Path) -> None:
    assert await BranchOps(repo).run_branch_tip("run/nope") is None


async def test_merge_refused_when_audit_failed(repo: Path) -> None:
    await _git(repo, "branch", "run/r1")
    await _git(repo, "checkout", "-q", "-b", "task/t1")
    (repo / "app.py").write_text("x = 2\n")
    src_tip = await _commit(repo, "work")
    await _git(repo, "checkout", "-q", "main")
    ops = BranchOps(repo)
    result = await ops.audit_gated_merge(
        source_branch="task/t1", target_branch="run/r1", audit_passed=False
    )
    assert result == result.__class__(merged=False, merged_commit=None, reason=result.reason)
    assert result.reason
    assert await ops.run_branch_tip("run/r1") != src_tip


async def test_merge_refused_non_ff(repo: Path) -> None:
    ops = BranchOps(repo)
    base = (await _git(repo, "rev-parse", "HEAD")).strip()
    await ops.ensure_run_branch("run/r2", base_commit=base)
    # target advances past source (an earlier hotfix merged first)
    await _git(repo, "checkout", "-q", "-b", "hotfix")
    (repo / "hot.py").write_text("h\n")
    await _commit(repo, "hotfix")
    hot_tip = (await _git(repo, "rev-parse", "HEAD")).strip()
    await _git(repo, "branch", "-f", "run/r2", "hotfix")
    # source diverges from base, not from the advanced target
    await _git(repo, "checkout", "-q", "-b", "task/t2", base)
    (repo / "task.py").write_text("t\n")
    src_tip = await _commit(repo, "task work")
    result = await ops.audit_gated_merge(
        source_branch="task/t2", target_branch="run/r2", audit_passed=True
    )
    assert not result.merged
    assert "fast-forward" in (result.reason or "")
    assert await ops.run_branch_tip("run/r2") == hot_tip
    assert await ops.run_branch_tip("run/r2") != src_tip


async def test_merge_succeeds_ff_and_updates_target(repo: Path, tmp_path: Path) -> None:
    ops = BranchOps(repo)
    base = (await _git(repo, "rev-parse", "HEAD")).strip()
    await ops.ensure_run_branch("run/r3", base_commit=base)
    mgr = WorktreeManager(repo, base=tmp_path / "wt")
    ref = await mgr.create("run3", "t3", base)
    (ref.path / "app.py").write_text("x = 3\n")
    await _git(ref.path, "add", "-A")
    await _git(ref.path, "commit", "-m", "task work")
    src_tip = (await _git(repo, "rev-parse", "task/t3")).strip()

    result = await ops.audit_gated_merge(
        source_branch="task/t3",
        target_branch="run/r3",
        audit_passed=True,
        worktree_path=ref.path,
    )
    assert result.merged
    assert result.merged_commit == src_tip
    assert await ops.run_branch_tip("run/r3") == src_tip


async def test_merge_refused_when_worktree_dirty(repo: Path, tmp_path: Path) -> None:
    ops = BranchOps(repo)
    base = (await _git(repo, "rev-parse", "HEAD")).strip()
    await ops.ensure_run_branch("run/r4", base_commit=base)
    mgr = WorktreeManager(repo, base=tmp_path / "wt")
    ref = await mgr.create("run4", "t4", base)
    (ref.path / "dirty.py").write_text("d\n")
    result = await ops.audit_gated_merge(
        source_branch="task/t4",
        target_branch="run/r4",
        audit_passed=True,
        worktree_path=ref.path,
    )
    assert not result.merged
    assert "clean" in (result.reason or "")


async def test_merge_refused_missing_branch(repo: Path) -> None:
    base = (await _git(repo, "rev-parse", "HEAD")).strip()
    ops = BranchOps(repo)
    await ops.ensure_run_branch("run/r5", base_commit=base)
    result = await ops.audit_gated_merge(
        source_branch="task/ghost", target_branch="run/r5", audit_passed=True
    )
    assert not result.merged
    assert "not found" in (result.reason or "")
