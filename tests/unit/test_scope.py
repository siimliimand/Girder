"""WP-1.7 — ScopeGuard: R2 verdict policy, glob matching, path escapes."""

from __future__ import annotations

import pytest

from girder.guard.scope import (
    TaskScopes,
    Verdict,
    check_tool_call,
    glob_to_regex,
    path_matches,
    resolve_path,
)


@pytest.fixture
def scopes() -> TaskScopes:
    return TaskScopes(
        write_globs=["src/widget.py", "src/widgets/**"],
        protected_globs=[".github/**", "openspec/**", ".env*", "**/*.pem"],
    )


# ------------------------------------------------------------------- glob tests


@pytest.mark.parametrize(
    ("path", "pattern", "expected"),
    [
        ("src/widget.py", "src/widget.py", True),
        ("src/widgets/a.py", "src/widgets/**", True),
        ("src/widgets/sub/b.py", "src/widgets/**", True),
        ("src/widgets", "src/widgets/**", True),  # bare dir matches its ** form
        ("src/other.py", "src/widgets/**", False),
        ("tests/test_x.py", "**/test_*.py", True),
        ("src/deep/nested/test_x.py", "**/test_*.py", True),
        ("tests/test_x/utils.py", "**/test_*.py", False),  # * does not cross '/'
        ("src", "src", True),  # literal dir matches subtree
        ("src/a/b.py", "src", True),
        ("srcx/a.py", "src", False),  # prefix must respect boundary
        ("a/b/c.py", "a/*/c.py", True),
        ("a/b/d/c.py", "a/*/c.py", False),
    ],
)
def test_path_matches(path: str, pattern: str, expected: bool) -> None:
    assert path_matches(path, pattern) is expected


def test_glob_to_regex_anchors() -> None:
    assert glob_to_regex("**/*.py").match("x/y/z.py")
    assert glob_to_regex("**/*.py").match("z.py")
    assert not glob_to_regex("*.py").match("a/b.py")


# ------------------------------------------------------------------ path tests


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("src/a.py", "src/a.py"),
        ("./src/a.py", "src/a.py"),
        ("/workspace/src/a.py", "src/a.py"),
        ("src/../src/a.py", "src/a.py"),
        ("../outside.py", None),
        ("/etc/passwd", None),
        ("/workspace/../../etc/passwd", None),
    ],
)
def test_resolve_path(raw: str, expected: str | None) -> None:
    assert resolve_path(raw, "/workspace") == expected


# ---------------------------------------------------------------- verdict tests


def test_write_in_scope_allowed(scopes: TaskScopes) -> None:
    assert check_tool_call("write_file", {"path": "src/widget.py"}, scopes) is Verdict.ALLOW
    assert (
        check_tool_call("write_file", {"path": "/workspace/src/widgets/new.py"}, scopes)
        is Verdict.ALLOW
    )


def test_write_out_of_scope_violation(scopes: TaskScopes) -> None:
    assert check_tool_call("write_file", {"path": "src/other.py"}, scopes) is Verdict.VIOLATION
    assert check_tool_call("apply_patch", {"path": "README.md"}, scopes) is Verdict.VIOLATION


def test_reads_logged_not_held(scopes: TaskScopes) -> None:
    assert check_tool_call("read_file", {"path": "src/other.py"}, scopes) is Verdict.ALLOW_LOGGED
    assert check_tool_call("ripgrep", {"path": ""}, scopes) is Verdict.ALLOW_LOGGED
    assert check_tool_call("find_files", {"glob": "src/**/*.py"}, scopes) is Verdict.ALLOW_LOGGED


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/ci.yml",
        "openspec/proposals/x.md",
        ".env.local",
        "certs/server.pem",
    ],
)
def test_protected_reads_always_violation(scopes: TaskScopes, path: str) -> None:
    assert check_tool_call("read_file", {"path": path}, scopes) is Verdict.VIOLATION
    assert check_tool_call("ripgrep", {"path": path}, scopes) is Verdict.VIOLATION


def test_protected_write_is_violation_even_if_in_scope() -> None:
    scopes = TaskScopes(write_globs=["**"], protected_globs=[".github/**"])
    assert (
        check_tool_call("write_file", {"path": ".github/workflows/ci.yml"}, scopes)
        is Verdict.VIOLATION
    )


def test_path_escape_violation(scopes: TaskScopes) -> None:
    assert check_tool_call("write_file", {"path": "../../etc/passwd"}, scopes) is Verdict.VIOLATION
    assert check_tool_call("read_file", {"path": "/etc/shadow"}, scopes) is Verdict.VIOLATION


def test_strict_read_scope_literal_behavior() -> None:
    scopes = TaskScopes(
        write_globs=["src/widget.py"],
        protected_globs=[".github/**"],
        strict_read_scope=True,
    )
    assert check_tool_call("read_file", {"path": "src/other.py"}, scopes) is Verdict.VIOLATION
    assert check_tool_call("read_file", {"path": "src/widget.py"}, scopes) is Verdict.ALLOW


def test_no_path_tools_allowed(scopes: TaskScopes) -> None:
    assert check_tool_call("mark_task_complete", {}, scopes) is Verdict.ALLOW
    assert check_tool_call("request_spec_amendment", {"reason": "x"}, scopes) is Verdict.ALLOW
