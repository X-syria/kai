"""Pydantic wire schemas for the Memory API.

Memory is *context only*. No request carries ``tenant_id``/``user_id`` — they
come from the authenticated JWT via the current-user dependency, so there is no
field a caller could abuse to read or write another tenant's or user's memory.
Every numeric memory field is validated to ``[0, 1]`` here (and re-validated in
the service); out-of-range values are errors, never silent clamps.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator

from kai.memory.models import MemoryTTL, MemoryType, SourceKind

#: Upper bound on stored memory content (characters).
_CONTENT_LIMIT = 2000


def _check_unit(value: float, field: str) -> float:
    if value < 0.0 or value > 1.0:
        raise ValueError(f"{field} must be in [0, 1], got {value!r}")
    return value


def _clean_content(value: str) -> str:
    content = " ".join(value.split()).strip()
    if not content:
        raise ValueError("content must not be empty")
    if len(content) > _CONTENT_LIMIT:
        raise ValueError(f"content must be at most {_CONTENT_LIMIT} characters")
    return content


class MemoryCreateRequest(BaseModel):
    """Create one memory entry.

    ``confidence``/``importance``/``recurrence``/``future_relevance`` default
    deterministically (confidence from the source weight, the rest to neutral
    midpoints) when omitted; any explicit value outside ``[0, 1]`` is rejected.
    """

    model_config = ConfigDict(extra="forbid")

    type: MemoryType
    content: str
    source: SourceKind = SourceKind.EXPLICIT_USER_STATEMENT
    confidence: float | None = None
    importance: float | None = None
    recurrence: float | None = None
    future_relevance: float | None = None
    ttl: MemoryTTL = MemoryTTL.LONG_TTL

    @field_validator("content")
    @classmethod
    def _content(cls, value: str) -> str:
        return _clean_content(value)

    @field_validator("confidence", "importance", "recurrence", "future_relevance")
    @classmethod
    def _unit_ranges(cls, value: float | None) -> float | None:
        if value is None:
            return None
        return _check_unit(value, cls.__name__)


class MemoryUpdateRequest(BaseModel):
    """In-place edits to an ACTIVE memory (content/metadata/TTL).

    Corrections that must keep the old fact visible as superseded use the
    ``correct`` endpoint instead; ``memory_type`` and ``source`` are immutable
    here so a memory can never be silently re-categorized or re-sourced.
    """

    model_config = ConfigDict(extra="forbid")

    content: str | None = None
    importance: float | None = None
    recurrence: float | None = None
    future_relevance: float | None = None
    ttl: MemoryTTL | None = None

    @field_validator("content")
    @classmethod
    def _content(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _clean_content(value)

    @field_validator("importance", "recurrence", "future_relevance")
    @classmethod
    def _unit_ranges(cls, value: float | None) -> float | None:
        if value is None:
            return None
        return _check_unit(value, cls.__name__)


class MemoryCorrectRequest(BaseModel):
    """Supersede an ACTIVE memory with a corrected fact.

    ``source`` becomes ``USER_CORRECTION`` automatically; the old entry is
    marked SUPERSEDED and kept for provenance but never retrieved as current
    truth.
    """

    model_config = ConfigDict(extra="forbid")

    content: str
    importance: float | None = None

    @field_validator("content")
    @classmethod
    def _content(cls, value: str) -> str:
        return _clean_content(value)

    @field_validator("importance")
    @classmethod
    def _importance(cls, value: float | None) -> float | None:
        if value is None:
            return None
        return _check_unit(value, "importance")


class MemoryResponse(BaseModel):
    """A read-only representation of one memory entry.

    ``freshness`` is computed deterministically from ``updated_at`` and the
    server's ``now`` at read time (never stored, so it can never drift from the
    clock). ``status`` distinguishes ACTIVE / SUPERSEDED / DELETED explicitly so
    "not retrievable" and "deleted" are always distinguishable.
    """

    id: UUID
    tenant_id: UUID
    user_id: UUID
    type: str
    content: str
    source: str
    confidence: float
    importance: float
    freshness: float
    recurrence: float
    future_relevance: float
    status: str
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None
    supersedes_id: UUID | None
    superseded_by_id: UUID | None


class MemoryListResponse(BaseModel):
    memories: list[MemoryResponse]


class CategoryToggleRequest(BaseModel):
    """Enable/disable one memory category for retrieve."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool


class CategoryStatus(BaseModel):
    memory_type: str
    enabled: bool


class CategoryListResponse(BaseModel):
    categories: list[CategoryStatus]


__all__ = [
    "CategoryListResponse",
    "CategoryStatus",
    "CategoryToggleRequest",
    "MemoryCorrectRequest",
    "MemoryCreateRequest",
    "MemoryListResponse",
    "MemoryResponse",
    "MemoryUpdateRequest",
]