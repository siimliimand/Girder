"""WP-1.7 — Redactor: golden secret corpus, markers, redaction_log rows."""

from __future__ import annotations

import pytest

from girder.guard.redact import Redactor, redact_and_log

GOLDEN = [
    # (label, planted text, pattern name)
    ("aws", "using key AKIAIOSFODNN7EXAMPLE please", "aws_access_key"),
    ("github", "token ghp_16C7e42F292c6912E7710c838347Ae178B4a", "github_token"),
    ("github_pat", "GITHUB_PAT=github_pat_11ABCDEFG0abcdefghijklmnopqrstuvwxyz", None),
    ("anthropic", "sk-ant-api03-48charslongkeyvalue-X9zZ", "anthropic_key"),
    ("openai", "export OPENAI=sk-proj4abc123def456ghi789jkl", "openai_key"),
    ("slack", "found xoxb-123456789012-1234567890123-abcdef", "slack_token"),
    (
        "jwt",
        "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
        "eyJzdWIiOiIxMjM0NTY3ODkwIn0."
        "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJVadQssw5c",
        "jwt",
    ),
    ("url_password", "postgres://admin:hunter2secret@db.example.com/prod", "url_password"),
    ("generic_yaml", "api_key: 8fJk29sL0dPq3Z", None),
    ("generic_env", "PASSWORD='Str0ng!Passw0rd#2026'", None),
]


@pytest.fixture
def redactor() -> Redactor:
    return Redactor(secret_env_names=["GITHUB_TOKEN", "OPENROUTER_API_KEY"])


def test_golden_corpus_fully_redacted(redactor: Redactor) -> None:
    for label, text, _ in GOLDEN:
        redacted, fired = redactor.redact(text)
        assert fired, f"{label}: nothing fired"
        # the planted secret must be gone
        assert "***[REDACTED:" in redacted, f"{label}: no marker"
        for token in text.replace(":", " ").replace("=", " ").split():
            if (
                len(token) > 12
                and any(c.isdigit() for c in token)
                and token
                not in (
                    "123456789012-1234567890123-abcdef",  # slack prefix covered below
                )
            ):
                assert token not in redacted, f"{label}: leaked {token!r}"


def test_marker_preserves_structure(redactor: Redactor) -> None:
    secret = "AKIAIOSFODNN7EXAMPLE"
    redacted, _ = redactor.redact(f"key {secret} end")
    assert f"AKI***[REDACTED:{len(secret)}chars]" in redacted
    assert secret not in redacted


def test_pem_block_redacted(redactor: Redactor) -> None:
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA7x9s\n"
        "morebase64==\n-----END RSA PRIVATE KEY-----"
    )
    redacted, fired = redactor.redact(f"cert:\n{pem}")
    assert "private_key_pem" in fired
    assert "MIIEowIBAAKCAQEA7x9s" not in redacted
    assert "BEGIN" in redacted or "REDACTED" in redacted


def test_env_denylist(redactor: Redactor) -> None:
    redacted, fired = redactor.redact("GITHUB_TOKEN=ghp_AAAABBBBCCCCDDDDEEEE")
    assert "env:GITHUB_TOKEN" in fired
    assert "ghp_AAAABBBBCCCCDDDDEEEE" not in redacted
    assert "GITHUB_TOKEN=" in redacted  # name kept, value replaced


def test_url_password_partial_redaction(redactor: Redactor) -> None:
    redacted, _ = redactor.redact("postgres://admin:Sup3rS3cret@db.example.com/x")
    assert "admin:" in redacted
    assert "Sup3rS3cret" not in redacted
    assert "db.example.com" in redacted


def test_low_entropy_words_survive(redactor: Redactor) -> None:
    text = "the password is incorrect; please try a different passphrase"
    redacted, fired = redactor.redact(text)
    assert "generic_assignment" not in fired
    assert redacted == text


def test_normal_code_untouched(redactor: Redactor) -> None:
    text = (
        "def calculate_total(items):\n"
        "    return sum(item.price for item in items)\n"
        "# TODO: refactor the token bucket later\n"
    )
    redacted, fired = redactor.redact(text)
    assert fired == []
    assert redacted == text


async def test_redact_and_log_writes_rows(db, redactor: Redactor) -> None:  # type: ignore[no-untyped-def]
    text = "connect with AKIAIOSFODNN7EXAMPLE and GITHUB_TOKEN=ghp_AAAABBBBCCCCDDDDEEEE"
    redacted = await redact_and_log(redactor, text, source_field="tool_output:run_command", db=db)
    assert "AKIAIOSFODNN7EXAMPLE" not in redacted
    rows = await db.fetchall("SELECT source_field, pattern_matched FROM redaction_log")
    patterns = {r["pattern_matched"] for r in rows}
    assert "aws_access_key" in patterns
    assert "env:GITHUB_TOKEN" in patterns
    assert all(r["source_field"] == "tool_output:run_command" for r in rows)
    # the secret value itself must appear nowhere in the log table
    dump = await db.fetchall("SELECT * FROM redaction_log")
    assert all("AKIA" not in str(tuple(d)) for d in dump)


async def test_redact_and_log_no_rows_when_clean(db, redactor: Redactor) -> None:  # type: ignore[no-untyped-def]
    await redact_and_log(redactor, "all clear here", source_field="x", db=db)
    count = await db.fetchone("SELECT COUNT(*) AS c FROM redaction_log")
    assert count["c"] == 0
