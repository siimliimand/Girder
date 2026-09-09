"""Tests for the decomposer: spec -> wave-0 tasks (WP 3.4)."""

from __future__ import annotations

import pytest

from girder.db import repo
from girder.db.models import TaskType
from girder.specs.decomposer import DecompositionError, _extract_slice, decompose_spec
from girder.specs.validator import OPENSPEC_TEMPLATE, SpecDocument, TaskSpec, parse_spec


def _spec_text(task_block: str) -> str:
    return (
        "---\n"
        "schema: girder.openspec/v1\n"
        "title: Add user authentication\n"
        "intent: Users must be able to log in with a token.\n"
        "tasks:\n"
        f"{task_block}"
        "---\n"
        "## Narrative\n\n"
        "Context for the implementers.\n"
    )


def _task_block(with_sections: bool) -> str:
    block = (
        "  - id: auth-endpoint\n"
        "    title: Implement token endpoint\n"
        "    type: code_change\n"
        '    scope_globs: ["src/auth/**"]\n'
        "    success_criteria:\n"
        '      - "POST /token returns a JWT for valid credentials"\n'
        "    depends_on: []\n"
        "  - id: auth-tests\n"
        "    title: Add endpoint tests\n"
        "    type: test_change\n"
        '    scope_globs: ["tests/auth/**"]\n'
        "    success_criteria:\n"
        '      - "token route covered by integration tests"\n'
        "    depends_on: [auth-endpoint]\n"
    )
    if not with_sections:
        return block
    return (
        block
        + "---\n"
        + "## Task: auth-endpoint\n\n"
        + "POST /token returns a JWT for valid credentials.\n"
        + "Details for the endpoint task.\n\n"
        + "## auth-tests: Add endpoint tests\n\n"
        + "token route covered by integration tests.\n"
    )


@pytest.fixture
async def run(db):  # type: ignore[no-untyped-def]
    project = await repo.create_project(db, "girder", "/tmp/girder")
    return await repo.create_run(db, project.id, "ship auth", "feature/auth", 5.0)


async def test_happy_path(db, run) -> None:  # type: ignore[no-untyped-def]
    spec = parse_spec(_spec_text(_task_block(with_sections=True)))
    tasks = await decompose_spec(db, run, spec)

    assert len(tasks) == 2
    by_seq = {t.seq: t for t in tasks}
    assert by_seq[1].title == "Implement token endpoint"
    assert by_seq[1].task_type.value == "code_change"
    assert by_seq[1].scope_globs == ["src/auth/**"]
    assert by_seq[1].depends_on == []
    assert by_seq[2].task_type.value == "test_change"
    assert by_seq[2].scope_globs == ["tests/auth/**"]
    assert by_seq[2].depends_on == [by_seq[1].id]

    # persisted, in seq order, under the run's wave 0
    stored = await repo.list_tasks_for_run(db, run.id)
    assert [t.id for t in stored] == [t.id for t in tasks]
    assert [t.seq for t in stored] == [1, 2]
    waves = await db.fetchall("SELECT * FROM waves WHERE run_id = ?", (run.id,))
    assert len(waves) == 1
    assert waves[0]["sequence_order"] == 0

    for task, task_spec in zip(tasks, spec.tasks, strict=True):
        assert task.spec_slice_md
        for criterion in task_spec.success_criteria:
            assert criterion in task.spec_slice_md

    event = await repo.get_latest_event(db, run.id, "spec_decomposed")
    assert event is not None
    assert event["payload"]["task_count"] == 2
    assert event["payload"]["task_ids"] == ["auth-endpoint", "auth-tests"]


async def test_redecomposition_rejected(db, run) -> None:  # type: ignore[no-untyped-def]
    spec = parse_spec(_spec_text(_task_block(with_sections=False)))
    await decompose_spec(db, run, spec)
    with pytest.raises(DecompositionError, match="exactly once"):
        await decompose_spec(db, run, spec)


async def test_missing_scope_globs_rejected(db, run) -> None:  # type: ignore[no-untyped-def]
    spec = SpecDocument(
        title="t",
        intent="i",
        tasks=[
            TaskSpec(
                id="no-scope",
                title="Do something",
                type=TaskType.CODE_CHANGE,
                scope_globs=[],
                success_criteria=["works"],
            )
        ],
        body_md="",
        raw="",
    )
    with pytest.raises(DecompositionError, match="no-scope"):
        await decompose_spec(db, run, spec)


async def test_empty_task_list_rejected(db, run) -> None:  # type: ignore[no-untyped-def]
    spec = SpecDocument(title="t", intent="i", tasks=[], body_md="", raw="")
    with pytest.raises(DecompositionError, match="no tasks"):
        await decompose_spec(db, run, spec)


async def test_slice_extraction_scopes_to_own_section(db, run) -> None:  # type: ignore[no-untyped-def]
    spec = parse_spec(_spec_text(_task_block(with_sections=True)))
    tasks = await decompose_spec(db, run, spec)

    endpoint = tasks[0].spec_slice_md
    assert "Details for the endpoint task" in endpoint
    assert "integration tests" not in endpoint  # other task's section excluded
    assert not endpoint.lstrip().startswith("###")  # stops at next heading

    tests = tasks[1].spec_slice_md
    assert "integration tests" in tests
    assert "Details for the endpoint task" not in tests


async def test_synthesized_slice_fallback(db, run) -> None:  # type: ignore[no-untyped-def]
    spec = parse_spec(_spec_text(_task_block(with_sections=False)))
    tasks = await decompose_spec(db, run, spec)
    for task, task_spec in zip(tasks, spec.tasks, strict=True):
        assert task.spec_slice_md
        assert task_spec.id in task.spec_slice_md
        assert task_spec.type.value in task.spec_slice_md
        for glob in task_spec.scope_globs:
            assert glob in task.spec_slice_md


def _multi_task_spec_text(task_block: str) -> str:
    return (
        "---\n"
        "schema: girder.openspec/v1\n"
        "title: Chain\n"
        "intent: Dependency chain.\n"
        "tasks:\n"
        f"{task_block}"
        "---\n"
        "## Narrative\n\n"
        "Context.\n"
    )


async def test_depends_on_resolved_to_db_ids_diamond(db, run) -> None:  # type: ignore[no-untyped-def]
    spec = parse_spec(
        _multi_task_spec_text(
            "  - id: a\n"
            "    title: A\n"
            "    type: code_change\n"
            '    scope_globs: ["a/**"]\n'
            "    success_criteria: [works]\n"
            "    depends_on: []\n"
            "  - id: b\n"
            "    title: B\n"
            "    type: code_change\n"
            '    scope_globs: ["b/**"]\n'
            "    success_criteria: [works]\n"
            "    depends_on: [a]\n"
            "  - id: c\n"
            "    title: C\n"
            "    type: code_change\n"
            '    scope_globs: ["c/**"]\n'
            "    success_criteria: [works]\n"
            "    depends_on: [a, b]\n"
        )
    )
    tasks = await decompose_spec(db, run, spec)
    by_id = {t.title: t for t in tasks}
    assert by_id["C"].depends_on == [by_id["A"].id, by_id["B"].id]
    assert by_id["B"].depends_on == [by_id["A"].id]
    assert by_id["A"].depends_on == []

    # persisted, not just in-memory
    stored = {t.title: t for t in await repo.list_tasks_for_run(db, run.id)}
    assert stored["C"].depends_on == [stored["A"].id, stored["B"].id]

    event = await repo.get_latest_event(db, run.id, "spec_decomposed")
    assert event is not None
    assert event["payload"]["depends_on_edges"] == 3


async def test_forward_reference_resolves(db, run) -> None:  # type: ignore[no-untyped-def]
    spec = parse_spec(
        _multi_task_spec_text(
            "  - id: first\n"
            "    title: First\n"
            "    type: code_change\n"
            '    scope_globs: ["x/**"]\n'
            "    success_criteria: [works]\n"
            "    depends_on: [second]\n"
            "  - id: second\n"
            "    title: Second\n"
            "    type: code_change\n"
            '    scope_globs: ["y/**"]\n'
            "    success_criteria: [works]\n"
            "    depends_on: []\n"
        )
    )
    tasks = await decompose_spec(db, run, spec)
    by_title = {t.title: t for t in tasks}
    assert by_title["First"].depends_on == [by_title["Second"].id]
    assert by_title["Second"].depends_on == []


def test_template_task_sections_extractable() -> None:
    """The canonical template must not rot: its task extracts with criteria."""
    spec = parse_spec(OPENSPEC_TEMPLATE)
    slice_md = _extract_slice(spec.raw, spec.tasks[0])
    assert slice_md
    assert spec.tasks[0].id in slice_md
    for criterion in spec.tasks[0].success_criteria:
        assert criterion in slice_md
