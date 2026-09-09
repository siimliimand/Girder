"""A tiny calculator the Girder agent loop can genuinely extend.

``divide`` is deliberately ABSENT: the e2e scenarios ask the (scripted) agent
to implement it against this file.
"""

from __future__ import annotations


class Calculator:
    """Integer arithmetic on two operands."""

    def add(self, a: int, b: int) -> int:
        """Return the sum of ``a`` and ``b``."""
        return a + b

    def subtract(self, a: int, b: int) -> int:
        """Return ``a`` minus ``b``."""
        return a - b

    def multiply(self, a: int, b: int) -> int:
        """Return the product of ``a`` and ``b``."""
        return a * b
