"""Context-window management: token estimation and compaction (impl-plan §8.5).

Compaction is *deterministic* — no extra model call. The system message and
the trusted task brief survive verbatim; older turns are elided (untrusted
bodies replaced by size markers) and their useful signal is distilled into a
scratchpad string the runtime re-injects as a trusted system message.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

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
    """
    del context_window  # the decision was made by should_compact; kept for symmetry
    if stats is None:
        stats = CompactionStats()
    stats.tokens_before = sum(estimate_tokens(m.content) for m in messages)
    stats.turns_dropped = max(len(messages) - 2, 0)
    kept_head = messages[:2]
    # The scratchpad is distilled from the ORIGINAL text: elision removes the
    # untrusted bodies before the error lines inside them would be scanned.
    scratchpad = _build_scratchpad("\n\n".join(m.content for m in messages[2:]))
    elided = [Message(m.role, _elide(m.content)) for m in messages[2:]]
    stats.scratchpad = scratchpad
    stats.tokens_after = sum(estimate_tokens(m.content) for m in kept_head + elided)
    return kept_head + elided, scratchpad
