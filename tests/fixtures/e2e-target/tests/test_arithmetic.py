"""Baseline suite for the e2e fixture repo (all green by construction)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from calculator import Calculator


def test_add() -> None:
    assert Calculator().add(2, 3) == 5


def test_subtract() -> None:
    assert Calculator().subtract(7, 4) == 3


def test_multiply() -> None:
    assert Calculator().multiply(6, 7) == 42
