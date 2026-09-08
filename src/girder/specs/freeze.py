"""Spec freezing: commit the approved proposal to the run branch and anchor it
with a SHA-256 hash (plan.md Phase 1 task 4, impl-plan §6.9 freeze.py).

The git commit happens BEFORE the spec_approved transition (the §5.1 guard
requires the spec committed); runs.spec_hash pins the exact frozen bytes.
Any later edit — by anyone — changes the hash, which is the tamper evidence
Phase 1 exit criterion 2 demands. For agents, openspec/** is a protected
read path (config default), so they cannot rewrite it anyway.
"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path

from girder.db import repo
from girder.db.engine import Database
from girder.db.models import Project, Run, RunStatus
from girder.fsm import InvalidTransition, transition_run
from girder.gitops.worktree import DEFAULT_BASE
from girder.specs.validator import parse_spec
from girder.util import CommandError, run_host_cmd


@dataclass(slots=True)
class FreezeResult:
    run_id: str
    spec_hash: str  # sha256 hex of the exact proposal text
    branch: str  # run branch the file was committed to
    commit_sha: str


class FreezeError(RuntimeError):
    """Freezing could not complete; the run is left untouched where possible."""


async def approve_and_freeze(
    db: Database,
    *,
    project: Project,
    run: Run,
    proposal_text: str,
    repo_path: Path,
    worktree_base: Path | None = None,
) -> FreezeResult:
    """Validate, commit, hash, and transition the run to spec_approved.

    Raises SpecValidationError for a malformed proposal (caller shows the
    aggregated errors) and FreezeError for state/git failures.
    """
    parse_spec(proposal_text)  # raises SpecValidationError before any side effect

    if run.spec_hash is not None:
        raise FreezeError(f"run {run.id} already frozen")
    if run.status != RunStatus.SPEC_PENDING:
        raise FreezeError(f"run {run.id} must be spec_pending to freeze (is {run.status})")

    spec_hash = hashlib.sha256(proposal_text.encode()).hexdigest()

    branch = run.branch
    base = await _resolve_base(repo_path, branch)
    wt_path = (worktree_base or DEFAULT_BASE) / f"spec-{run.id}"
    commit_sha = await _commit_in_worktree(repo_path, wt_path, branch, base, run.id, proposal_text)

    updated = await repo.get_run(db, run.id)
    if updated is None:
        raise FreezeError(f"run {run.id} disappeared during freeze")
    if updated.spec_hash is not None or updated.status != RunStatus.SPEC_PENDING:
        raise FreezeError(
            f"run {run.id} moved under us: status={updated.status}, spec_hash={updated.spec_hash}"
        )
    await repo.update_run_fields(db, run.id, spec_hash=spec_hash)
    try:
        await transition_run(db, run.id, RunStatus.SPEC_APPROVED, spec_hash=spec_hash)
    except InvalidTransition as exc:
        raise FreezeError(f"run {run.id} could not transition to spec_approved: {exc}") from exc
    await repo.insert_event(
        db,
        "spec_frozen",
        {"spec_hash": spec_hash, "commit_sha": commit_sha, "branch": branch},
        run_id=run.id,
    )
    return FreezeResult(run_id=run.id, spec_hash=spec_hash, branch=branch, commit_sha=commit_sha)


async def _resolve_base(repo_path: Path, branch: str) -> str:
    """Worktree base: the run branch if it exists, else the repo default branch."""
    exists = await run_host_cmd(
        ["git", "-C", str(repo_path), "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        check=False,
        timeout_s=30,
    )
    if exists.returncode == 0:
        return branch
    for candidate in await _default_branch_candidates(repo_path):
        probe = await run_host_cmd(
            ["git", "-C", str(repo_path), "rev-parse", "--verify", "--quiet", candidate],
            check=False,
            timeout_s=30,
        )
        if probe.returncode == 0:
            return candidate
    raise FreezeError(f"no resolvable base branch found for run branch {branch!r}")


async def _default_branch_candidates(repo_path: Path) -> list[str]:
    candidates: list[str] = []
    try:
        sym = await run_host_cmd(
            ["git", "-C", str(repo_path), "symbolic-ref", "refs/remotes/origin/HEAD"],
            check=False,
            timeout_s=30,
        )
        if sym.returncode == 0:
            candidates.append(sym.stdout.strip().removeprefix("refs/remotes/origin/"))
    except CommandError:  # pragma: no cover - run_host_cmd only raises when check=True
        pass
    return [*candidates, "main", "master", "HEAD"]


async def _commit_in_worktree(
    repo_path: Path,
    wt_path: Path,
    branch: str,
    base: str,
    run_id: str,
    proposal_text: str,
) -> str:
    """Materialize a worktree on *branch*, commit the proposal, return the sha."""
    if base == branch:
        add_args = ["git", "-C", str(repo_path), "worktree", "add", str(wt_path), base]
    else:
        add_args = [
            "git", "-C", str(repo_path), "worktree", "add", "-b", branch, str(wt_path), base,
        ]
    try:
        await run_host_cmd(add_args, timeout_s=60)
    except CommandError as exc:
        # A stale registered worktree (crashed prior attempt): clear it once, retry once.
        if "already" in exc.stderr or "exists" in exc.stderr or "registered" in exc.stderr:
            await run_host_cmd(
                ["git", "-C", str(repo_path), "worktree", "remove", "--force", str(wt_path)],
                check=False,
                timeout_s=60,
            )
            try:
                await run_host_cmd(add_args, timeout_s=60)
            except CommandError as retry_exc:
                raise FreezeError(f"worktree add failed for {wt_path}") from retry_exc
        else:
            raise FreezeError(f"worktree add failed for {wt_path}") from exc

    try:
        proposals = wt_path / "openspec" / "proposals"
        proposals.mkdir(parents=True, exist_ok=True)
        (proposals / f"{run_id}.md").write_text(proposal_text)
        await run_host_cmd(
            ["git", "-C", str(wt_path), "add", f"openspec/proposals/{run_id}.md"], timeout_s=60
        )
        await run_host_cmd(
            [
                "git",
                "-C",
                str(wt_path),
                "-c",
                "user.name=girder",
                "-c",
                "user.email=girder@local",
                "commit",
                "-m",
                f"spec: freeze openspec proposal {run_id}",
            ],
            timeout_s=60,
        )
        result = await run_host_cmd(["git", "-C", str(wt_path), "rev-parse", "HEAD"], timeout_s=30)
    except CommandError as exc:
        raise FreezeError(f"failed to commit frozen spec for run {run_id}") from exc
    finally:
        await run_host_cmd(
            ["git", "-C", str(repo_path), "worktree", "remove", "--force", str(wt_path)],
            check=False,
            timeout_s=60,
        )
        await run_host_cmd(
            ["git", "-C", str(repo_path), "worktree", "prune"], check=False, timeout_s=30
        )
        shutil.rmtree(wt_path, ignore_errors=True)
    return result.stdout.strip()
