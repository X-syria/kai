"""System-wide risk taxonomy for Tool execution.

``RiskLevel`` is the single vocabulary every Tool definition and every
boundary speaks. Ordering is explicit and deterministic: LOW < MEDIUM < HIGH <
CRITICAL. The MVP catalog ships only LOW and MEDIUM tools; HIGH/CRITICAL are
reserved for future slices and are refused before any execution can happen.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

_RANK: Final[dict[str, int]] = {
    "LOW": 0,
    "MEDIUM": 1,
    "HIGH": 2,
    "CRITICAL": 3,
}


class RiskLevel(StrEnum):
    """Risk associated with invoking a tool; deterministic total ordering."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"

    @property
    def rank(self) -> int:
        """A stable integer for ordering comparisons (never changes)."""
        return _RANK[self.value]

    def __lt__(self, other: object) -> bool:
        if isinstance(other, RiskLevel):
            return self.rank < other.rank
        return NotImplemented

    def __le__(self, other: object) -> bool:
        if isinstance(other, RiskLevel):
            return self.rank <= other.rank
        return NotImplemented

    def __gt__(self, other: object) -> bool:
        if isinstance(other, RiskLevel):
            return self.rank > other.rank
        return NotImplemented

    def __ge__(self, other: object) -> bool:
        if isinstance(other, RiskLevel):
            return self.rank >= other.rank
        return NotImplemented


__all__ = ["RiskLevel"]