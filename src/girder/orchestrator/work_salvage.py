"""Group B work salvage: commit a dead attempt's dirty worktree (impl-plan §5.2).

Live dogfood runs lost every retry: the dead attempt's uncommitted worktree
was force-pruned, so attempt N+1 re-explored from scratch and died identically.
Committing the leftovers (mechanically, with a pinned identity so a missing
host git config can't fail) keeps that work; ``allocate_attempt`` then bases
the retry worktree on the salvaged task-branch tip. Integrity violations and
amendment parks keep force-prune semantics — their callers never invoke this.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from girder.db.models import Attempt, Task
from girder.util import run_host_cmd

if TYPE_CHECKING:
    from girder.orchestrator.task_engine import TaskEngine

# Keep the historical logger name (tests assert on it via caplog).
log = logging.getLogger("girder.orchestrator.task_engine")


async def salvage_worktree(
    repo_path: Path, task: Task, attempt: Attempt
) -> tuple[str, list[str]] | None:
    """Commit a dead attempt's dirty worktree onto its task branch (Group B).

    Returns ``(commit sha, changed files)`` or ``None`` when there was nothing
    to salvage (clean tree, missing worktree, or a failed salvage commit).
    """
    wt = attempt.worktree_path
    if wt is None:
        return None
    status = await run_host_cmd(
        ["git", "-C", wt, "status", "--porcelain"], check=False, timeout_s=30
    )
    if status.returncode != 0 or not status.stdout.strip():
        return None
    await run_host_cmd(["git", "-C", wt, "add", "-A"], check=False, timeout_s=60)
    commit = await run_host_cmd(
        [
            "git",
            "-C",
            wt,
            "-c",
            "user.name=girder",
            "-c",
            "user.email=girder@local",
            "commit",
            "-m",
            f"wip(attempt {attempt.attempt_num}): salvaged on retry",
        ],
        check=False,
        timeout_s=60,
    )
    if commit.returncode != 0:
        log.warning("attempt %s: salvage commit failed: %s", attempt.id, commit.stderr[-200:])
        return None
    head = await run_host_cmd(["git", "-C", wt, "rev-parse", "HEAD"], timeout_s=30)
    sha = head.stdout.strip()
    files = await run_host_cmd(
        ["git", "-C", wt, "diff", "--name-only", "HEAD~1"], check=False, timeout_s=30
    )
    # Advance the task branch even when the worktree HEAD is detached (retry
    # worktrees are created detached once the branch exists), so the salvage
    # is reachable for the retry base and post-mortems.
    await run_host_cmd(
        [
            "git",
            "-C",
            str(repo_path),
            "update-ref",
            f"refs/heads/task/{task.id}",
            sha,
        ],
        timeout_s=30,
    )
    changed = [ln for ln in files.stdout.splitlines() if ln.strip()]
    log.info(
        "attempt %s: salvaged uncommitted work as %s (%d file(s))",
        attempt.id,
        sha[:12],
        len(changed),
    )
    return sha, changed


def salvage_guidance(attempt_num: int, fate: str, salvage: tuple[str, list[str]], base: str) -> str:
    """Trusted retry guidance pointing attempt N+1 at the salvaged commit."""
    sha, files = salvage
    return (
        f"{base}\nPrevious attempt {attempt_num} {fate}; its uncommitted work was "
        f"salvaged as commit {sha} (files: {', '.join(files)}). Continue from "
        "there — do not redo completed work; finish and call mark_task_complete."
    )


async def salvage_dirty_worktree(
    engine: TaskEngine, task: Task, attempt: Attempt
) -> str | None:
    """Salvage *attempt*'s dirty worktree and record the tip for the retry.

    Returns the salvage commit SHA (also stored in
    ``engine._salvage_tips[task.id]`` so ``allocate_attempt`` bases the retry
    worktree on it), or ``None`` when there was nothing to salvage. Callers
    wrap this in ``suppress(Exception)`` — a salvage failure must not mask the
    real outcome.
    """
    result = await salvage_worktree(engine.repo_path, task, attempt)
    if result is None:
        return None
    sha, _files = result
    engine._salvage_tips[task.id] = sha
    return sha
