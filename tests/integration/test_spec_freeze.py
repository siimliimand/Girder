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
