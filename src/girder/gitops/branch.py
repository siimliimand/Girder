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

Exception: :meth:`BranchOps.integrate_branch` **does** create a commit, via
pure plumbing (``merge-tree --write-tree`` → ``commit-tree`` →
``update-ref``), when target and source have diverged. No working tree is
ever touched. Empirically verified with git 2.53, ``git merge-tree
--write-tree --name-only <ours> <theirs>`` prints, on **success** (exit 0),
a single line with the merged tree oid; on **conflict** (exit 1) the
*partial* tree oid on the first line, then the conflicted file paths (one
per line), then a single blank line, then informational messages
(``Auto-merging ...`` / ``CONFLICT ...`` lines). We parse the file lines as
everything between line 1 and the blank line.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from girder.util import run_host_cmd


@dataclass(frozen=True)
class MergeResult:
    merged: bool
    merged_commit: str | None  # new tip of target branch (sha)
    reason: str | None  # refusal reason when merged=False
    conflicted_files: tuple[str, ...] = ()  # populated on three-way conflict


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

    async def reset_branch(self, branch: str, *, base_ref: str = "main") -> str | None:
        """Force-move ``branch`` to ``base_ref`` (abort steering, plan.md
        Phase 5 task 2: "reset branch to ``main``").

        Mirrors the integrator's CAS rollback: a ``git update-ref`` with the
        branch's current tip as the old value, so a concurrent ref move makes
        this fail closed instead of clobbering it. Returns the new tip sha;
        ``None`` when ``base_ref`` does not resolve or the CAS lost (a missing
        branch is simply created at ``base_ref``).
        """
        base = await self.resolve_ref(base_ref)
        if base is None:
            return None
        old = await self.run_branch_tip(branch)
        if old is None:
            return await self.ensure_run_branch(branch, base_commit=base)
        move = await run_host_cmd(
            [
                "git",
                "-C",
                str(self.repo_path),
                "update-ref",
                f"refs/heads/{branch}",
                base,
                old,
            ],
            check=False,
            timeout_s=30,
        )
        if move.returncode != 0:
            return None
        return base

    async def resolve_ref(self, ref: str) -> str | None:
        """Resolve *ref* (branch, ``origin/main``, sha) to a sha, or ``None``."""
        result = await run_host_cmd(
            ["git", "-C", str(self.repo_path), "rev-parse", "--verify", "--quiet", ref],
            check=False,
            timeout_s=30,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip()

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
        audit_passed_for_commit: str,
        worktree_path: Path | None = None,
    ) -> MergeResult:
        """Fast-forward ``target_branch`` to ``source_branch`` iff gated checks pass.

        The audit gate is bound to the exact commit (impl-plan §6.5: merge is
        refused unless DiffAudit passed "on the exact commit being merged"):
        ``audit_passed_for_commit`` must equal the source branch's resolved
        tip, so a ref move between audit and merge cannot smuggle an
        unaudited commit across the boundary.
        """
        source_tip = await self.run_branch_tip(source_branch)
        if source_tip is None:
            return MergeResult(False, None, f"source branch not found: {source_branch}")
        target_tip = await self.run_branch_tip(target_branch)
        if target_tip is None:
            return MergeResult(False, None, f"target branch not found: {target_branch}")
        if audit_passed_for_commit != source_tip:
            return MergeResult(False, None, "audit did not pass for the commit being merged")
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

    async def integrate_branch(
        self,
        *,
        source_branch: str,
        target_branch: str,
        audit_passed: bool,
        message: str | None = None,
    ) -> MergeResult:
        """Fast-forward target to source when possible, else a clean three-way
        merge commit; never loses the audit gate (impl-plan §6.12: serialized
        integration with audit before each merge).

        Refusals (merged=False, reason set, no ref moved): missing branches,
        audit_passed=False, textual merge conflicts (conflicted_files populated).
        """
        source_tip = await self.run_branch_tip(source_branch)
        if source_tip is None:
            return MergeResult(False, None, f"source branch not found: {source_branch}")
        target_tip = await self.run_branch_tip(target_branch)
        if target_tip is None:
            return MergeResult(False, None, f"target branch not found: {target_branch}")
        if not audit_passed:
            return MergeResult(False, None, "audit did not pass")

        # "source has no new commits": source is an ancestor of target
        # (including equal tips) — nothing to integrate.
        noop = await self._git_nocheck("merge-base", "--is-ancestor", source_tip, target_tip)
        if noop.returncode == 0:
            return MergeResult(False, None, "source has no new commits")

        # FF case: target is already an ancestor of source — plain CAS move.
        check = await self._git_nocheck(
            "merge-base", "--is-ancestor", target_tip, source_tip
        )
        if check.returncode == 0:
            move = await self._git_nocheck(
                "update-ref", f"refs/heads/{target_branch}", source_tip, target_tip
            )
            if move.returncode != 0:
                return MergeResult(
                    False, None, f"ref update failed: {move.stderr.strip()[:500]}"
                )
            return MergeResult(True, source_tip, None)

        # Three-way case: compute the merged tree without any working tree.
        merged = await self._git_nocheck(
            "merge-tree", "--write-tree", "--name-only", target_tip, source_tip
        )
        tokens = merged.stdout.split()
        tree = tokens[0] if tokens else None

        if tree is None:
            return MergeResult(False, None, f"merge-tree failed: {merged.stderr.strip()[:500]}")

        if merged.returncode != 0:
            # Conflict layout (verified on git 2.53): the partial tree oid on
            # line 1, then the conflicted paths (one per line), then a single
            # blank line, then informational messages (Auto-merging/CONFLICT).
            lines = merged.stdout.splitlines()
            blank = lines.index("")
            conflicted = tuple(line for line in lines[1:blank] if line.strip())
            return MergeResult(False, None, "merge conflict", conflicted)

        commit_message = message or f"Merge {source_branch} into {target_branch}"
        commit = await self._git_nocheck(
            "commit-tree", tree, "-p", target_tip, "-p", source_tip, "-m", commit_message
        )
        if commit.returncode != 0:
            return MergeResult(
                False, None, f"commit-tree failed: {commit.stderr.strip()[:500]}"
            )
        new_commit = commit.stdout.strip()

        move = await self._git_nocheck(
            "update-ref", f"refs/heads/{target_branch}", new_commit, target_tip
        )
        if move.returncode != 0:
            return MergeResult(False, None, f"ref update failed: {move.stderr.strip()[:500]}")
        return MergeResult(True, new_commit, None)

    async def _git_nocheck(self, *args: str) -> subprocess.CompletedProcess[str]:
        return await run_host_cmd(
            ["git", "-C", str(self.repo_path), *args], check=False, timeout_s=60
        )
