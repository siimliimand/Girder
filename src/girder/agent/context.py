"""Context-window management: token estimation and compaction (impl-plan §8.5).

Compaction is *deterministic* — no extra model call. The system message and
the trusted task brief survive verbatim; older turns are elided (untrusted
bodies replaced by size markers) and their useful signal is distilled into a
scratchpad string the runtime re-injects as a trusted system message.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from girder.models.gateway import Message

# chars/4 — the plan's agreed R8 heuristic (good enough for budgeting, free).
_CHARS_PER_TOKEN = 4

# Compact when the estimate crosses 70% of the role's context window.
_COMPACT_THRESHOLD = 0.70

_UNTRUSTED_BODY = re.compile(r"(<untrusted-data[^>]*>)([\s\S]*?)(</untrusted-data>)")
_WRITTEN_SOURCE = re.compile(r'<untrusted-data source="(?:write_file|apply_patch):([^"]+)"')
_COMMAND_SOURCE = re.compile(r'<untrusted-data source="run_command:([^"]+)"')
_ERROR_LINE = re.compile(r"^(?:.*\berror\b.*|.*\btimed out\.?.*|\[held.*)$", re.MULTILINE)


def estimate_tokens(text: str) -> int:
    """R8 heuristic: one token per four characters."""
    return len(text) // _CHARS_PER_TOKEN


@dataclass
class CompactionStats:
    """What the last compaction removed; purely informational."""

    turns_dropped: int = 0
    tokens_before: int = 0
    tokens_after: int = 0
    scratchpad: str = ""


_SCRATCHPAD_PREFIX = "[TRUSTED] Scratchpad"


@dataclass
class Scratchpad:
    """WP 8.2: structured, orchestrator-maintained scratchpad.

    Populated by the tool registry (files_read / files_written / test_results),
    the runtime (plan, milestones), and the retry loop (prior_attempt_summary).
    Survives compaction with zero distillation cost: only its JSON rendering is
    refreshed at context index 2.
    """

    plan: str = ""
    files_read: list[str] = field(default_factory=list)
    files_written: list[str] = field(default_factory=list)
    test_results: list[str] = field(default_factory=list)
    milestones: list[str] = field(default_factory=list)
    prior_attempt_summary: str | None = None

    def note_read(self, path: str) -> None:
        if path and path not in self.files_read:
            self.files_read.append(path)

    def note_write(self, path: str) -> None:
        if path and path not in self.files_written:
            self.files_written.append(path)

    def note_test(self, summary: str) -> None:
        if summary and summary not in self.test_results:
            self.test_results.append(summary)

    def to_json(self) -> str:
        """Compact JSON rendering (injected/refreshed verbatim on compaction)."""
        return json.dumps(
            {
                # B3 provenance: the plan is verbatim MODEL text stored by the
                # runtime — it rides inside a [TRUSTED] system message only for
                # compaction-survival, so its key carries the untrusted marker
                # (no sanitization machinery; labeling only).
                "plan (untrusted model output, verbatim)": self.plan,
                "files_read": self.files_read,
                "files_written": self.files_written,
                "test_results": self.test_results,
                "milestones": self.milestones,
                "prior_attempt_summary": self.prior_attempt_summary,
            },
            ensure_ascii=False,
        )


def scratchpad_message(scratchpad: Scratchpad) -> Message:
    """The trusted system message carrying the scratchpad JSON (index 2)."""
    return Message(
        role="system",
        content=f"{_SCRATCHPAD_PREFIX}:\n{scratchpad.to_json()}",
    )


def is_scratchpad_message(message: Message) -> bool:
    return message.role == "system" and message.content.startswith(_SCRATCHPAD_PREFIX)


def should_compact(messages: list[Message], context_window: int) -> bool:
    """True when the estimated conversation exceeds 70% of the window (§8.5)."""
    total = sum(estimate_tokens(m.content) for m in messages)
    return total > int(context_window * _COMPACT_THRESHOLD)


def _elide(text: str) -> str:
    """Replace untrusted bodies with size markers, keeping the framing tags."""

    def repl(match: re.Match[str]) -> str:
        marker = f"[untrusted-data elided: {len(match.group(2))} chars]"
        return f"{match.group(1)}\n{marker}\n{match.group(3)}"

    return _UNTRUSTED_BODY.sub(repl, text)


def _build_scratchpad(text: str) -> str:
    """Distill kept (elided) turns into a short trusted progress summary."""
    lines: list[str] = []
    written = _WRITTEN_SOURCE.findall(text)
    if written:
        lines.append("Files written/patched: " + ", ".join(dict.fromkeys(written)))
    commands = [cmd.strip() for cmd in _COMMAND_SOURCE.findall(text)]
    if commands:
        lines.append("Commands run: " + "; ".join(dict.fromkeys(c.split()[0] for c in commands)))
    errors = _ERROR_LINE.findall(text)
    if errors:
        lines.append("Errors seen:")
        lines.extend(f"  - {e.strip()[:120]}" for e in dict.fromkeys(errors))
    return "\n".join(lines)


def compact(
    messages: list[Message],
    *,
    context_window: int,
    stats: CompactionStats | None = None,
    structured: Scratchpad | None = None,
) -> tuple[list[Message], str]:
    """Deterministically compact a conversation (pure — the input is not mutated).

    Keeps ``messages[0]`` (system) and ``messages[1]`` (task brief) verbatim,
    elides ``<untrusted-data>`` bodies from the rest, and returns a scratchpad
    string summarizing files written, commands run, and errors seen. The
    caller re-injects the scratchpad as a system-role message placed AFTER the
    head pair.

    Invariant (§8.5): ``messages[:2]`` is always ``[system, task brief]``;
    scratchpads live in the tail and are re-distilled (collapsed into the new
    scratchpad) on every compaction.

    WP 8.2: when a structured :class:`Scratchpad` is supplied, it IS the
    scratchpad — no distillation pass runs over the conversation at all, and
    stale structured-scratchpad messages are dropped from the tail (the caller
    re-injects the fresh JSON at index 2). The free-text distillation remains
    only as the fallback for pre-8.2 / scratchpad-less conversations.
    """
    del context_window  # the decision was made by should_compact; kept for symmetry
    if stats is None:
        stats = CompactionStats()
    stats.tokens_before = sum(estimate_tokens(m.content) for m in messages)
    stats.turns_dropped = max(len(messages) - 2, 0)
    kept_head = messages[:2]
    if structured is not None:
        scratchpad = structured.to_json()
        elided = [
            Message(m.role, _elide(m.content))
            for m in messages[2:]
            if not is_scratchpad_message(m)
        ]
    else:
        # The scratchpad is distilled from the ORIGINAL text: elision removes the
        # untrusted bodies before the error lines inside them would be scanned.
        scratchpad = _build_scratchpad("\n\n".join(m.content for m in messages[2:]))
        elided = [Message(m.role, _elide(m.content)) for m in messages[2:]]
    stats.scratchpad = scratchpad
    stats.tokens_after = sum(estimate_tokens(m.content) for m in kept_head + elided)
    return kept_head + elided, scratchpad
