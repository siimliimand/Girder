"""Mechanical test protection layers 2 & 3 (impl-plan §8.2 / plan.md §8.2).

All git invocations run from the orchestrator process via ``git -C <path>`` —
never inside the guest. The audit compares declared task intent (task type,
scope globs) against the *observed* diff, providing an independent,
prompt-unfaithful check of what actually changed.

The ff-only merge gate in :mod:`girder.gitops.branch` refuses to integrate any
attempt whose :class:`AuditResult` is not ``passed``.
"""

from __future__ import annotations

import asyncio
import hashlib
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from girder.db.models import TaskType
from girder.guard.scope import path_matches
from girder.util import run_host_cmd


class GitOpsError(RuntimeError):
    """A git plumbing operation failed (message includes stderr)."""


@dataclass(frozen=True)
class TestManifest:
    """Content hashes of every test-signal file in a worktree at a moment."""

    root_hash: str  # sha256 over sorted "path\0hash\n" lines
    files: dict[str, str]  # relpath -> sha256 of raw bytes


@dataclass(frozen=True)
class AuditResult:
    passed: bool
    test_path_violations: list[str] = field(default_factory=list)
    content_hash_mismatches: list[str] = field(default_factory=list)
    out_of_scope_writes: list[str] = field(default_factory=list)
    protected_path_touches: list[str] = field(default_factory=list)
    uncommitted_leftovers: list[str] = field(default_factory=list)


def _matches_any(rel: str, patterns: list[str]) -> bool:
    return any(path_matches(rel, p) for p in patterns)


def _unquote(p: str) -> str:
    """Undo ``core.quotePath`` quoting (``"tab\there"`` → ``tab<tab>here``)."""
    p = p.strip()
    if len(p) >= 2 and p.startswith('"') and p.endswith('"'):
        p = p[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return p


def _porcelain_paths(out: str) -> list[str]:
    """Paths from ``git status --porcelain`` (v1) output.

    Each line is ``XY <space> <path>``. Rename/copy entries carry
    ``old -> new``: BOTH endpoints belong in the leftover set, otherwise a
    pure rename of an out-of-scope or test-signal file would evade
    classification (the old string ``"old -> new"`` matches neither scope
    nor test patterns). Quoted paths are unquoted; anything unparsable is
    skipped rather than crashing the audit.
    """
    paths: list[str] = []
    for line in out.splitlines():
        if len(line) < 4 or line[2] != " ":
            continue
        entry = line[3:]
        if not entry:
            continue
        if " -> " in entry:
            # partition BEFORE unquoting: either side may be individually
            # quoted (`R  "old\tname" -> "new"`)
            old, _, new = entry.partition(" -> ")
            old, new = _unquote(old), _unquote(new)
            if old:
                paths.append(old)
            if new:
                paths.append(new)
        else:
            unquoted = _unquote(entry)
            if unquoted:
                paths.append(unquoted)
    return paths


async def _git(cwd: Path, *args: str, check: bool = True) -> str:
    result = await run_host_cmd(["git", "-C", str(cwd), *args], check=check, timeout_s=60)
    if check and result.returncode != 0:
        raise GitOpsError(f"git {' '.join(args)} failed: {result.stderr.strip()[:500]}")
    return result.stdout


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class DiffAudit:
    def __init__(
        self, *, test_signal_patterns: list[str], protected_read_paths: list[str]
    ) -> None:
        self.test_signal_patterns = test_signal_patterns
        self.protected_read_paths = protected_read_paths

    async def capture_test_manifest(self, worktree_path: Path) -> TestManifest:
        """Hash every tracked+untracked file matching a test-signal pattern."""
        out = await _git(worktree_path, "ls-files", "-co", "--exclude-standard")
        rels = sorted(
            line
            for line in out.splitlines()
            if line
            and not line.startswith(".git/")
            and _matches_any(line, self.test_signal_patterns)
        )
        files: dict[str, str] = {}
        for rel in rels:
            p = worktree_path / rel
            if not p.is_file():
                continue  # staged-delete leftovers: the diff audit reports them
            files[rel] = _hash_bytes(p.read_bytes())
        lines = "".join(f"{p}\0{h}\n" for p, h in sorted(files.items()))
        return TestManifest(root_hash=_hash_bytes(lines.encode()), files=files)

    async def manifest_from_commit(self, repo_path: Path, commit: str) -> TestManifest:
        """Hash every blob in ``<commit>``'s tree matching a test-signal pattern."""
        out = await _git(repo_path, "ls-tree", "-r", commit)
        rels: list[str] = []
        for line in out.splitlines():
            if not line:
                continue
            meta, _, path = line.partition("\t")
            parts = meta.split()
            if len(parts) != 3 or parts[1] != "blob":
                continue
            rels.append(path)
        files: dict[str, str] = {}
        for rel in rels:
            if not _matches_any(rel, self.test_signal_patterns):
                continue
            blob = await _git(repo_path, "show", f"{commit}:{rel}")
            files[rel] = _hash_bytes(blob.encode())
        lines = "".join(f"{p}\0{h}\n" for p, h in sorted(files.items()))
        return TestManifest(root_hash=_hash_bytes(lines.encode()), files=files)

    async def audit_commit(
        self,
        repo_path: Path,
        *,
        task_type: TaskType,
        scope_globs: list[str],
        base_commit: str,
        commit: str,
        start_manifest: TestManifest | None = None,
    ) -> AuditResult:
        """Layer 2/3 audit of an already-made commit, no worktree required."""
        diff_out = await _git(repo_path, "diff", "--name-only", "-M", base_commit, commit)
        changed = {line for line in diff_out.splitlines() if line}

        test_violations = sorted(
            p
            for p in changed
            if task_type is not TaskType.TEST_CHANGE
            and _matches_any(p, self.test_signal_patterns)
        )
        protected = sorted(p for p in changed if _matches_any(p, self.protected_read_paths))
        out_of_scope = sorted(
            p
            for p in changed
            if not p.startswith(".git/")
            and not any(path_matches(p, g) for g in scope_globs)
        )

        mismatches: list[str] = []
        if start_manifest is not None:
            end = await self.manifest_from_commit(repo_path, commit)
            if start_manifest.files:
                for path in sorted(set(start_manifest.files) | set(end.files)):
                    if start_manifest.files.get(path) != end.files.get(path):
                        # Same layer-2 semantics as audit_attempt: flag only
                        # test-signal drift on non-test tasks (deletions count).
                        if (
                            task_type is not TaskType.TEST_CHANGE
                            and _matches_any(path, self.test_signal_patterns)
                        ):
                            mismatches.append(path)
            elif (
                start_manifest.root_hash
                and start_manifest.root_hash != end.root_hash
                and task_type is not TaskType.TEST_CHANGE
            ):
                # The caller only kept the root hash (not per-file entries), so
                # per-path attribution is impossible; best-effort, report every
                # changed test-signal path as a mismatch. Same layer-2 gate as
                # above: a test_change task is ALLOWED to move test files, so
                # its root hash drifting from the pre-task anchor is normal.
                mismatches = sorted(
                    p for p in changed if _matches_any(p, self.test_signal_patterns)
                )

        leftovers: list[str] = []
        return AuditResult(
            passed=not (test_violations or mismatches or out_of_scope or protected or leftovers),
            test_path_violations=test_violations,
            content_hash_mismatches=mismatches,
            out_of_scope_writes=out_of_scope,
            protected_path_touches=protected,
            uncommitted_leftovers=leftovers,
        )

    async def audit_attempt(
        self,
        worktree_path: Path,
        *,
        task_type: TaskType,
        scope_globs: list[str],
        base_commit: str,
        start_manifest: TestManifest | None = None,
    ) -> AuditResult:
        """Compare the worktree's diff against declared intent (§8.2 layers 2 & 3)."""
        diff_out = await _git(worktree_path, "diff", "--name-only", "-M", base_commit, "HEAD")
        changed = {line for line in diff_out.splitlines() if line}

        status_out = await _git(worktree_path, "status", "--porcelain")
        leftovers = _porcelain_paths(status_out)
        changed |= set(leftovers)

        test_violations = sorted(
            p
            for p in changed
            if task_type is not TaskType.TEST_CHANGE
            and _matches_any(p, self.test_signal_patterns)
        )
        protected = sorted(p for p in changed if _matches_any(p, self.protected_read_paths))
        out_of_scope = sorted(
            p
            for p in changed
            if not p.startswith(".git/")
            and not any(path_matches(p, g) for g in scope_globs)
        )

        mismatches: list[str] = []
        if start_manifest is not None:
            end = await self.capture_test_manifest(worktree_path)
            for path in sorted(set(start_manifest.files) | set(end.files)):
                if start_manifest.files.get(path) != end.files.get(path):
                    # Layer 2: independent re-hash — flag only if the drift is
                    # test-signal and the task is not a test change (deletions
                    # included; a non-test task silently deleting tests is the
                    # SC-03 shape this defends against).
                    if (
                        task_type is not TaskType.TEST_CHANGE
                        and _matches_any(path, self.test_signal_patterns)
                    ):
                        mismatches.append(path)

        return AuditResult(
            passed=not (test_violations or mismatches or out_of_scope or protected or leftovers),
            test_path_violations=test_violations,
            content_hash_mismatches=mismatches,
            out_of_scope_writes=out_of_scope,
            protected_path_touches=protected,
            uncommitted_leftovers=leftovers,
        )

    async def materialize_test_snapshot(
        self, repo_path: Path, base_commit: str, rel_paths: list[str], dest: Path
    ) -> Path:
        """Extract ``rel_paths`` from ``base_commit`` into ``dest`` (layer 1 support).

        The snapshot is the source of truth the test runner consumes, so later
        worktree edits cannot affect it. Raises :class:`GitOpsError` when the
        archive is empty or the paths are invalid at that commit.
        """
        await asyncio.to_thread(dest.mkdir, parents=True, exist_ok=True)
        # Route the archive through a temp file via `git archive -o` (git
        # writes it; run_host_cmd only captures text output).
        with tempfile.NamedTemporaryFile(suffix=".tar", delete=False) as fh:
            archive = Path(fh.name)
        proc = await run_host_cmd(
            [
                "git",
                "-C",
                str(repo_path),
                "archive",
                "-o",
                str(archive),
                base_commit,
                "--",
                *rel_paths,
            ],
            check=False,
            timeout_s=120,
        )
        try:
            if proc.returncode != 0:
                raise GitOpsError(
                    f"git archive {base_commit} -- {' '.join(rel_paths)} failed: "
                    f"{proc.stderr.strip()[:500]}"
                )
            extract = await run_host_cmd(
                ["tar", "-xf", str(archive), "-C", str(dest)], check=False, timeout_s=120
            )
            if extract.returncode != 0:
                raise GitOpsError(f"tar extract failed: {extract.stderr.strip()[:500]}")
        finally:
            await asyncio.to_thread(archive.unlink, missing_ok=True)
        return dest
