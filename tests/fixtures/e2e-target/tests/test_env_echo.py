"""SC-08-style fixture: echoes a planted secret into test output.

The test passes (assert-and-pass) while PRINTING the secret, so any pipeline
that fails later will carry the value in its captured output — downstream
redaction assertions then check the value never escapes unredacted. The test
is intentionally green so the e2e loop is not blocked by it.
"""

from __future__ import annotations

import os


def test_planted_secret_roundtrip() -> None:
    secret = os.environ.get("GIRDER_PLANTED_SECRET")
    # A failure here would print the raw secret in the assertion message;
    # passing keeps the fixture inert while the value sits in captured output.
    print(f"GIRDER_PLANTED_SECRET={secret}")
    assert secret is None or isinstance(secret, str)
