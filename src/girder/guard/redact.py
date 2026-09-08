"""Secret redaction pipeline (plan.md §8.6 / impl-plan §6.6).

Sits between every raw tool/shell/log output and (a) SQLite persistence and
(b) the notification engine — nothing reaches either unredacted.

* Pattern set for common credential shapes (AWS keys, GitHub/OpenAI/Anthropic/
  Slack tokens, JWTs, PEM blocks, URL-embedded passwords, generic
  ``api_key = …`` assignments with an entropy floor).
* Cross-check against a denylist of known secret *environment variable names*.
* Matches are replaced with structure-preserving markers
  (``sk-***[REDACTED:20chars]``) — enough shape left to debug, no secret left.
* Every substitution can be recorded to ``redaction_log`` (pattern name +
  source field + timestamp); the secret value itself is never stored.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from girder.db import repo
from girder.db.engine import Database


def _marker(matched: str) -> str:
    """``sk-abc123…`` -> ``sk-***[REDACTED:20chars]`` — keep a short prefix."""
    keep = matched[:3] if len(matched) >= 8 else ""
    return f"{keep}***[REDACTED:{len(matched)}chars]"


def _char_class_diversity(s: str) -> int:
    classes = 0
    if re.search(r"[a-z]", s):
        classes += 1
    if re.search(r"[A-Z]", s):
        classes += 1
    if re.search(r"[0-9]", s):
        classes += 1
    if re.search(r"[^a-zA-Z0-9]", s):
        classes += 1
    return classes


_GENERIC_KEY_RE = re.compile(
    r"(?i)\b(api[_-]?key|apikey|secret|token|password|passwd|pwd|bearer)\b"
    r"(\s*[=:]\s*)"
    r"[\"']?([A-Za-z0-9_\-+/=.:!@#$%^&*(),;~]{8,})[\"']?"
)

_REDACTED_MARKER = "***[REDACTED:"

# Order matters: specific shapes first, generic assignment last.
_BASE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("github_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b")),
    ("openai_key", re.compile(r"\bsk-(?!ant-)[A-Za-z0-9_\-]{20,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{5,}\b")),
    (
        "private_key_pem",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    ),
    ("url_password", re.compile(r"\b([a-z][a-z0-9+.\-]*://)([^/\s:@']+):([^@\s']+)@")),
)


class Redactor:
    """Redact credential-shaped substrings from arbitrary text.

    Stateless and deterministic; construct one per process with the
    deployment's env-var denylist.
    """

    def __init__(self, secret_env_names: list[str] | None = None) -> None:
        self.patterns: list[tuple[str, re.Pattern[str]]] = list(_BASE_PATTERNS)
        for name in secret_env_names or []:
            self.patterns.append(
                (f"env:{name}", re.compile(rf"\b{re.escape(name)}\b(\s*[=:]\s*)(\S+)"))
            )

    def redact(self, text: str) -> tuple[str, list[str]]:
        """Return ``(redacted_text, fired_patterns)``. Pure — no I/O."""
        fired: set[str] = set()

        def make_repl(name: str) -> Callable[[re.Match[str]], str]:
            def repl(m: re.Match[str]) -> str:
                if _REDACTED_MARKER in m.group(0):
                    # Already redacted by an earlier pattern: keep the text as
                    # is, but still record that this pattern matched for audit.
                    fired.add(name)
                    return m.group(0)
                fired.add(name)
                if name == "url_password":
                    return f"{m.group(1)}{m.group(2)}:{_marker(m.group(3))}@"
                if name.startswith("env:"):
                    var = name.removeprefix("env:")
                    return f"{var}{m.group(1)}{_marker(m.group(2))}"
                return _marker(m.group(0))

            return repl

        for name, pattern in self.patterns:
            text = pattern.sub(make_repl(name), text)

        def generic_repl(m: re.Match[str]) -> str:
            value = m.group(3)
            if _REDACTED_MARKER in m.group(0):
                return m.group(0)
            if _char_class_diversity(value) < 3:
                return m.group(0)  # low entropy: prose, not a secret
            fired.add("generic_assignment")
            return f"{m.group(1)}{m.group(2)}{_marker(value)}"

        return _GENERIC_KEY_RE.sub(generic_repl, text), sorted(fired)


async def redact_and_log(
    redactor: Redactor,
    text: str,
    *,
    source_field: str,
    db: Database,
    attempt_id: str | None = None,
) -> str:
    """Redact and persist one ``redaction_log`` row per fired pattern.

    The redacted text is computed *before* any DB write, and a DB failure
    propagates rather than ever returning the raw text.
    """
    redacted, fired = redactor.redact(text)
    for name in fired:
        await repo.insert_redaction(db, source_field, name, attempt_id=attempt_id)
    return redacted
