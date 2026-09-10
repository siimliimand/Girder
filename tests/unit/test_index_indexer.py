"""Unit tests for the codebase indexer (WP 8.4, R-SP8-4).

A fixture tree on disk; no DB, no containers.
"""

from __future__ import annotations

from pathlib import Path

from girder.index.indexer import CodebaseIndex, build_index


def make_tree(root: Path) -> Path:
    (root / "src" / "girder").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "girder" / "app.py").write_text(
        "import os\n"
        "from girder.db import repo\n"
        "\n"
        "class App:\n"
        "    pass\n"
        "\n"
        "def main() -> None:\n"
        "    pass\n"
    )
    (root / "src" / "girder" / "db.py").write_text("def connect():\n    pass\n")
    (root / "tests" / "test_app.py").write_text("def test_app():\n    assert True\n")
    (root / "README.md").write_text("# fixture\n")
    (root / "src" / "broken.py").write_text("def oops(:\n")  # SyntaxError
    (root / "src" / "__pycache__").mkdir()
    (root / "src" / "__pycache__" / "junk.py").write_text("x = 1\n")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[core]\n")
    return root


def test_file_tree_completeness_and_skips(tmp_path: Path) -> None:
    root = make_tree(tmp_path)
    index = build_index(root)
    paths = {f.path for f in index.files}
    assert paths == {
        "README.md",
        "src/broken.py",
        "src/girder/app.py",
        "src/girder/db.py",
        "tests/test_app.py",
    }
    # line counts
    app = next(f for f in index.files if f.path.endswith("app.py"))
    assert app.lines == 8


def test_symbol_map_classes_functions_line_numbers(tmp_path: Path) -> None:
    root = make_tree(tmp_path)
    index = build_index(root)
    app = next(f for f in index.files if f.path == "src/girder/app.py")
    assert app.classes == (("App", 4),)
    assert app.functions == (("main", 7),)
    dbf = next(f for f in index.files if f.path == "src/girder/db.py")
    assert dbf.classes == ()
    assert dbf.functions == (("connect", 1),)


def test_import_graph_edges(tmp_path: Path) -> None:
    root = make_tree(tmp_path)
    index = build_index(root)
    app = next(f for f in index.files if f.path == "src/girder/app.py")
    assert app.imports == ("girder.db", "os")
    readme = next(f for f in index.files if f.path == "README.md")
    assert readme.imports == () and readme.classes == ()


def test_syntax_error_file_is_listed_without_symbols(tmp_path: Path) -> None:
    root = make_tree(tmp_path)
    index = build_index(root)
    broken = next(f for f in index.files if f.path == "src/broken.py")
    assert broken.classes == () and broken.functions == () and broken.imports == ()


def test_json_round_trip(tmp_path: Path) -> None:
    import json

    root = make_tree(tmp_path)
    index = build_index(root)
    revived = CodebaseIndex.from_json_dict(json.loads(json.dumps(index.to_json_dict())))
    assert revived == index


def test_slice_by_globs_empty_keeps_everything(tmp_path: Path) -> None:
    root = make_tree(tmp_path)
    index = build_index(root)
    assert index.slice_by_globs([]) == index.files


def test_directories_grouping(tmp_path: Path) -> None:
    root = make_tree(tmp_path)
    index = build_index(root)
    dirs = index.directories()
    assert set(dirs) == {".", "src", "src/girder", "tests"}
    assert {f.path for f in dirs["."]} == {"README.md"}
