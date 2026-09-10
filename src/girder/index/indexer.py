"""Build a structural index of a worktree (WP 8.4, R-SP8-4).

Three views, all derived with :mod:`ast` for Python files (no execution,
no imports of indexed code):

* **File tree** — every indexed file with its line count, grouped by directory.
* **Symbol map** — top-level classes and functions with defining line numbers.
* **Import graph** — ``import x`` / ``from x import y`` module targets per file.

Pure-Python extraction only in this workstream (WS-07 generalizes per-stack
later). Non-Python files are listed (path + line count) so the file tree is
complete, but carry no symbol data.

The result serializes to JSON for the ``tasks.codebase_index_json`` column
(migration 014) and is rebuilt on every attempt — never cached.
"""

from __future__ import annotations

import ast
import fnmatch
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Directories never entered. Hidden dirs (dotfiles) are also skipped — that
# covers .git plus tool caches in one rule.
SKIP_DIRS = frozenset({"__pycache__", ".venv", "venv", "node_modules", ".mypy_cache"})

MAX_BYTES = 4_000_000  # safety valve: skip absurdly large files
MAX_LINES = 20_000


@dataclass(frozen=True)
class FileEntry:
    """One indexed file. ``symbols``/``imports`` are empty for non-Python."""

    path: str  # repo-relative posix path
    lines: int
    classes: tuple[tuple[str, int], ...] = ()  # (name, lineno), source order
    functions: tuple[tuple[str, int], ...] = ()
    imports: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "lines": self.lines,
            "classes": [list(s) for s in self.classes],
            "functions": [list(s) for s in self.functions],
            "imports": list(self.imports),
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> FileEntry:
        return FileEntry(
            path=str(d["path"]),
            lines=int(d["lines"]),
            classes=tuple((str(n), int(ln)) for n, ln in d["classes"]),
            functions=tuple((str(n), int(ln)) for n, ln in d["functions"]),
            imports=tuple(str(m) for m in d["imports"]),
        )


@dataclass(frozen=True)
class CodebaseIndex:
    """A whole-worktree structural snapshot at one moment in time."""

    root_name: str
    files: tuple[FileEntry, ...]

    def to_json_dict(self) -> dict[str, object]:
        return {
            "root_name": self.root_name,
            "files": [f.to_dict() for f in self.files],
        }

    @staticmethod
    def from_json_dict(d: dict[str, Any]) -> CodebaseIndex:
        return CodebaseIndex(
            root_name=str(d["root_name"]),
            files=tuple(FileEntry.from_dict(f) for f in d["files"]),
        )

    def directories(self) -> dict[str, list[FileEntry]]:
        """Group files by their containing directory ("." for the root)."""
        groups: dict[str, list[FileEntry]] = {}
        for f in self.files:
            parent = str(Path(f.path).parent)
            groups.setdefault("." if parent == "." else parent, []).append(f)
        return groups

    def slice_by_globs(self, scope_globs: list[str]) -> tuple[FileEntry, ...]:
        """Files whose repo-relative path matches any scope glob.

        An empty glob list keeps everything (task touches the whole repo).
        """
        if not scope_globs:
            return self.files
        return tuple(
            f for f in self.files if any(fnmatch.fnmatch(f.path, g) for g in scope_globs)
        )


def _walk_files(root: Path) -> list[Path]:
    out: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(part.startswith(".") or part in SKIP_DIRS for part in rel.parts):
            continue
        out.append(path)
    return out


def _python_symbols(path: Path) -> tuple[
    tuple[tuple[str, int], ...], tuple[tuple[str, int], ...], tuple[str, ...]
]:
    """Top-level classes, functions, and imported modules via ``ast.parse``."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return (), (), ()
    classes: list[tuple[str, int]] = []
    functions: list[tuple[str, int]] = []
    imports: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            classes.append((node.name, node.lineno))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append((node.name, node.lineno))
        elif isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return tuple(classes), tuple(functions), tuple(sorted(imports))


def build_index(root: Path) -> CodebaseIndex:
    """Index *root* (an attempt worktree) synchronously. No caching."""
    files: list[FileEntry] = []
    for path in _walk_files(root):
        try:
            lines = sum(1 for _ in open(path, encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if lines > MAX_LINES or path.stat().st_size > MAX_BYTES:
            continue
        classes: tuple[tuple[str, int], ...] = ()
        functions: tuple[tuple[str, int], ...] = ()
        imports: tuple[str, ...] = ()
        if path.suffix == ".py":
            classes, functions, imports = _python_symbols(path)
        files.append(
            FileEntry(
                path=path.relative_to(root).as_posix(),
                lines=lines,
                classes=classes,
                functions=functions,
                imports=imports,
            )
        )
    files.sort(key=lambda f: f.path)
    return CodebaseIndex(root_name=root.name, files=tuple(files))


def empty_index(root_name: str = "") -> CodebaseIndex:
    return CodebaseIndex(root_name=root_name, files=())


# Re-exported for callers that only want the dataclasses.
__all__ = ["SKIP_DIRS", "CodebaseIndex", "FileEntry", "build_index", "empty_index"]
