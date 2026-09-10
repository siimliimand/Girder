"""Format index slices for prompt injection (WP 8.4).

One entry point — :func:`codebase_index_guidance` — is the single hook called
from ``task_engine`` at attempt start. It builds the index from the attempt
worktree (fresh every attempt, R-SP8-4), slices it by the task's
``scope_globs``, renders the ``[TRUSTED]`` block, persists the JSON blob to
``tasks.codebase_index_json`` (migration 014), and returns the caller's
guidance composed with the block. When ``[limits] inject_index`` is false it
is a pass-through.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from girder.config import LimitsConfig
from girder.db.engine import Database
from girder.db.models import Task
from girder.index.indexer import CodebaseIndex, FileEntry, build_index

log = logging.getLogger(__name__)

BLOCK_HEADER = "[TRUSTED] Codebase index (relevant to your scope):"
TRUNCATION_NOTE = (
    "(index truncated to fit inject_index_max_tokens — lowest-relevance directories dropped)"
)

# Rough token estimate: ~4 characters per token is good enough for a budget.
_CHARS_PER_TOKEN = 4
_MAX_NAMES_PER_LIST = 8  # per file, then ", ..."
_MIN_DIRS_KEPT = 1  # even a huge top dir beats an empty index


def _format_names(entries: list[tuple[str, int]]) -> str:
    names = [n for n, _ in entries[:_MAX_NAMES_PER_LIST]]
    if len(entries) > _MAX_NAMES_PER_LIST:
        names.append("...")
    return ", ".join(names)


def format_file(entry: FileEntry) -> str:
    """One file line, matching the spec block in ws03-codebase-index.md."""
    detail: list[str] = []
    if entry.classes:
        detail.append(f"classes: {_format_names(list(entry.classes))}")
    if entry.functions:
        detail.append(f"functions: {_format_names(list(entry.functions))}")
    suffix = f" — {'; '.join(detail)}" if detail else ""
    return f"  {Path(entry.path).name} ({entry.lines} lines){suffix}"


def _relevance(files: list[FileEntry], matched: set[str]) -> int:
    """How much of a directory is in-scope: in-scope file count (fallback 0)."""
    return sum(1 for f in files if f.path in matched)


def format_block(index: CodebaseIndex, files: tuple[FileEntry, ...], max_tokens: int) -> str:
    """Render the trusted block; drop whole directories to fit *max_tokens*.

    Directories are ranked by relevance (in-scope file count) and dropped
    lowest-first; lines are never cut mid-line. With no glob slice the whole
    index is eligible for ranking but everything counts as relevance 0, so
    the smallest directories go first — the top-level package dirs survive.
    """
    if not files:
        return ""
    by_dir = {d: sorted(fs, key=lambda f: f.path) for d, fs in _group(files).items()}
    matched = {f.path for f in files}
    ranked = sorted(
        by_dir.items(),
        key=lambda kv: (_relevance(kv[1], matched), _char_count(kv[1])),
        reverse=True,
    )

    def render(dirs: list[tuple[str, list[FileEntry]]]) -> str:
        parts = [BLOCK_HEADER, ""]
        for d, fs in dirs:
            parts.append(f"{d}/")
            parts.extend(format_file(f) for f in fs)
            parts.append("")
        while parts and parts[-1] == "":
            parts.pop()
        return "\n".join(parts)

    budget = max_tokens * _CHARS_PER_TOKEN
    kept = list(ranked)
    while kept and len(render(kept)) > budget and len(kept) > _MIN_DIRS_KEPT:
        kept.pop()  # `ranked` is descending by relevance: pop = least relevant
    text = render(kept)
    if len(kept) < len(ranked):
        text += f"\n{TRUNCATION_NOTE}"
    return text


def _group(files: tuple[FileEntry, ...]) -> dict[str, list[FileEntry]]:
    groups: dict[str, list[FileEntry]] = {}
    for f in files:
        parent = str(Path(f.path).parent)
        groups.setdefault("." if parent == "." else parent, []).append(f)
    return groups


def _char_count(fs: list[FileEntry]) -> int:
    return sum(len(format_file(f)) for f in fs)


async def codebase_index_guidance(
    base: str | None,
    db: Database,
    task: Task,
    worktree_path: Path,
    limits: LimitsConfig,
) -> str | None:
    """The one-line hook target: compose *base* guidance with the index block.

    Pass-through (returns *base* unchanged) when ``inject_index`` is disabled
    or the index comes back empty. Failures degrade to *base* — a broken
    index must never kill an attempt.
    """
    if not limits.inject_index:
        return base
    try:
        index = build_index(worktree_path)
        files = index.slice_by_globs(task.scope_globs)
        block = format_block(index, files, limits.inject_index_max_tokens)
        if db is not None:
            from girder.db import repo as db_repo

            await db_repo.update_task_fields(
                db,
                task.id,
                codebase_index_json=json.dumps(index.to_json_dict()),
            )
    except Exception:  # pragma: no cover - defensive; never block an attempt
        log.exception("codebase index build failed for task %s", task.id)
        return base
    if not block:
        return base
    if base:
        return f"{base}\n\n{block}"
    return block
