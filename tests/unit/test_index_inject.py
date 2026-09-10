"""Unit tests for index slice/format injection (WP 8.4).

Scope-glob slicing, whole-directory token-cap truncation, spec block format.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import ClassVar

from girder.config import LimitsConfig
from girder.index.indexer import CodebaseIndex, FileEntry, build_index
from girder.index.inject import BLOCK_HEADER, codebase_index_guidance, format_block
from tests.unit.test_index_indexer import make_tree


def _entries(*paths_lines: tuple[str, int]) -> tuple[FileEntry, ...]:
    return tuple(FileEntry(path=p, lines=n) for p, n in paths_lines)


def test_format_block_matches_spec_layout() -> None:
    files = _entries(("src/girder/app.py", 180), ("src/girder/db.py", 120))
    block = format_block(build_index(Path(".")), files, max_tokens=2000)
    lines = block.splitlines()
    assert lines[0] == BLOCK_HEADER
    assert "src/girder/" in lines
    app_line = next(line for line in lines if "app.py" in line)
    assert app_line == "  app.py (180 lines)"
    assert lines[-1].startswith("  ") and lines[2].endswith("/")


def test_format_block_lists_symbols(tmp_path: Path) -> None:
    root = make_tree(tmp_path)
    index = build_index(root)
    block = format_block(index, index.files, max_tokens=2000)
    assert "classes: App" in block
    assert "functions: main" in block


def test_scope_glob_slicing_includes_only_relevant_dirs(tmp_path: Path) -> None:
    root = make_tree(tmp_path)
    index = build_index(root)
    sliced = index.slice_by_globs(["src/**"])
    assert {f.path for f in sliced} == {"src/broken.py", "src/girder/app.py", "src/girder/db.py"}
    block = format_block(index, sliced, max_tokens=2000)
    assert "tests/" not in block and "README.md" not in block
    assert "src/" in block


def test_token_cap_drops_whole_directories_lowest_relevance_first() -> None:
    # src/core matches the scope (relevant); src/misc does not. A tight cap
    # must drop src/misc whole — never mid-line, never mid-file.
    sliced = _entries(
        ("src/core/engine.py", 400),
        ("src/core/util.py", 300),
        ("src/misc/notes.py", 200),
    )
    block = format_block(build_index(Path(".")), sliced, max_tokens=20)
    assert "notes.py" not in block  # whole directory dropped
    assert "engine.py" in block  # relevant directory survives
    assert "truncated" in block


def test_token_cap_never_cuts_midline() -> None:
    files = _entries(("src/a/one.py", 50), ("src/b/two.py", 50))
    block = format_block(build_index(Path(".")), files, max_tokens=15)
    for line in block.splitlines():
        if line.startswith("  "):  # file lines are complete
            assert line.endswith(")") and " (" in line


def test_empty_slice_renders_empty_block() -> None:
    assert format_block(build_index(Path(".")), (), max_tokens=2000) == ""


class _FakeTask:
    id = "t1"
    scope_globs: ClassVar[list[str]] = []


def test_codebase_index_guidance_disabled_is_passthrough(tmp_path: Path) -> None:
    limits = LimitsConfig(inject_index=False)

    async def run() -> str | None:
        return await codebase_index_guidance(
            "base guidance", None, _FakeTask(), tmp_path, limits  # type: ignore[arg-type]
        )

    assert asyncio.run(run()) == "base guidance"


def test_codebase_index_guidance_composes_block(tmp_path: Path) -> None:
    root = make_tree(tmp_path)
    limits = LimitsConfig()

    async def run() -> str | None:
        return await codebase_index_guidance(
            None, None, _FakeTask(), root, limits  # type: ignore[arg-type]
        )

    out = asyncio.run(run())
    assert out is not None and out.startswith(BLOCK_HEADER)


def test_codebase_index_round_trip_through_format(tmp_path: Path) -> None:
    import json

    root = make_tree(tmp_path)
    index = build_index(root)
    revived = CodebaseIndex.from_json_dict(json.loads(json.dumps(index.to_json_dict())))
    assert format_block(revived, revived.files, 2000) == format_block(
        index, index.files, 2000
    )
