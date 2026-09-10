"""WP-1.7 — ScopeGuard: R2 verdict policy, glob matching, path escapes."""

from __future__ import annotations

import pathlib

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


# ------------------------------------------------------- run_command path args


def test_run_command_redirect_out_of_scope_is_violation(scopes: TaskScopes) -> None:
    assert (
        check_tool_call(
            "run_command", {"cmd": "echo x > tests/test_new.py"}, scopes
        )
        is Verdict.VIOLATION
    )


@pytest.mark.parametrize(
    "cmd",
    [
        "echo x >> tests/test_new.py",
        "echo x 2> tests/test_new.py",
        "echo x &> tests/test_new.py",
        "echo x >tests/test_new.py",  # attached form
        "cat src/a.txt | tee tests/test_new.py",
        "tee tests/test_new.py < src/a.txt",
    ],
)
def test_run_command_write_target_out_of_scope_is_violation(
    scopes: TaskScopes, cmd: str
) -> None:
    assert check_tool_call("run_command", {"cmd": cmd}, scopes) is Verdict.VIOLATION


def test_run_command_fd_dup_is_not_a_path_arg(scopes: TaskScopes) -> None:
    assert check_tool_call("run_command", {"cmd": "python -m pytest 2>&1"}, scopes) is (
        Verdict.ALLOW
    )


def test_run_command_tee_in_scope_allowed(scopes: TaskScopes) -> None:
    assert check_tool_call("run_command", {"cmd": "tee src/widgets/new.py"}, scopes) is (
        Verdict.ALLOW
    )


def test_run_command_no_path_args_allowed(scopes: TaskScopes) -> None:
    assert check_tool_call("run_command", {"cmd": "python -m pytest -q"}, scopes) is (
        Verdict.ALLOW
    )


def test_run_command_read_of_existing_path_is_not_write_gated(
    tmp_path: pathlib.Path,
) -> None:
    """In-root path-shaped token, read-shaped: reads stay allowed (R2) even
    though the token falls outside write_globs... and even inside them it is
    ALLOW_LOGGED, never a write-policy hold. Existence is not probed on the
    host filesystem — run_command executes in the guest."""
    scopes = TaskScopes(
        write_globs=["src/widgets/**"],
        protected_globs=[".github/**"],
        root=str(tmp_path),
    )
    assert check_tool_call("run_command", {"cmd": "cat src/app.py"}, scopes) is (
        Verdict.ALLOW_LOGGED
    )


def test_run_command_in_root_read_shape_is_allow_logged_without_host_probe(
    scopes: TaskScopes,
) -> None:
    """Path classification is purely lexical: a token that lexically
    resolves inside the task root is a read (ALLOW_LOGGED) even though the
    file does not exist on the orchestrator host (§6.6 R2, plan §8.5)."""
    assert check_tool_call("run_command", {"cmd": "cat src/app.py"}, scopes) is (
        Verdict.ALLOW_LOGGED
    )


def test_run_command_in_root_read_path_is_logged() -> None:
    """The read audit trail is live: in-root reads yield ALLOW_LOGGED with
    the conventional ``/workspace`` root, not a bare ALLOW."""
    scopes = TaskScopes(write_globs=["src/widgets/**"], protected_globs=[".github/**"])
    assert check_tool_call("run_command", {"cmd": "cat src/app.py"}, scopes) is (
        Verdict.ALLOW_LOGGED
    )


def test_run_command_protected_read_under_absolute_root_is_violation() -> None:
    scopes = TaskScopes(write_globs=["src/**"], protected_globs=[".github/**"])
    assert check_tool_call(
        "run_command", {"cmd": "cat /workspace/.github/workflows/ci.yml"}, scopes
    ) is Verdict.VIOLATION


def test_run_command_escape_token_is_violation(scopes: TaskScopes) -> None:
    assert check_tool_call("run_command", {"cmd": "cat ../../etc/passwd"}, scopes) is (
        Verdict.VIOLATION
    )


def test_run_command_nonexistent_in_root_token_is_still_a_read(scopes: TaskScopes) -> None:
    """A path-shaped token inside the root that does not exist anywhere is
    still read-shaped (ALLOW_LOGGED); the Layer 2/3 audits backstop
    indirection."""
    assert check_tool_call("run_command", {"cmd": "python -m pytest build/app.py"}, scopes) is (
        Verdict.ALLOW_LOGGED
    )


def test_run_command_protected_write_target_is_violation(scopes: TaskScopes) -> None:
    assert check_tool_call("run_command", {"cmd": "echo x > .github/x.yml"}, scopes) is (
        Verdict.VIOLATION
    )


def test_run_command_protected_read_token_is_violation(tmp_path: pathlib.Path) -> None:
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "ci.yml").write_text("on: push\n")
    scopes = TaskScopes(
        write_globs=["src/**"], protected_globs=[".github/**"], root=str(tmp_path)
    )
    assert check_tool_call("run_command", {"cmd": "cat .github/ci.yml"}, scopes) is (
        Verdict.VIOLATION
    )


def test_run_command_redirect_escape_is_violation(scopes: TaskScopes) -> None:
    assert (
        check_tool_call("run_command", {"cmd": "echo x > ../escape.py"}, scopes)
        is Verdict.VIOLATION
    )


def test_run_command_unparsable_shell_falls_back_to_allow(scopes: TaskScopes) -> None:
    """shlex cannot split it — no path args extracted; denylist + Layer 2/3
    audits own the rest."""
    assert check_tool_call("run_command", {"cmd": "echo 'unbalanced"}, scopes) is (
        Verdict.ALLOW
    )


# ------------------------------------------- run_command multi-token evaluation


def _default_protected_scopes(strict: bool = False) -> TaskScopes:
    """TaskScopes with the real default protected_read_paths from
    src/girder/config.py (the way production builds scopes)."""
    return TaskScopes(
        write_globs=["src/**"],
        protected_globs=[
            ".github/**",
            "openspec/**",
            ".git/**",
            ".env*",
            "**/*.pem",
            "**/*credentials*",
        ],
        strict_read_scope=strict,
    )


def test_run_command_multi_path_later_protected_token_is_violation() -> None:
    """§6.6 R2: every read-shaped token is evaluated — the first clean token
    must not short-circuit a later protected path."""
    scopes = _default_protected_scopes()
    assert check_tool_call(
        "run_command", {"cmd": "cat src/a.py .github/workflows/ci.yml"}, scopes
    ) is Verdict.VIOLATION
    assert check_tool_call(
        "run_command", {"cmd": "grep x src/a.py .env"}, scopes
    ) is Verdict.VIOLATION


def test_run_command_multi_path_all_clean_is_allow_logged() -> None:
    scopes = _default_protected_scopes()
    assert check_tool_call(
        "run_command", {"cmd": "cat src/a.py src/b.py"}, scopes
    ) is Verdict.ALLOW_LOGGED


def test_run_command_first_token_clean_second_escape_is_violation() -> None:
    scopes = _default_protected_scopes()
    assert check_tool_call(
        "run_command", {"cmd": "cat src/a.py ../../etc/passwd"}, scopes
    ) is Verdict.VIOLATION


def test_run_command_strict_read_scope_holds_out_of_scope_reads() -> None:
    """strict_read_scope=True applies to run_command reads too (§6.6 R2):
    an in-root, out-of-scope path token is a VIOLATION, matching read_file."""
    strict = _default_protected_scopes(strict=True)
    assert check_tool_call(
        "run_command", {"cmd": "cat other/x.py"}, strict
    ) is Verdict.VIOLATION
    lax = _default_protected_scopes(strict=False)
    assert check_tool_call(
        "run_command", {"cmd": "cat other/x.py"}, lax
    ) is Verdict.ALLOW_LOGGED


def test_run_command_strict_read_scope_keeps_in_scope_reads_allowed() -> None:
    strict = _default_protected_scopes(strict=True)
    assert check_tool_call(
        "run_command", {"cmd": "cat src/a.py"}, strict
    ) is Verdict.ALLOW


# ------------------------------------------------------- apply_patch diff body


def _patch(*lines: str) -> dict[str, object]:
    return {"path": "src/widget.py", "unified_diff": "\n".join(lines)}


def test_apply_patch_in_scope_diff_allowed(scopes: TaskScopes) -> None:
    diff = _patch(
        "--- a/src/widgets/new.py",
        "+++ b/src/widgets/new.py",
        "@@ -1 +1 @@",
        "-old",
        "+new",
    )
    assert check_tool_call("apply_patch", diff, scopes) is Verdict.ALLOW


def test_apply_patch_diff_targeting_out_of_scope_path_is_violation(
    scopes: TaskScopes,
) -> None:
    # declared path is in scope, but the diff body sneaks in a test-signal
    # file (SC-02 shape): the whole call must be held
    diff = _patch(
        "--- a/src/widget.py",
        "+++ b/src/widget.py",
        "--- /dev/null",
        "+++ b/tests/test_evade.py",
        "+def test_x(): pass",
    )
    assert check_tool_call("apply_patch", diff, scopes) is Verdict.VIOLATION


def test_apply_patch_diff_targeting_protected_path_is_violation(
    scopes: TaskScopes,
) -> None:
    diff = _patch(
        "--- a/.github/workflows/ci.yml",
        "+++ b/.github/workflows/ci.yml",
        "-on: push",
    )
    assert check_tool_call("apply_patch", diff, scopes) is Verdict.VIOLATION


def test_apply_patch_devnull_lines_are_skipped(scopes: TaskScopes) -> None:
    diff = _patch(
        "--- /dev/null",
        "+++ b/src/widgets/created.py\t2026-01-01 00:00:00",
        "+x = 1",
    )
    assert check_tool_call("apply_patch", diff, scopes) is Verdict.ALLOW
    # a delete (--- real path, +++ /dev/null) is still a write target
    delete = _patch(
        "--- a/src/other.py",
        "+++ /dev/null",
    )
    assert check_tool_call("apply_patch", delete, scopes) is Verdict.VIOLATION


def test_apply_patch_diff_path_escape_is_violation(scopes: TaskScopes) -> None:
    diff = _patch(
        "--- a/../evil.py",
        "+++ b/../evil.py",
        "-x",
        "+y",
    )
    assert check_tool_call("apply_patch", diff, scopes) is Verdict.VIOLATION


def test_apply_patch_unparsable_diff_falls_back_to_declared_path(
    scopes: TaskScopes,
) -> None:
    """No parseable +++/--- targets: the declared-path policy applies alone."""
    no_targets = _patch("this is not a diff")
    assert check_tool_call("apply_patch", no_targets, scopes) is Verdict.ALLOW
    assert (
        check_tool_call(
            "apply_patch", {"path": "README.md", "unified_diff": "not a diff"}, scopes
        )
        is Verdict.VIOLATION
    )


def test_apply_patch_quoted_paths_unquoted(scopes: TaskScopes) -> None:
    diff = _patch(
        '--- "a/src/widgets/q.py"',
        '+++ "b/src/widgets/q.py"',
        "-x",
        "+y",
    )
    assert check_tool_call("apply_patch", diff, scopes) is Verdict.ALLOW
