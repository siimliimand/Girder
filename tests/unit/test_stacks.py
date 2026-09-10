"""Unit tests for the stack plugin registry (WS-07 Part A).

These are regression guards: the python-3.12 plugin must produce byte-identical
commands/patterns to the formerly hardcoded behavior so SC-01 stays green.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from girder.config import load_settings
from girder.stacks import STACK_REGISTRY, VERIFY_XML, StackPlugin
from girder.stacks.python import _AST_OUTLINE_SNIPPET, PythonPlugin


def test_registry_contains_python_312() -> None:
    assert "python-3.12" in STACK_REGISTRY
    assert isinstance(STACK_REGISTRY["python-3.12"], PythonPlugin)
    assert isinstance(STACK_REGISTRY["python-3.12"], StackPlugin)


def test_python_plugin_runner_image() -> None:
    assert STACK_REGISTRY["python-3.12"].runner_image() == "girder-runner:python-3.12"


def test_python_plugin_test_command_byte_identical() -> None:
    """Matches the former verify_cmd() output exactly (§5.5)."""
    plugin = STACK_REGISTRY["python-3.12"]
    assert plugin.test_command("python3") == [
        "python3",
        "-m",
        "pytest",
        "-q",
        f"--junitxml=/workspace/{VERIFY_XML}",
        "-p",
        "no:cacheprovider",
    ]
    assert plugin.test_command() == plugin.test_command("python3")


def test_python_plugin_symbol_outline_command_byte_identical() -> None:
    assert STACK_REGISTRY["python-3.12"].symbol_outline_command("src/a.py") == [
        "python3",
        "-c",
        _AST_OUTLINE_SNIPPET,
        "src/a.py",
    ]


def test_python_plugin_test_signal_patterns_match_config_default() -> None:
    from girder.config import ProjectConfig

    assert (
        STACK_REGISTRY["python-3.12"].test_signal_patterns()
        == ProjectConfig().test_signal_patterns
    )


def test_python_plugin_package_cache_mounts() -> None:
    assert STACK_REGISTRY["python-3.12"].package_cache_mounts() == {
        "/var/cache/orchestrator/pip": "/cache/pip"
    }


def test_verify_xml_filename_unchanged() -> None:
    assert VERIFY_XML == ".girder-verify.xml"


def test_load_settings_rejects_unknown_stack(tmp_path: Path) -> None:
    toml = tmp_path / "girder.toml"
    toml.write_text('[project]\nstack = "ruby-3.4"\n')
    with pytest.raises(ValueError, match=r"project\.stack.*ruby-3\.4"):
        load_settings(toml)


def test_load_settings_accepts_registered_stack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GIRDER_ALLOW_NO_ROLES", "1")  # role-less dev opt-out
    toml = tmp_path / "girder.toml"
    toml.write_text('[project]\nstack = "python-3.12"\n')
    assert load_settings(toml).project.stack == "python-3.12"
