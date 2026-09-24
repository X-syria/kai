"""Memory ORM models and the deterministic vocabulary around them.

Memory is *context only*: it is never a substitute for Policy, permissions,
approvals, the Task Contract, or the Identity Resolver. ``source`` records
provenance and a trust *weight* for retrieval ranking; it never grants access
and never becomes trusted instructions.

Tenancy is a hard FK into ``tenants.id`` and every memory is owned by exactly
one ``user_id``; every query in :mod:`kai.memory.service` filters on both so no
row can be read, corrected, or removed through another tenant's or user's
context.

Fields are kept inside ``[0, 1]`` by explicit validation (DB CHECK constraints
are defense-in-depth); the codebase never clamps silently — an out-of-range
value is a validation error, not a silent correction.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Final
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from kai.db.base import Base, PKMixin, TenantMixin, TimestampMixin


class MemoryType(StrEnum):
    """The only memory categories that exist. No others are accepted."""

    IDENTITY = "IDENTITY"
    PREFERENCE = "PREFERENCE"
    PERSON = "PERSON"
    PROJECT = "PROJECT"
    RULE = "RULE"
    EPISODIC_EVENT = "EPISODIC_EVENT"
    SEMANTIC_KNOWLEDGE = "SEMANTIC_KNOWLEDGE"


class SourceKind(StrEnum):
    """Provenance of a memory entry.

    A source is a *provenance label* with a fixed trust weight used purely for
    retrieval ranking. It is never trusted-instruction content: memory can never
    change policy, permissions, approvals, or identity verification.
    """

    EXPLICIT_USER_STATEMENT = "EXPLICIT_USER_STATEMENT"
    USER_CORRECTION = "USER_CORRECTION"
    REPEATED_BEHAVIOR = "REPEATED_BEHAVIOR"
    IMPORTED_STRUCTURED_DATA = "IMPORTED_STRUCTURED_DATA"
    AGENT_INFERENCE = "AGENT_INFERENCE"
    EXTERNAL_INFERENCE = "EXTERNAL_INFERENCE"


#: Trust weight per source, used as the default ``confidence`` and as provenance
#: metadata. Fixed values per the Memory System contract.
SOURCE_WEIGHTS: Final[dict[SourceKind, float]] = {
    SourceKind.EXPLICIT_USER_STATEMENT: 1.00,
    SourceKind.USER_CORRECTION: 1.00,
    SourceKind.REPEATED_BEHAVIOR: 0.85,
    SourceKind.IMPORTED_STRUCTURED_DATA: 0.80,
    SourceKind.AGENT_INFERENCE: 0.50,
    SourceKind.EXTERNAL_INFERENCE: 0.30,
}


class MemoryTTL(StrEnum):
    """Expiry classes a memory may be created under.

    ``PERMANENT`` never expires (``expires_at`` is ``NULL``); the other classes
    map to concrete lifetimes from settings. Expiration is *not* deletion: an
    expired entry stops being retrievable but its row still exists and stays
    viewable by direct ``get``.
    """

    PERMANENT = "PERMANENT"
    LONG_TTL = "LONG_TTL"
    SHORT_TTL = "SHORT_TTL"
    EPHEMERAL = "EPHEMERAL"


class MemoryStatus(StrEnum):
    """Lifecycle of one memory entry.

    ``ACTIVE`` is current truth. ``SUPERSEDED`` is the *old* fact after a
    correction (kept for provenance, never retrieved as current truth).
    ``DELETED`` is a user-delete tombstone (also never retrieved).
    """

    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    DELETED = "DELETED"


def _range_constraint(column: str) -> CheckConstraint:
    return CheckConstraint(
        f"{column} >= 0 AND {column} <= 1",
        name=f"ck_memory_entries_{column}_range",
    )


class MemoryEntry(PKMixin, TenantMixin, TimestampMixin, Base):
    """A single tenant+user-scoped memory fact."""

    __tablename__ = "memory_entries"
    __table_args__ = (
        _range_constraint("confidence"),
        _range_constraint("importance"),
        _range_constraint("recurrence"),
        _range_constraint("future_relevance"),
        {"comment": "tenant+user-scoped deterministic memory"},
    )

    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    memory_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    importance: Mapped[float] = mapped_column(Float, nullable=False)
    recurrence: Mapped[float] = mapped_column(Float, nullable=False)
    future_relevance: Mapped[float] = mapped_column(Float, nullable=False)
    ttl: Mapped[str] = mapped_column(String(16), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=MemoryStatus.ACTIVE.value, index=True
    )
    # Provenance chain: the ACTIVE correction points back at the fact it
    # superseded; the superseded row points forward at its replacement.
    supersedes_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("memory_entries.id", ondelete="SET NULL"),
        nullable=True,
    )
    superseded_by_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("memory_entries.id", ondelete="SET NULL"),
        nullable=True,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def is_active(self) -> bool:
        return self.status == MemoryStatus.ACTIVE.value

    @property
    def is_superseded(self) -> bool:
        return self.status == MemoryStatus.SUPERSEDED.value

    @property
    def is_deleted(self) -> bool:
        return self.status == MemoryStatus.DELETED.value


class MemoryPreference(PKMixin, TenantMixin, TimestampMixin, Base):
    """Per-user memory category preferences.

    ``disabled_types`` is the set of ``MemoryType`` values that must **not** be
    returned by retrieve for this user. Disabling a category only suppresses
    retrieval — the rows themselves are untouched and remain viewable (they are
    not a deletion).
    """

    __tablename__ = "memory_preferences"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "user_id", name="uq_memory_preferences_tenant_user"
        ),
        {"comment": "per-user memory category preferences (disabled retrieval set)"},
    )

    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    disabled_types: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)


__all__ = [
    "MemoryEntry",
    "MemoryPreference",
    "MemoryStatus",
    "MemoryTTL",
    "MemoryType",
    "SOURCE_WEIGHTS",
    "SourceKind",
]