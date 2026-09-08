"""Git worktree manager (impl-plan §6.5, D9 / plan.md §8.1).

Worktrees are disposable execution surfaces allocated under
``/tmp/orchestrator-worktrees/<run-id>/<task-id>`` on fast local storage and
force-removed the moment their task reaches a terminal state. This class is
pure git — DB bookkeeping (the ``worktrees`` table) lives with the callers
(the GC daemon and recovery service) so a failed DB write never strands a
git-level worktree and vice versa.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from girder.util import CommandError, run_host_cmd, utcnow_iso

DEFAULT_BASE = Path("/tmp/orchestrator-worktrees")


@dataclass
class WorktreeRef:
    """A provisioned worktree: path plus the task branch it materializes."""

    path: Path
    branch: str
    run_id: str
    task_id: str
    created_at: str


class WorktreeManager:
    def __init__(self, repo_path: Path, base: Path = DEFAULT_BASE) -> None:
        self.repo_path = Path(repo_path).resolve()
        self.base = Path(base)

    async def create(self, run_id: str, task_id: str, base_commit: str = "HEAD") -> WorktreeRef:
        """Create ``<base>/<run-id>/<task-id>`` on new branch ``task/<task-id>``."""
        branch = f"task/{task_id}"
        path = self.base / run_id / task_id
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            raise FileExistsError(f"worktree path already exists: {path}")
        try:
            await run_host_cmd(
                [
                    "git",
                    "-C",
                    str(self.repo_path),
                    "worktree",
                    "add",
                    "-b",
                    branch,
                    str(path),
                    base_commit,
                ],
                timeout_s=60,
            )
        except CommandError as exc:
            # A stale branch of the same name (retry after crash) can exist;
            # it is deliberately preserved — its commits have post-mortem/audit
            # value (D8: attempts are disposable, git history is evidence) — and
            # the detached worktree starts clean from base_commit.
            if await self._branch_exists(branch):
                await run_host_cmd(
                    [
                        "git",
                        "-C",
                        str(self.repo_path),
                        "worktree",
                        "add",
                        "--detach",
                        str(path),
                        base_commit,
                    ],
                    timeout_s=60,
                )
            else:
                raise RuntimeError(f"worktree add failed: {exc}") from exc
        return WorktreeRef(
            path=path,
            branch=branch,
            run_id=run_id,
            task_id=task_id,
            created_at=utcnow_iso(),
        )

    async def remove(self, path: Path | str, *, force: bool = True) -> None:
        """``git worktree remove`` (+ prune of stale metadata)."""
        args = ["git", "-C", str(self.repo_path), "worktree", "remove"]
        if force:
            args.append("--force")
        args.append(str(path))
        await run_host_cmd(args, check=False, timeout_s=60)
        await run_host_cmd(
            ["git", "-C", str(self.repo_path), "worktree", "prune"], check=False, timeout_s=60
        )

    async def list(self) -> list[str]:
        result = await run_host_cmd(
            ["git", "-C", str(self.repo_path), "worktree", "list", "--porcelain"],
            timeout_s=30,
        )
        return [
            line.removeprefix("worktree ")
            for line in result.stdout.splitlines()
            if line.startswith("worktree ")
        ]

    async def _branch_exists(self, branch: str) -> bool:
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
        return result.returncode == 0
