"""The Memory Service: tenant+user-scoped CRUD, correction, retrieval.

Deterministic, PostgreSQL-only, fully auditable. Every operation is scoped by
the trusted ``(tenant_id, user_id)`` — never by client-supplied values — so no
row can be read, corrected, or removed through a foreign tenant/user context.

Architectural boundary: ``Agent Runtime -> Memory Service -> PostgreSQL``. This
module never touches Task Manager, Planner, Policy, Approvals, or the Identity
Resolver, and no other module reads memory storage directly. Memory is context
only: it changes nothing about policy, permissions, approvals, the Task
Contract, or identity verification.

Lifecycle semantics:

* ``ACTIVE`` — current truth, retrievable until ``expires_at`` (PERMANENT
  never expires; expiration sets the row not-retrievable but does not delete
  it).
* ``SUPERSEDED`` — a corrected fact's old row; kept for provenance and never
  retrieved as current truth.
* ``DELETED`` — a user delete tombstone; invisible to every read, kept only for
  audit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from kai.config import get_settings
from kai.memory.errors import (
    InvalidMemoryTransition,
    MemoryNotFoundError,
    MemoryValidationError,
)
from kai.memory.models import (
    SOURCE_WEIGHTS,
    MemoryEntry,
    MemoryPreference,
    MemoryStatus,
    MemoryTTL,
    MemoryType,
    SourceKind,
)
from kai.memory.scoring import score_memory

#: Defaults for the neutral scoring fields when the caller omits them.
_DEFAULT_IMPORTANCE = 0.5
_DEFAULT_RECURRENCE = 0.0
_DEFAULT_FUTURE_RELEVANCE = 0.5
_CONTENT_LIMIT = 2000
_MAX_RETRIEVAL = 100
_MAX_SEARCH = 100


@dataclass(frozen=True)
class ScoredMemory:
    """One retrieved memory entry with its deterministic score."""

    entry: MemoryEntry
    score: float


# --------------------------------------------------------------------------- #
# Create / read / update / delete
# --------------------------------------------------------------------------- #


async def create_memory(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    user_id: UUID,
    memory_type: MemoryType,
    content: str,
    source: SourceKind = SourceKind.EXPLICIT_USER_STATEMENT,
    confidence: float | None = None,
    importance: float | None = None,
    recurrence: float | None = None,
    future_relevance: float | None = None,
    ttl: MemoryTTL = MemoryTTL.LONG_TTL,
    now: datetime | None = None,
) -> MemoryEntry:
    """Create one ACTIVE memory entry owned by ``(tenant, user)``."""
    current = _as_utc(now)
    cleaned = _clean_content(content)
    _check_type(memory_type)
    _check_source(source)

    resolved_confidence = (
        confidence if confidence is not None else SOURCE_WEIGHTS[source]
    )
    entry = MemoryEntry(
        tenant_id=tenant_id,
        user_id=user_id,
        memory_type=memory_type.value,
        content=cleaned,
        source=source.value,
        confidence=_validate_unit(resolved_confidence, "confidence"),
        importance=_validate_unit(
            importance if importance is not None else _DEFAULT_IMPORTANCE,
            "importance",
        ),
        recurrence=_validate_unit(
            recurrence if recurrence is not None else _DEFAULT_RECURRENCE,
            "recurrence",
        ),
        future_relevance=_validate_unit(
            future_relevance
            if future_relevance is not None
            else _DEFAULT_FUTURE_RELEVANCE,
            "future_relevance",
        ),
        ttl=ttl.value,
        expires_at=_expiry_for(ttl, current),
        status=MemoryStatus.ACTIVE.value,
    )
    session.add(entry)
    await session.commit()
    await session.refresh(entry)
    return entry


async def get_memory(
    session: AsyncSession,
    *,
    memory_id: UUID,
    tenant_id: UUID,
    user_id: UUID,
) -> MemoryEntry:
    """Fetch one memory by id, strictly scoped.

    A DELETED row resolves the same as one that never existed (the user deleted
    it); SUPERSEDED and expired rows stay viewable through ``get`` so nothing
    is ever silently hidden except a deletion.
    """
    entry = await _load_entry(session, memory_id, tenant_id, user_id)
    if entry.is_deleted:
        raise MemoryNotFoundError("memory not found for tenant")
    return entry


async def update_memory(
    session: AsyncSession,
    *,
    memory_id: UUID,
    tenant_id: UUID,
    user_id: UUID,
    content: str | None = None,
    importance: float | None = None,
    recurrence: float | None = None,
    future_relevance: float | None = None,
    ttl: MemoryTTL | None = None,
    now: datetime | None = None,
) -> MemoryEntry:
    """In-place edit of an ACTIVE memory (content/metadata/TTL)."""
    current = _as_utc(now)
    entry = await _load_entry(session, memory_id, tenant_id, user_id)
    if entry.is_deleted:
        raise MemoryNotFoundError("memory not found for tenant")
    if entry.is_superseded:
        raise InvalidMemoryTransition(
            "a superseded memory cannot be edited; correct its replacement instead"
        )

    if content is not None:
        entry.content = _clean_content(content)
    if importance is not None:
        entry.importance = _validate_unit(importance, "importance")
    if recurrence is not None:
        entry.recurrence = _validate_unit(recurrence, "recurrence")
    if future_relevance is not None:
        entry.future_relevance = _validate_unit(
            future_relevance, "future_relevance"
        )
    if ttl is not None:
        entry.ttl = ttl.value
        entry.expires_at = _expiry_for(ttl, current)

    await session.commit()
    await session.refresh(entry)
    return entry


async def correct_memory(
    session: AsyncSession,
    *,
    memory_id: UUID,
    tenant_id: UUID,
    user_id: UUID,
    content: str,
    importance: float | None = None,
    now: datetime | None = None,
) -> tuple[MemoryEntry, MemoryEntry]:
    """Supersede an ACTIVE fact with a corrected one, keeping provenance.

    Returns ``(corrected, superseded)``. The new entry carries
    ``source=USER_CORRECTION``, keeps the memory's category and scoring
    metadata, and points ``supersedes_id`` at the old entry; the old entry is
    marked SUPERSEDED and, from that moment, is never retrieved as current
    truth. Both rows live on for audit.
    """
    current = _as_utc(now)
    cleaned = _clean_content(content)
    target = await _load_entry(session, memory_id, tenant_id, user_id)
    if target.is_deleted:
        raise MemoryNotFoundError("memory not found for tenant")
    if target.is_superseded:
        raise InvalidMemoryTransition(
            "a superseded memory cannot be corrected again; correct the current fact"
        )

    corrected = MemoryEntry(
        tenant_id=tenant_id,
        user_id=user_id,
        memory_type=target.memory_type,
        content=cleaned,
        source=SourceKind.USER_CORRECTION.value,
        confidence=SOURCE_WEIGHTS[SourceKind.USER_CORRECTION],
        importance=_validate_unit(
            importance if importance is not None else target.importance,
            "importance",
        ),
        recurrence=_validate_unit(target.recurrence, "recurrence"),
        future_relevance=_validate_unit(
            target.future_relevance, "future_relevance"
        ),
        ttl=target.ttl,
        expires_at=_expiry_for(
            MemoryTTL[target.ttl], current
        ),
        status=MemoryStatus.ACTIVE.value,
        supersedes_id=target.id,
    )
    session.add(corrected)
    # Flush assigns ``corrected.id`` (application-side default) so the old row
    # can point forward at its replacement before the final commit.
    await session.flush()
    target.status = MemoryStatus.SUPERSEDED.value
    target.superseded_by_id = corrected.id
    await session.commit()
    await session.refresh(corrected)
    await session.refresh(target)
    return corrected, target


async def delete_memory(
    session: AsyncSession,
    *,
    memory_id: UUID,
    tenant_id: UUID,
    user_id: UUID,
    now: datetime | None = None,
) -> MemoryEntry:
    """Logically delete a memory (tombstone; not physical).

    A DELETED row is invisible to every read (authorization is checked before
    the tombstone is applied). The row stays in the table for audit/provenance.
    """
    current = _as_utc(now)
    entry = await _load_entry(session, memory_id, tenant_id, user_id)
    if entry.is_deleted:
        raise MemoryNotFoundError("memory not found for tenant")
    entry.status = MemoryStatus.DELETED.value
    entry.deleted_at = current
    await session.commit()
    await session.refresh(entry)
    return entry


# --------------------------------------------------------------------------- #
# Search / retrieve
# --------------------------------------------------------------------------- #


async def search_memory(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    user_id: UUID,
    query: str | None = None,
    memory_type: MemoryType | None = None,
    limit: int = 50,
    now: datetime | None = None,
) -> list[MemoryEntry]:
    """Deterministic listing of *current truth* (view control).

    Excludes SUPERSEDED and DELETED and expired rows, but - because disabling a
    category only blocks retrieval - does *not* exclude disabled categories;
    the user can always see what is stored. Ordered by ``updated_at`` desc with
    id asc as the tie-breaker.
    """
    current = _as_utc(now)
    limit = _bound(limit, _MAX_SEARCH, "limit")
    stmt = _current_truth_scope(tenant_id, user_id, now=current)
    if memory_type is not None:
        _check_type(memory_type)
        stmt = stmt.where(MemoryEntry.memory_type == memory_type.value)
    if query is not None and query.strip():
        stmt = stmt.where(MemoryEntry.content.ilike(_like_pattern(query)))
    stmt = stmt.order_by(
        MemoryEntry.updated_at.desc(), MemoryEntry.id.asc()
    ).limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def retrieve_memory(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    user_id: UUID,
    query: str | None = None,
    memory_types: set[MemoryType] | frozenset[MemoryType] | None = None,
    limit: int = 10,
    now: datetime | None = None,
) -> list[ScoredMemory]:
    """Ranked, deterministic retrieval (the agent-facing context path).

    Pipeline: strict (tenant, user) -> ACTIVE + not-expired -> category filter
    (requested types only) -> disabled categories dropped -> optional keyword
    relevance -> deterministic score -> score/updated_at/id ordering -> top-k.

    Postgres owns all candidate filtering; scoring is a pure deterministic
    function of stored fields + the injected ``now``. No embeddings, vectors,
    or external search services anywhere on this path.
    """
    current = _as_utc(now)
    limit = _bound(limit, _MAX_RETRIEVAL, "limit")

    candidates = await _retrieval_candidates(
        session,
        tenant_id=tenant_id,
        user_id=user_id,
        now=current,
        memory_types=memory_types,
        query=query,
    )
    scored = [ScoredMemory(entry=entry, score=score_memory(entry, current)) for entry in candidates]
    scored.sort(
        key=lambda sm: (
            -sm.score,
            -sm.entry.updated_at.timestamp(),
            str(sm.entry.id),
        )
    )
    return scored[:limit]


async def _retrieval_candidates(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    user_id: UUID,
    now: datetime,
    memory_types: set[MemoryType] | frozenset[MemoryType] | None,
    query: str | None,
) -> list[MemoryEntry]:
    disabled = await _disabled_type_values(session, tenant_id, user_id)
    stmt = _current_truth_scope(tenant_id, user_id, now=now)
    if memory_types:
        for candidate in memory_types:
            _check_type(candidate)
        type_values = [candidate.value for candidate in memory_types]
        stmt = stmt.where(MemoryEntry.memory_type.in_(type_values))
    if disabled:
        stmt = stmt.where(MemoryEntry.memory_type.not_in(disabled))
    if query is not None and query.strip():
        stmt = stmt.where(MemoryEntry.content.ilike(_like_pattern(query)))
    result = await session.execute(stmt)
    return list(result.scalars().all())


def _current_truth_scope(
    tenant_id: UUID, user_id: UUID, *, now: datetime
):
    """STRUCTURED scope: (tenant, user), ACTIVE, not expired.

    Returns a Select; the caller layers category/relevance filters on top.
    """
    return (
        select(MemoryEntry)
        .where(
            MemoryEntry.tenant_id == tenant_id,
            MemoryEntry.user_id == user_id,
            MemoryEntry.status == MemoryStatus.ACTIVE.value,
            _not_expired(now),
        )
    )


# --------------------------------------------------------------------------- #
# Category preferences (disable blocks retrieval only)
# --------------------------------------------------------------------------- #


async def disabled_categories(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    user_id: UUID,
) -> list[str]:
    """The MemoryType values currently blocked from retrieve for this user."""
    return await _disabled_type_values(session, tenant_id, user_id)


async def set_category_enabled(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    user_id: UUID,
    memory_type: MemoryType,
    enabled: bool,
    now: datetime | None = None,
) -> MemoryPreference:
    """Enable or disable one memory category for retrieve.

    Disabling only blocks retrieval; nothing is deleted and viewing still works.
    """
    _check_type(memory_type)
    preference = await _load_preference(session, tenant_id, user_id)

    if preference is None:
        if enabled:
            # Enabling a category that was never disabled is the default state;
            # we still record a preference row so the intent is explicit.
            preference = MemoryPreference(
                tenant_id=tenant_id,
                user_id=user_id,
                disabled_types=[],  # untouched below when enabled
            )
        else:
            preference = MemoryPreference(
                tenant_id=tenant_id,
                user_id=user_id,
                disabled_types=[memory_type.value],
            )
        session.add(preference)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            preference = await _load_preference(session, tenant_id, user_id)
            assert preference is not None
            await _apply_toggle(preference, memory_type, enabled)
            await session.commit()
        await session.refresh(preference)
        return preference

    await _apply_toggle(preference, memory_type, enabled)
    await session.commit()
    await session.refresh(preference)
    return preference


async def _apply_toggle(
    preference: MemoryPreference, memory_type: MemoryType, enabled: bool
) -> None:
    current = list(preference.disabled_types or [])
    if enabled:
        preference.disabled_types = [t for t in current if t != memory_type.value]
    elif memory_type.value not in current:
        current.append(memory_type.value)
        preference.disabled_types = current


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _not_expired(now: datetime):
    """Expiration is not deletion: expired rows are hidden only from ``now`` on."""
    return or_(
        MemoryEntry.expires_at.is_(None), MemoryEntry.expires_at > now
    )


async def _disabled_type_values(
    session: AsyncSession, tenant_id: UUID, user_id: UUID
) -> list[str]:
    preference = await _load_preference(session, tenant_id, user_id)
    if preference is None:
        return []
    return list(dict.fromkeys(preference.disabled_types or []))


async def _load_preference(
    session: AsyncSession, tenant_id: UUID, user_id: UUID
) -> MemoryPreference | None:
    result = await session.execute(
        select(MemoryPreference).where(
            MemoryPreference.tenant_id == tenant_id,
            MemoryPreference.user_id == user_id,
        )
    )
    return result.scalar_one_or_none()


async def _load_entry(
    session: AsyncSession, memory_id: UUID, tenant_id: UUID, user_id: UUID
) -> MemoryEntry:
    result = await session.execute(
        select(MemoryEntry).where(
            MemoryEntry.id == memory_id,
            MemoryEntry.tenant_id == tenant_id,
            MemoryEntry.user_id == user_id,
        )
    )
    entry = result.scalar_one_or_none()
    if entry is None:
        raise MemoryNotFoundError("memory not found for tenant")
    return entry


def _expiry_for(ttl: MemoryTTL, now: datetime) -> datetime | None:
    if ttl is MemoryTTL.PERMANENT:
        return None
    settings = get_settings()
    if ttl is MemoryTTL.LONG_TTL:
        lifetime = timedelta(days=settings.memory_long_ttl_days)
    elif ttl is MemoryTTL.SHORT_TTL:
        lifetime = timedelta(days=settings.memory_short_ttl_days)
    else:  # EPHEMERAL
        lifetime = timedelta(hours=settings.memory_ephemeral_hours)
    return now + lifetime


def _like_pattern(query: str) -> str:
    escaped = (
        query.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
    )
    return f"%{escaped}%"


def _bound(value: int, upper: int, name: str) -> int:
    if value < 1 or value > upper:
        raise MemoryValidationError(f"{name} must be in [1, {upper}]")
    return value


def _clean_content(content: str) -> str:
    cleaned = " ".join(content.split()).strip()
    if not cleaned:
        raise MemoryValidationError("content must not be empty")
    if len(cleaned) > _CONTENT_LIMIT:
        raise MemoryValidationError(f"content too long (max {_CONTENT_LIMIT})")
    return cleaned


def _validate_unit(value: float, field: str) -> float:
    if not isinstance(value, (int, float)) or value < 0.0 or value > 1.0:
        raise MemoryValidationError(f"{field} must be in [0, 1], got {value!r}")
    return float(value)


def _check_type(memory_type: MemoryType) -> None:
    if not isinstance(memory_type, MemoryType):
        raise MemoryValidationError(f"unknown memory type {memory_type!r}")


def _check_source(source: SourceKind) -> None:
    if not isinstance(source, SourceKind):
        raise MemoryValidationError(f"unknown memory source {source!r}")


def _as_utc(now: datetime | None) -> datetime:
    return now if now is not None else datetime.now(UTC)


__all__ = [
    "ScoredMemory",
    "correct_memory",
    "create_memory",
    "delete_memory",
    "disabled_categories",
    "get_memory",
    "retrieve_memory",
    "search_memory",
    "set_category_enabled",
    "update_memory",
]