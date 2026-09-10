"""Unit tests for context compaction (§8.5) and §7 prompt framing."""

from __future__ import annotations

import copy

from girder.agent.context import (
    CompactionStats,
    compact,
    estimate_tokens,
    should_compact,
)
from girder.agent.prompts import build_system_prompt, build_task_message, wrap_tool_result
from girder.agent.runtime import _milestone_for_turn
from girder.db.models import Task, TaskStatus, TaskType
from girder.models.gateway import Message


def _task() -> Task:
    return Task(
        id="t1",
        wave_id="w0",
        seq=1,
        title="add parser",
        task_type=TaskType.CODE_CHANGE,
        status=TaskStatus.PENDING,
        scope_globs=["src/**"],
    )


# ----------------------------------------------------------------- estimation


def test_estimate_tokens_is_chars_over_four() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("a" * 401) == 100


def test_should_compact_at_70pct() -> None:
    window = 1000
    under = [Message(role="system", content="a" * (4 * 700))]  # exactly 700 tokens
    assert not should_compact(under, window)
    over = [Message(role="system", content="a" * (4 * 701))]  # 701 tokens
    assert should_compact(over, window)


# ------------------------------------------------------------------ compaction


def _conversation() -> list[Message]:
    return [
        Message(role="system", content="SYSTEM INVARIANTS"),
        Message(role="user", content="TASK BRIEF"),
        Message(
            role="assistant",
            content='[tool calls this turn: write_file({"path": "src/a.py"})]',
        ),
        Message(
            role="user",
            content=(
                '<untrusted-data source="write_file:src/a.py">\nwrote ok\n'
                "</untrusted-data>\n\n"
                '<untrusted-data source="read_file:src/big.py">\n'
                + ("X" * 500)
                + "</untrusted-data>"
            ),
        ),
        Message(role="assistant", content="[tool calls this turn: run_command({...})]"),
        Message(
            role="user",
            content=(
                '<untrusted-data source="run_command:python -m pytest -q">\n'
                "error: 3 failed\n</untrusted-data>"
            ),
        ),
    ]


def test_compact_keeps_system_and_brief_elides_bodies() -> None:
    msgs = _conversation()
    original = copy.deepcopy(msgs)
    out, _scratchpad = compact(msgs, context_window=1000)
    # purity: the input list is unmutated
    assert [m.content for m in msgs] == [m.content for m in original]
    assert out[0].content == "SYSTEM INVARIANTS"
    assert out[1].content == "TASK BRIEF"
    joined = "\n".join(m.content for m in out[2:])
    assert "X" * 500 not in joined
    assert "[untrusted-data elided: 501 chars]" in joined  # body + newline
    assert '<untrusted-data source="read_file:src/big.py">' in joined  # framing kept


def test_compact_scratchpad_mentions_writes_commands_errors() -> None:
    _, scratchpad = compact(_conversation(), context_window=1000)
    assert "src/a.py" in scratchpad  # files written/patched
    assert "python" in scratchpad  # commands run (tool + first arg)
    assert "error: 3 failed" in scratchpad  # errors seen


def test_compact_stats_populated() -> None:
    msgs = _conversation()
    stats = CompactionStats()
    compact(msgs, context_window=1000, stats=stats)
    assert stats.turns_dropped == 4
    assert stats.tokens_before > stats.tokens_after
    assert "elided" in stats.scratchpad or stats.scratchpad


# -------------------------------------------------------------- §7 prompts


def test_system_prompt_carries_invariants() -> None:
    prompt = build_system_prompt(task=_task(), spec_slice="slice")
    assert "UNTRUSTED CONTENT RULE" in prompt
    assert "Test files are read-only" in prompt
    assert "request_spec_amendment" in prompt


def test_system_prompt_carries_orientation() -> None:
    """The agent must know where it is: without this block a model that never
    saw the container layout burns held calls probing for the host repo
    (dogfood run 7725f897, 7 scope violations in one attempt)."""
    prompt = build_system_prompt(task=_task(), spec_slice="slice")
    assert "ORIENTATION" in prompt
    assert "/workspace" in prompt
    assert "host filesystem does not exist" in prompt
    assert "path escape" in prompt
    assert ".git is protected" in prompt


def test_system_prompt_states_turn_budget_when_given() -> None:
    prompt = build_system_prompt(task=_task(), spec_slice="slice", turn_budget=60)
    assert "WORKING METHOD" in prompt
    assert "turn budget for this attempt is 60" in prompt
    assert "turn 30" in prompt  # half-budget writing deadline
    assert "mark_task_complete MUST end your attempt" in prompt


def test_system_prompt_without_budget_has_no_method_block() -> None:
    prompt = build_system_prompt(task=_task(), spec_slice="slice")
    assert "WORKING METHOD" not in prompt
    assert "turn budget" not in prompt


def test_system_prompt_lists_restricted_paths() -> None:
    prompt = build_system_prompt(
        task=_task(), spec_slice="s", turn_budget=40, read_restricted=(".git/**", ".env*")
    )
    assert ".git/**" in prompt
    assert "read-forbidden" in prompt


def test_milestone_fires_at_half_and_cap_minus_two_only() -> None:
    cap = 60
    fired = {t for t in range(1, cap + 1) if _milestone_for_turn(t, cap) is not None}
    assert fired == {30, 58}
    assert _milestone_for_turn(30, cap) == "Half your turns are spent — start writing now"
    assert "mark_task_complete" in (_milestone_for_turn(58, cap) or "")


def test_milestone_never_fires_twice_for_a_turn_number() -> None:
    # Milestones are pure functions of (turn, cap): each turn maps to at most
    # one message, so driving the loop twice yields identical firings.
    first = [_milestone_for_turn(t, 20) for t in range(1, 21)]
    second = [_milestone_for_turn(t, 20) for t in range(1, 21)]
    assert first == second
    assert [m for m in first if m is not None] == [
        "Half your turns are spent — start writing now",
        "Final turns: conclude and call `mark_task_complete`.",
    ]


def test_task_message_lists_read_restricted_globs() -> None:
    msg = build_task_message(
        task=_task(), spec_slice="# frozen spec", read_restricted=(".git/**", "openspec/**")
    )
    assert "Read-restricted: .git/**, openspec/**" in msg


def test_task_message_omits_read_restricted_when_empty() -> None:
    msg = build_task_message(task=_task(), spec_slice="# frozen spec")
    assert "Read-restricted" not in msg


def test_task_message_trust_labels_and_guidance() -> None:
    msg = build_task_message(task=_task(), spec_slice="# frozen spec", guidance="focus on foo")
    assert "[TRUSTED] Frozen specification slice:" in msg
    assert "# frozen spec" in msg
    assert "src/**" in msg
    assert "[TRUSTED] Steering directive (user-authored):" in msg
    assert "focus on foo" in msg


def test_wrap_tool_result_shape() -> None:
    wrapped = wrap_tool_result("read_file:src/a.py", "hello")
    assert wrapped == '<untrusted-data source="read_file:src/a.py">\nhello\n</untrusted-data>'


def test_generator_prompt_carries_task_sizing_clause() -> None:
    from girder.specs.generator import _SYSTEM_TEMPLATE

    assert "within the orchestrator's turn budget" in _SYSTEM_TEMPLATE
    assert "one task per section" in _SYSTEM_TEMPLATE


# ------------------------------------------------- WP 8.2 structured scratchpad


def test_scratchpad_dedupes_preserving_order() -> None:
    from girder.agent.context import Scratchpad

    pad = Scratchpad()
    pad.note_read("b.py")
    pad.note_read("a.py")
    pad.note_read("b.py")
    pad.note_read("")  # empty paths are dropped
    pad.note_write("src/x.py")
    pad.note_write("src/x.py")
    pad.note_test("PASSED: 1  FAILED: 0  ERROR: 0")
    pad.note_test("PASSED: 0  FAILED: 1  ERROR: 0")
    assert pad.files_read == ["b.py", "a.py"]
    assert pad.files_written == ["src/x.py"]
    assert len(pad.test_results) == 2
    assert '"files_read": ["b.py", "a.py"]' in pad.to_json()


def test_scratchpad_message_shape_and_detection() -> None:
    from girder.agent.context import Scratchpad, is_scratchpad_message, scratchpad_message

    pad = Scratchpad(plan="PLAN: x", prior_attempt_summary="brief")
    msg = scratchpad_message(pad)
    assert msg.role == "system"
    assert msg.content.startswith("[TRUSTED] Scratchpad:")
    assert '"prior_attempt_summary": "brief"' in msg.content
    assert is_scratchpad_message(msg)
    assert not is_scratchpad_message(Message(role="user", content=msg.content))
    assert not is_scratchpad_message(Message(role="system", content="[TRUSTED] other"))


def test_compact_structured_skips_distillation_and_stale_scratchpads() -> None:
    """WP 8.2: with a structured scratchpad the returned scratchpad IS the
    structured JSON (no free-text distillation) and stale structured-scratchpad
    messages are dropped from the tail."""
    from girder.agent.context import Scratchpad, scratchpad_message

    pad = Scratchpad(plan="PLAN: x")
    pad.note_write("src/new.py")
    msgs = [
        Message(role="system", content="sys"),
        Message(role="user", content="brief"),
        scratchpad_message(Scratchpad(plan="stale")),
        Message(
            role="user",
            content='<untrusted-data source="write_file:src/old.py">\nsecret\n</untrusted-data>',
        ),
    ]
    out, scratchpad = compact(msgs, context_window=10_000, structured=pad)
    assert "src/new.py" in scratchpad and "old.py" not in scratchpad  # structured, not distilled
    assert sum(1 for m in out if "Scratchpad" in m.content) == 0  # stale block dropped
    assert out[0].content == "sys" and out[1].content == "brief"
    assert "elided" in out[-1].content  # untrusted bodies still elided


# ----------------------------------------------------- WP 8.1 prompt additions


def test_system_prompt_announces_planning_phase() -> None:
    text = build_system_prompt(
        task=_task(), spec_slice="s", turn_budget=20, planning_turns=5
    )
    assert "REQUIRED PLANNING PHASE (turns 1-5)" in text
    assert "Do NOT call write_file, edit_file, or apply_patch before" in text


def test_system_prompt_omits_planning_phase_when_disabled() -> None:
    text = build_system_prompt(
        task=_task(), spec_slice="s", turn_budget=20, planning_turns=0
    )
    assert "PLANNING PHASE" not in text
