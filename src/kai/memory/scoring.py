"""Deterministic, pure scoring for memory retrieval.

No embeddings, no vectors, no semantic search: every number is a pure function
of stored fields and an injectable ``now``, so the exact same inputs always
produce the exact same scores.

* :func:`freshness` decays exponentially from ``updated_at`` with a settings
  half-life; ``now`` is passed in so tests are fully deterministic and never
  read the wall clock.
* :func:`retrieval_score` is the fixed weighted sum from the Memory System
  contract: ``0.30*Importance + 0.25*Confidence + 0.20*FutureRelevance +
  0.15*Recurrence + 0.10*Freshness``.
* Both clamp nothing: inputs outside ``[0, 1]`` raise ``ValueError``.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from kai.config import get_settings
from kai.memory.models import MemoryEntry

#: Retrieval formula weights (fixed contract, sum == 1.0).
WEIGHT_IMPORTANCE: float = 0.30
WEIGHT_CONFIDENCE: float = 0.25
WEIGHT_FUTURE_RELEVANCE: float = 0.20
WEIGHT_RECURRENCE: float = 0.15
WEIGHT_FRESHNESS: float = 0.10


def _unit(value: float, name: str) -> float:
    if value < 0.0 or value > 1.0:
        raise ValueError(f"{name} must be in [0, 1], got {value!r}")
    return float(value)


def freshness(updated_at: datetime, now: datetime) -> float:
    """Exponential decay of a memory's freshness from its last update.

    ``now`` is injected (never read from the wall clock) so the value is fully
    deterministic for tests. A memory updated exactly one half-life ago scores
    ``0.5**1 == 0.5``; the result is always in ``(0, 1]``.
    """
    if now < updated_at:
        return 1.0
    age = now - updated_at
    half_life = timedelta(days=get_settings().memory_freshness_half_life_days)
    exponent = max(0.0, age.total_seconds() / half_life.total_seconds())
    return 0.5 ** exponent


def retrieval_score(
    *,
    importance: float,
    confidence: float,
    future_relevance: float,
    recurrence: float,
    freshness: float,
) -> float:
    """The exact retrieval score: the fixed weighted sum, all inputs in [0,1]."""
    return (
        WEIGHT_IMPORTANCE * _unit(importance, "importance")
        + WEIGHT_CONFIDENCE * _unit(confidence, "confidence")
        + WEIGHT_FUTURE_RELEVANCE * _unit(future_relevance, "future_relevance")
        + WEIGHT_RECURRENCE * _unit(recurrence, "recurrence")
        + WEIGHT_FRESHNESS * _unit(freshness, "freshness")
    )


def score_memory(entry: MemoryEntry, now: datetime) -> float:
    """Score one stored memory against an injected ``now``."""
    return retrieval_score(
        importance=entry.importance,
        confidence=entry.confidence,
        future_relevance=entry.future_relevance,
        recurrence=entry.recurrence,
        freshness=freshness(entry.updated_at, now),
    )


__all__ = ["freshness", "retrieval_score", "score_memory"]