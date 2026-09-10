"""Integration: spec freezing against a real temp git repo (plan.md Phase 1 task 4)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from girder.db import repo
from girder.db.engine import Database
from girder.db.models import Project, Run, RunStatus
from girder.specs.freeze import FreezeError, FreezeResult, approve_and_freeze
from girder.specs.validator import SpecValidationError
from girder.util import run_host_cmd
from tests.conftest import seed_run_status

pytestmark = pytest.mark.integration

PROPOSAL = """\
---
schema: girder.openspec/v1
title: Freeze test
intent: Verify the freeze path end to end.
tasks:
  - id: do-thing
    title: Do the thing
    type: code_change
    scope_globs: ["src/**"]
    success_criteria: ["the thing is done"]
    depends_on: []
---
Narrative.
"""


async def _git(cwd: Path, *args: str) -> str:
    result = await run_host_cmd(["git", "-C", str(cwd), *args], timeout_s=30)
    return result.stdout


@dataclass
class Ctx:
    db: Database
    project: Project
    run: Run
    git_repo: Path
    wt_base: Path
    result: FreezeResult


@pytest.fixture
async def git_repo(tmp_path: Path) -> Path:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    await _git(repo_dir, "init", "-b", "main")
    await _git(repo_dir, "config", "user.email", "test@girder.local")
    await _git(repo_dir, "config", "user.name", "girder-test")
    (repo_dir / "README.md").write_text("repo\n")
    await _git(repo_dir, "add", "-A")
    await _git(repo_dir, "commit", "-m", "initial")
    return repo_dir


@pytest.fixture
async def frozen(db: Database, git_repo: Path, tmp_path: Path) -> Ctx:
    project = await repo.create_project(db, "freeze-test", str(git_repo))
    run = await repo.create_run(db, project.id, "intent", "run/freeze1", 5.0)
    await seed_run_status(db, run.id, RunStatus.SPEC_PENDING.value)
    run = await repo.get_run(db, run.id)
    assert run is not None
    wt_base = tmp_path / "wt"
    result = await approve_and_freeze(
        db, project=project, run=run, proposal_text=PROPOSAL,
        repo_path=git_repo, worktree_base=wt_base,
    )
    return Ctx(db=db, project=project, run=run, git_repo=git_repo, wt_base=wt_base, result=result)


async def test_freeze_commits_and_transitions(frozen: Ctx) -> None:
    ctx, result = frozen, frozen.result
    assert result.spec_hash == hashlib.sha256(PROPOSAL.encode()).hexdigest()
    assert result.branch == ctx.run.branch

    await _git(ctx.git_repo, "rev-parse", "--verify", f"refs/heads/{result.branch}")
    blob = await _git(
        ctx.git_repo, "show", f"{result.branch}:openspec/proposals/{ctx.run.id}.md"
    )
    assert blob == PROPOSAL
    assert result.commit_sha in await _git(ctx.git_repo, "log", "--format=%H", result.branch)

    fresh = await repo.get_run(ctx.db, ctx.run.id)
    assert fresh is not None
    assert fresh.spec_hash == result.spec_hash
    assert fresh.status is RunStatus.SPEC_APPROVED

    # worktree directory cleaned up, branch survives
    assert not (ctx.wt_base / f"spec-{ctx.run.id}").exists()
    branches = await _git(ctx.git_repo, "branch", "--list", result.branch)
    assert result.branch in branches

    events = await ctx.db.fetchall(
        "SELECT event_type, payload_json FROM agent_events WHERE run_id = ?", (ctx.run.id,)
    )
    frozen_events = [
        e for e in events if e["event_type"] == "spec_frozen"
        and json.loads(e["payload_json"])["commit_sha"] == result.commit_sha
        and json.loads(e["payload_json"])["spec_hash"] == result.spec_hash
    ]
    assert frozen_events


async def test_freeze_resumes_after_crash_between_commit_and_db(
    db: Database, git_repo: Path, tmp_path: Path
) -> None:
    """Crash invariance: if a prior attempt committed the proposal but died
    before the DB writes (run still spec_pending, spec_hash NULL), a retry
    must skip the duplicate commit, reconcile the DB, and set spec_hash
    exactly once."""
    project = await repo.create_project(db, "crash", str(git_repo))
    run = await repo.create_run(db, project.id, "intent", "run/crash", 5.0)
    await seed_run_status(db, run.id, RunStatus.SPEC_PENDING.value)
    fresh = await repo.get_run(db, run.id)
    assert fresh is not None

    # Simulate the crash state using the module's own commit path.
    wt_base = tmp_path / "wt"
    from girder.specs.freeze import _commit_in_worktree, _resolve_base

    base = await _resolve_base(git_repo, fresh.branch)
    crashed_head = await _commit_in_worktree(
        git_repo, wt_base / f"spec-{run.id}", fresh.branch, base, run.id, PROPOSAL
    )
    mid = await repo.get_run(db, run.id)
    assert mid is not None and mid.status is RunStatus.SPEC_PENDING
    assert mid.spec_hash is None

    result = await approve_and_freeze(
        db, project=project, run=fresh, proposal_text=PROPOSAL,
        repo_path=git_repo, worktree_base=wt_base,
    )
    # No duplicate commit: retry reused the crashed attempt's head.
    assert result.commit_sha == crashed_head
    assert crashed_head in await _git(git_repo, "log", "--format=%H", fresh.branch)
    subjects = await _git(git_repo, "log", "--format=%s", fresh.branch)
    assert subjects.count(f"spec: freeze openspec proposal {run.id}") == 1

    reconciled = await repo.get_run(db, run.id)
    assert reconciled is not None
    assert reconciled.spec_hash == hashlib.sha256(PROPOSAL.encode("utf-8")).hexdigest()
    assert reconciled.status is RunStatus.SPEC_APPROVED


async def test_freeze_refuses_differing_committed_proposal(
    db: Database, git_repo: Path, tmp_path: Path
) -> None:
    """A blob already on the run branch that differs from the approved text is
    tamper evidence: refuse, never overwrite silently."""
    project = await repo.create_project(db, "tamper-blob", str(git_repo))
    run = await repo.create_run(db, project.id, "intent", "run/tamper-blob", 5.0)
    await seed_run_status(db, run.id, RunStatus.SPEC_PENDING.value)
    fresh = await repo.get_run(db, run.id)
    assert fresh is not None

    wt_base = tmp_path / "wt"
    from girder.specs.freeze import _commit_in_worktree, _resolve_base

    base = await _resolve_base(git_repo, fresh.branch)
    await _commit_in_worktree(
        git_repo, wt_base / f"spec-{run.id}", fresh.branch, base, run.id,
        PROPOSAL + "malicious edit\n",
    )
    with pytest.raises(FreezeError, match="differs"):
        await approve_and_freeze(
            db, project=project, run=fresh, proposal_text=PROPOSAL,
            repo_path=git_repo, worktree_base=wt_base,
        )
    after = await repo.get_run(db, run.id)
    assert after is not None
    assert after.status is RunStatus.SPEC_PENDING
    assert after.spec_hash is None


async def test_double_freeze_refused(frozen: Ctx) -> None:
    again = await repo.get_run(frozen.db, frozen.run.id)
    assert again is not None
    with pytest.raises(FreezeError, match="already frozen"):
        await approve_and_freeze(
            frozen.db, project=frozen.project, run=again, proposal_text=PROPOSAL,
            repo_path=frozen.git_repo, worktree_base=frozen.wt_base,
        )


async def test_wrong_state_refused(db: Database, git_repo: Path, tmp_path: Path) -> None:
    project = await repo.create_project(db, "wrong-state", str(git_repo))
    run = await repo.create_run(db, project.id, "intent", "run/draft", 5.0)
    with pytest.raises(FreezeError, match="spec_pending"):
        await approve_and_freeze(
            db, project=project, run=run, proposal_text=PROPOSAL,
            repo_path=git_repo, worktree_base=tmp_path / "wt",
        )


async def test_invalid_proposal_leaves_run_untouched(
    db: Database, git_repo: Path, tmp_path: Path
) -> None:
    project = await repo.create_project(db, "invalid", str(git_repo))
    run = await repo.create_run(db, project.id, "intent", "run/invalid", 5.0)
    await seed_run_status(db, run.id, RunStatus.SPEC_PENDING.value)
    fresh = await repo.get_run(db, run.id)
    assert fresh is not None
    with pytest.raises(SpecValidationError):
        await approve_and_freeze(
            db, project=project, run=fresh, proposal_text="no frontmatter",
            repo_path=git_repo, worktree_base=tmp_path / "wt",
        )
    after = await repo.get_run(db, run.id)
    assert after is not None
    assert after.status is RunStatus.SPEC_PENDING
    assert after.spec_hash is None
    out = await run_host_cmd(
        ["git", "-C", str(git_repo), "rev-parse", "--verify", "--quiet",
         f"refs/heads/{run.branch}"],
        check=False, timeout_s=30,
    )
    assert out.returncode != 0  # no branch created


async def test_tamper_evidence_hash_changes(git_repo: Path, tmp_path: Path) -> None:
    """Phase 1 exit criterion 2: any edit to the proposal changes the hash."""
    hashes = []
    dbs = [await Database.open(tmp_path / f"tamper-{i}.db") for i in range(2)]
    try:
        for i, (db, text) in enumerate(zip(dbs, (PROPOSAL, PROPOSAL + "x"), strict=True)):
            project = await repo.create_project(db, "tamper", str(git_repo))
            run = await repo.create_run(db, project.id, "intent", f"run/tamper-{i}", 5.0)
            await seed_run_status(db, run.id, RunStatus.SPEC_PENDING.value)
            fresh = await repo.get_run(db, run.id)
            assert fresh is not None
            result = await approve_and_freeze(
                db, project=project, run=fresh, proposal_text=text,
                repo_path=git_repo, worktree_base=tmp_path / f"wt-{i}",
            )
            assert result.spec_hash == hashlib.sha256(text.encode()).hexdigest()
            hashes.append(result.spec_hash)
    finally:
        for db in dbs:
            await db.close()
    assert hashes[0] != hashes[1]


async def test_non_ascii_hash_matches_committed_bytes(
    db: Database, git_repo: Path, tmp_path: Path
) -> None:
    """The hash must cover the bytes actually committed (explicit UTF-8 write),
    not just the in-memory text (Phase 1 exit criterion 2)."""
    proposal = PROPOSAL.replace("Narrative.", "Narrative — émoji 🚀 naïve.")
    project = await repo.create_project(db, "utf8", str(git_repo))
    run = await repo.create_run(db, project.id, "intént 🚀", "run/utf8", 5.0)
    await seed_run_status(db, run.id, RunStatus.SPEC_PENDING.value)
    fresh = await repo.get_run(db, run.id)
    assert fresh is not None
    result = await approve_and_freeze(
        db, project=project, run=fresh, proposal_text=proposal,
        repo_path=git_repo, worktree_base=tmp_path / "wt",
    )
    blob = await _git(git_repo, "show", f"{run.branch}:openspec/proposals/{run.id}.md")
    assert blob == proposal
    assert result.spec_hash == hashlib.sha256(blob.encode("utf-8")).hexdigest()
    assert result.spec_hash == hashlib.sha256(proposal.encode("utf-8")).hexdigest()
