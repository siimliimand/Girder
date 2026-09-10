"""WS-07 Part A tests: stack plugin architecture, Node, Go.

Covers (ws07-multi-stack.md §Tests):

* registry: every built-in stack validates; unknown ``project.stack`` is
  rejected by ``load_settings()`` with a clear error;
* ``PythonPlugin`` produces byte-identical ``test_command`` / patterns to the
  pre-plugin hardcoded behavior (SC-01 regression guard);
* Node/Go plugins: command construction, pattern globs, cache mounts;
* the TS e2e fixture (``tests/fixtures/e2e-target-ts/``) is structurally
  sound (SC-20 prerequisites).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from girder.config import ProjectConfig, load_settings
from girder.orchestrator.verification import VERIFY_CMD
from girder.stacks import (
    STACK_REGISTRY,
    VERIFY_XML,
    StackPlugin,
    UnknownStackError,
    get_stack,
)
from girder.stacks.golang import GoPlugin
from girder.stacks.node import NodePlugin
from girder.stacks.python import _AST_OUTLINE_SNIPPET, PythonPlugin

FIXTURE_TS = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "e2e-target-ts"

BUILTINS = ["python-3.12", "node-20", "go-1.23"]


# ---------------------------------------------------------------- registry


def test_registry_contains_builtins() -> None:
    for name in BUILTINS:
        assert name in STACK_REGISTRY
        assert STACK_REGISTRY[name].name == name


def test_registry_python_plugin_type() -> None:
    assert isinstance(STACK_REGISTRY["python-3.12"], PythonPlugin)
    assert isinstance(STACK_REGISTRY["python-3.12"], StackPlugin)


@pytest.mark.parametrize("name", BUILTINS)
def test_builtin_plugins_are_complete(name: str) -> None:
    plugin = get_stack(name)
    assert plugin.runner_image().startswith("girder-runner:")
    cmd = plugin.test_command()
    assert cmd and cmd[0]
    assert any(VERIFY_XML in part for part in cmd) or name == "node-20"
    assert plugin.test_signal_patterns()
    assert plugin.package_cache_mounts()
    outline = plugin.symbol_outline_command("src/x.ts")
    assert "src/x.ts" in outline


def test_get_stack_unknown_is_clear_error() -> None:
    with pytest.raises(UnknownStackError, match=r"cobol-85.*known stacks") as exc:
        get_stack("cobol-85")
    listed = exc.value.args[0].split("known stacks: ")[1]
    assert ast.literal_eval(listed) == sorted(STACK_REGISTRY)


# ---------------------------------------------------------------- settings


# ------------------------------------------------- load_settings validation


def test_load_settings_rejects_unknown_stack(tmp_path: Path) -> None:
    toml = tmp_path / "girder.toml"
    toml.write_text('[project]\nstack = "cobol-85"\n')
    with pytest.raises(
        # Which guard fires first varies (pydantic model validator vs
        # load_settings pre-flight); both are hard configuration errors.
        ValueError,
        match=r"project\.stack 'cobol-85' is not a (?:registered|known) stack",
    ):
        load_settings(toml)


@pytest.mark.parametrize("name", BUILTINS)
def test_load_settings_accepts_builtins(
    tmp_path: Path, name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GIRDER_ALLOW_NO_ROLES", "1")
    monkeypatch.setenv("GIRDER_PROJECT__STACK", name)
    settings = load_settings()
    assert settings.project.stack == name


# ------------------------------------------- Python regression guard (SC-01)


def test_python_test_command_byte_identical() -> None:
    for pybin in (None, "python3", "/usr/bin/python3.12"):
        expected = [
            pybin or "python3",
            "-m",
            "pytest",
            "-q",
            f"--junitxml=/workspace/{VERIFY_XML}",
            "-p",
            "no:cacheprovider",
        ]
        assert PythonPlugin().test_command(pybin) == expected
        # Same argv the verify loop uses for the Python stack (post-split).
        assert PythonPlugin().test_command("python3") == VERIFY_CMD


def test_python_patterns_identical_to_config_defaults() -> None:
    assert PythonPlugin().test_signal_patterns() == ProjectConfig().test_signal_patterns
    assert PythonPlugin().test_signal_patterns()[0] == "tests/**"


def test_python_runner_image_and_caches() -> None:
    plugin = PythonPlugin()
    assert plugin.runner_image() == "girder-runner:python-3.12"
    assert plugin.package_cache_mounts() == {"/var/cache/orchestrator/pip": "/cache/pip"}


def test_python_symbol_outline_command_byte_identical() -> None:
    assert STACK_REGISTRY["python-3.12"].symbol_outline_command("src/a.py") == [
        "python3",
        "-c",
        _AST_OUTLINE_SNIPPET,
        "src/a.py",
    ]


def test_verify_xml_filename_unchanged() -> None:
    assert VERIFY_XML == ".girder-verify.xml"


# ---------------------------------------------------------------- Node / Go


def test_node_command_construction() -> None:
    plugin = NodePlugin()
    cmd = plugin.test_command()
    assert cmd[:3] == ["npx", "jest", "--ci"]
    assert "jest-junit" in " ".join(cmd)
    # python_bin is meaningless on this stack — ignored, not errored.
    assert plugin.test_command("python3") == cmd
    outline = plugin.symbol_outline_command("src/calculator.ts")
    assert outline[0] == "node" and "src/calculator.ts" in outline


def test_node_patterns_and_caches() -> None:
    plugin = NodePlugin()
    patterns = plugin.test_signal_patterns()
    assert "**/*.test.ts" in patterns and "jest.config.*" in patterns
    assert plugin.package_cache_mounts() == {"/var/cache/orchestrator/npm": "/cache/npm"}
    assert plugin.runner_image() == "girder-runner:node-20"


def test_go_command_construction() -> None:
    plugin = GoPlugin()
    cmd = plugin.test_command()
    assert cmd[0] == "gotestsum" and "--junitfile" in cmd[1]
    assert f"/workspace/{VERIFY_XML}" in cmd[1]
    assert cmd[cmd.index("--") + 1 :] == ["go", "test", "./...", "-v"]
    assert plugin.test_command("python3") == cmd  # python_bin ignored
    assert plugin.symbol_outline_command("pkg/calc/calc.go") == [
        "go",
        "doc",
        "-all",
        "pkg/calc/calc.go",
    ]


def test_go_patterns_and_caches() -> None:
    plugin = GoPlugin()
    assert plugin.test_signal_patterns() == ["**/*_test.go"]
    assert plugin.package_cache_mounts() == {
        "/var/cache/orchestrator/go": "/root/go/pkg/mod"
    }
    assert plugin.runner_image() == "girder-runner:go-1.23"


# ------------------------------------------------- TS fixture sanity (SC-20)


def test_ts_fixture_structure() -> None:
    assert (FIXTURE_TS / "src" / "calculator.ts").is_file()
    calc_src = (FIXTURE_TS / "src" / "calculator.ts").read_text()
    # `divide` is deliberately absent as a method (docstring may mention it).
    assert "divide(a: number" not in calc_src
    assert "add(a: number" in calc_src
    test_file = (FIXTURE_TS / "tests" / "calculator.test.ts").read_text()
    assert "new Calculator().add(2, 3)" in test_file
    pkg = (FIXTURE_TS / "package.json").read_text()
    # The real reporter package is `jest-junit` (no `@jest/junit-reporter`).
    assert "jest-junit" in pkg
