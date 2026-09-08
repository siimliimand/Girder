"""Unit tests for OpenSpec structural validation (impl-plan §6.9, R7)."""

from __future__ import annotations

import pytest

from girder.config import ProjectConfig
from girder.db.models import TaskType
from girder.specs.validator import OPENSPEC_TEMPLATE, SPEC_SCHEMA, parse_spec

GOLDEN = """\
---
schema: girder.openspec/v1
title: Add user authentication
intent: Users must be able to log in with a token.
tasks:
  - id: auth-endpoint
    title: Implement token endpoint
    type: code_change
    scope_globs: ["src/auth/**"]
    success_criteria:
      - "POST /token returns a JWT for valid credentials"
      - "expired tokens are rejected with 401"
    depends_on: []
---
Optional human-readable narrative goes here.
"""


def test_template_parses() -> None:
    """Template rot guard: the canonical template must always be valid (§6.9)."""
    doc = parse_spec(OPENSPEC_TEMPLATE)
    assert doc.title
    assert doc.intent
    assert doc.tasks
    assert all(task.type in TaskType for task in doc.tasks)


def test_golden_document() -> None:
    doc = parse_spec(GOLDEN)
    assert doc.title == "Add user authentication"
    assert doc.intent == "Users must be able to log in with a token."
    assert doc.body_md == "Optional human-readable narrative goes here.\n"
    assert doc.raw == GOLDEN
    (task,) = doc.tasks
    assert task.id == "auth-endpoint"
    assert task.type is TaskType.CODE_CHANGE
    assert task.scope_globs == ["src/auth/**"]
    assert len(task.success_criteria) == 2
    assert task.depends_on == []


def test_empty_body_is_allowed() -> None:
    doc = parse_spec(GOLDEN.replace("Optional human-readable narrative goes here.\n", ""))
    assert doc.body_md == ""


def _doc(**overrides: str) -> str:
    """Golden document with one frontmatter line replaced."""
    text = GOLDEN
    for old, new in overrides.items():
        text = text.replace(old, new)
    return text


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        (GOLDEN.replace("schema: girder.openspec/v1\n", ""), "schema"),
        (GOLDEN.replace("girder.openspec/v1", "girder.openspec/v2"), "schema"),
        (GOLDEN.replace("title: Add user authentication\n", ""), "title"),
        (GOLDEN.replace("title: Add user authentication", "title: \"\""), "title"),
        (GOLDEN.replace("intent: Users must be able to log in with a token.\n", ""), "intent"),
        (_doc(**{"id: auth-endpoint": "id: Auth-Endpoint"}), "id"),
        (_doc(**{"type: code_change": "type: refactor_plus"}), "type"),
        (_doc(**{'scope_globs: ["src/auth/**"]': 'scope_globs: []'}), "scope_globs"),
        (_doc(**{'scope_globs: ["src/auth/**"]': 'scope_globs: ["/abs/**"]'}), "relative"),
        (_doc(**{'scope_globs: ["src/auth/**"]': 'scope_globs: ["src/../etc/**"]'}), ".."),
        (
            GOLDEN.replace(
                "      - \"POST /token returns a JWT for valid credentials\"\n", ""
            ).replace("      - \"expired tokens are rejected with 401\"\n", ""),
            "success_criteria",
        ),
        (_doc(**{"depends_on: []": "depends_on: [nope]"}), "unknown"),
        ("---\nnot yaml: [unclosed\n---\nbody\n", "YAML"),
        ("no frontmatter here\n", "'---'"),
    ],
)
def test_malformations(text: str, needle: str) -> None:
    with pytest.raises(ValueError) as excinfo:
        parse_spec(text)
    errors = excinfo.value.errors  # type: ignore[attr-defined]
    assert any(needle in e for e in errors), errors


def test_duplicate_task_id() -> None:
    text = (
        "---\n"
        "schema: girder.openspec/v1\n"
        "title: t\nintent: i\ntasks:\n"
        "  - id: same\n    title: a\n    type: fix\n"
        "    scope_globs: ['a/**']\n    success_criteria: ['c']\n"
        "  - id: same\n    title: b\n    type: fix\n"
        "    scope_globs: ['a/**']\n    success_criteria: ['c']\n"
        "---\n"
    )
    with pytest.raises(ValueError) as excinfo:
        parse_spec(text)
    assert any("duplicate" in e and "same" in e for e in excinfo.value.errors)  # type: ignore[attr-defined]


def test_cycle_named() -> None:
    text = (
        "---\n"
        "schema: girder.openspec/v1\n"
        "title: t\nintent: i\ntasks:\n"
        "  - id: a\n    title: x\n    type: fix\n"
        "    scope_globs: ['a/**']\n    success_criteria: ['c']\n    depends_on: [b]\n"
        "  - id: b\n    title: y\n    type: fix\n"
        "    scope_globs: ['b/**']\n    success_criteria: ['c']\n    depends_on: [a]\n"
        "---\n"
    )
    with pytest.raises(ValueError) as excinfo:
        parse_spec(text)
    errors = excinfo.value.errors  # type: ignore[attr-defined]
    assert any("cycle" in e and "a" in e and "b" in e for e in errors), errors


def test_aggregates_all_errors() -> None:
    text = (
        "---\n"
        "schema: wrong\n"
        "intent: \"\"\n"
        "tasks: []\n"
        "---\n"
    )
    with pytest.raises(ValueError) as excinfo:
        parse_spec(text)
    assert len(excinfo.value.errors) >= 3  # type: ignore[attr-defined]


def test_spec_path_is_protected_read() -> None:
    """Phase 1 DoD: openspec/** stays on the agent protected-read list."""
    assert "openspec/**" in ProjectConfig().protected_read_paths


def test_schema_constant() -> None:
    assert SPEC_SCHEMA == "girder.openspec/v1"
