"""Audit-gated branch operations — the merge gate (D4/D10, impl-plan §6.5).

This module never creates commits; the agent's commits already exist on
``task/<task_id>``. It only moves refs safely:

* run branches (``run/<id8>``) are created idempotently so retries converge;
* merges into the target branch are **fast-forward only**. Proven with
  ``git merge-base --is-ancestor`` and executed with
  ``git update-ref refs/heads/<target> <source_tip>`` — plumbing that works
  whether or not either branch is checked out anywhere (a worktree-checked-out
  branch would make ``git fetch . src:tgt`` refuse, and a checkout would
  mutate whatever worktree holds it).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from girder.util import run_host_cmd


@dataclass(frozen=True)
class MergeResult:
    merged: bool
    merged_commit: str | None  # new tip of target branch (sha)
    reason: str | None  # refusal reason when merged=False


class BranchOps:
    def __init__(self, repo_path: Path) -> None:
        self.repo_path = Path(repo_path).resolve()

    async def _git(self, *args: str, check: bool = True) -> str:
        result = await run_host_cmd(
            ["git", "-C", str(self.repo_path), *args], check=False, timeout_s=60
        )
        if check and result.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()[:500]}")
        return result.stdout

    async def ensure_run_branch(self, branch: str, *, base_commit: str = "HEAD") -> str:
        """Create ``<branch>`` at ``base_commit`` if missing; return its tip sha."""
        tip = await self.run_branch_tip(branch)
        if tip is not None:
            return tip
        await self._git("branch", branch, base_commit)
        tip = await self.run_branch_tip(branch)
        assert tip is not None  # we just created it
        return tip

    async def run_branch_tip(self, branch: str) -> str | None:
        result = await run_host_cmd(
            [
                "git",
                "-C",
                str(self.repo_path),
                "rev-parse",
                "--verify",
                "--quiet",
                f"refs/heads/{branch}",
            ],
            check=False,
            timeout_s=30,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip()

    async def _worktree_clean(self, worktree_path: Path) -> bool:
        result = await run_host_cmd(
            ["git", "-C", str(worktree_path), "status", "--porcelain"],
            check=False,
            timeout_s=30,
        )
        return result.returncode == 0 and not result.stdout.strip()

    async def audit_gated_merge(
        self,
        *,
        source_branch: str,
        target_branch: str,
        audit_passed: bool,
        worktree_path: Path | None = None,
    ) -> MergeResult:
        """Fast-forward ``target_branch`` to ``source_branch`` iff gated checks pass."""
        source_tip = await self.run_branch_tip(source_branch)
        if source_tip is None:
            return MergeResult(False, None, f"source branch not found: {source_branch}")
        target_tip = await self.run_branch_tip(target_branch)
        if target_tip is None:
            return MergeResult(False, None, f"target branch not found: {target_branch}")
        if not audit_passed:
            return MergeResult(False, None, "audit did not pass")
        if worktree_path is not None and not await self._worktree_clean(worktree_path):
            return MergeResult(False, None, "source worktree is not clean")

        # ff-only proof: target must already be an ancestor of source.
        check = await run_host_cmd(
            [
                "git",
                "-C",
                str(self.repo_path),
                "merge-base",
                "--is-ancestor",
                target_tip,
                source_tip,
            ],
            check=False,
            timeout_s=30,
        )
        if check.returncode != 0:
            reason = (
                "not fast-forwardable: target has commits not in source"
                if target_tip != source_tip
                else "source has no new commits"
            )
            return MergeResult(False, None, reason)

        move = await run_host_cmd(
            [
                "git",
                "-C",
                str(self.repo_path),
                "update-ref",
                f"refs/heads/{target_branch}",
                source_tip,
                target_tip,
            ],
            check=False,
            timeout_s=30,
        )
        if move.returncode != 0:
            return MergeResult(False, None, f"ref update failed: {move.stderr.strip()[:500]}")
        return MergeResult(True, source_tip, None)
