"""Memory API — minimal, authenticated, tenant+user scoped.

``tenant_id``/``user_id`` always come from the JWT (see
:func:`kai.auth.deps.get_current_user`), never from a request body, so there is
no field a caller could abuse to touch another tenant's or user's memory.

Endpoints:

* ``GET    /memory``             — view current-truth memories (search/filter)
* ``GET    /memory/categories``  — per-category retrieve status
* ``PUT    /memory/categories/{type}`` — disable/enable one category (internally
  stored per-user; disabling blocks retrieval, never a deletion)
* ``GET    /memory/{id}``        — read one memory by id
* ``POST   /memory``             — create (201)
* ``PATCH  /memory/{id}``        — in-place edit of an ACTIVE memory
* ``POST   /memory/{id}/correct``— supersede with a USER_CORRECTION (201)
* ``DELETE /memory/{id}``        — logical delete (204)

Retrieval for context is a *service* capability (``retrieve_memory``) consumed
by the future Agent Runtime; there is deliberately no public scoring/vector API.
"""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from kai.auth.deps import get_current_user
from kai.db.engine import get_session
from kai.db.models import User
from kai.memory.errors import (
    InvalidMemoryTransition,
    MemoryError,
    MemoryNotFoundError,
    MemoryValidationError,
)
from kai.memory.models import MemoryEntry, MemoryType
from kai.memory.schemas import (
    CategoryListResponse,
    CategoryStatus,
    CategoryToggleRequest,
    MemoryCorrectRequest,
    MemoryCreateRequest,
    MemoryListResponse,
    MemoryResponse,
    MemoryUpdateRequest,
)
from kai.memory.scoring import freshness
from kai.memory.service import (
    correct_memory,
    create_memory,
    delete_memory,
    disabled_categories,
    get_memory,
    search_memory,
    set_category_enabled,
    update_memory,
)

router = APIRouter(prefix="/memory", tags=["memory"])

#: Memory types, ordered deterministically for the category listing.
_CATEGORY_ORDER: tuple[MemoryType, ...] = tuple(MemoryType)


def _serialize(entry: MemoryEntry) -> dict:
    return {
        "id": str(entry.id),
        "tenant_id": str(entry.tenant_id),
        "user_id": str(entry.user_id),
        "type": entry.memory_type,
        "content": entry.content,
        "source": entry.source,
        "confidence": entry.confidence,
        "importance": entry.importance,
        "freshness": freshness(entry.updated_at, datetime.now(UTC)),
        "recurrence": entry.recurrence,
        "future_relevance": entry.future_relevance,
        "status": entry.status,
        "created_at": entry.created_at,
        "updated_at": entry.updated_at,
        "expires_at": entry.expires_at,
        "supersedes_id": str(entry.supersedes_id) if entry.supersedes_id else None,
        "superseded_by_id": (
            str(entry.superseded_by_id) if entry.superseded_by_id else None
        ),
    }


def _to_response(entry: MemoryEntry) -> MemoryResponse:
    return MemoryResponse.model_validate(_serialize(entry))


def _memory_error(exc: MemoryError) -> HTTPException:
    if isinstance(exc, MemoryNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, InvalidMemoryTransition):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, MemoryValidationError):
        return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.post("", response_model=MemoryResponse, status_code=201)
async def memory_create(
    body: MemoryCreateRequest,
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> MemoryResponse:
    entry = await create_memory(
        session,
        tenant_id=current.tenant_id,
        user_id=current.id,
        memory_type=body.type,
        content=body.content,
        source=body.source,
        confidence=body.confidence,
        importance=body.importance,
        recurrence=body.recurrence,
        future_relevance=body.future_relevance,
        ttl=body.ttl,
    )
    return _to_response(entry)


@router.get("", response_model=MemoryListResponse)
async def memory_list(
    q: str | None = None,
    type: MemoryType | None = None,
    limit: int = 50,
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> MemoryListResponse:
    try:
        entries = await search_memory(
            session,
            tenant_id=current.tenant_id,
            user_id=current.id,
            query=q,
            memory_type=type,
            limit=limit,
        )
    except MemoryError as exc:
        raise _memory_error(exc) from exc
    return MemoryListResponse(memories=[_to_response(e) for e in entries])


@router.get("/categories", response_model=CategoryListResponse)
async def memory_categories(
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> CategoryListResponse:
    disabled = await disabled_categories(
        session, tenant_id=current.tenant_id, user_id=current.id
    )
    disabled_set = set(disabled)
    categories = [
        CategoryStatus(
            memory_type=memory_type.value,
            enabled=memory_type.value not in disabled_set,
        )
        for memory_type in _CATEGORY_ORDER
    ]
    return CategoryListResponse(categories=categories)


@router.put("/categories/{memory_type}", response_model=CategoryStatus)
async def memory_category_toggle(
    memory_type: MemoryType,
    body: CategoryToggleRequest,
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> CategoryStatus:
    try:
        await set_category_enabled(
            session,
            tenant_id=current.tenant_id,
            user_id=current.id,
            memory_type=memory_type,
            enabled=body.enabled,
        )
    except MemoryError as exc:
        raise _memory_error(exc) from exc
    return CategoryStatus(
        memory_type=memory_type.value,
        enabled=body.enabled,
    )


@router.get("/{memory_id}", response_model=MemoryResponse)
async def memory_get(
    memory_id: UUID,
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> MemoryResponse:
    try:
        entry = await get_memory(
            session,
            memory_id=memory_id,
            tenant_id=current.tenant_id,
            user_id=current.id,
        )
    except MemoryError as exc:
        raise _memory_error(exc) from exc
    return _to_response(entry)


@router.patch("/{memory_id}", response_model=MemoryResponse)
async def memory_update(
    memory_id: UUID,
    body: MemoryUpdateRequest,
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> MemoryResponse:
    try:
        entry = await update_memory(
            session,
            memory_id=memory_id,
            tenant_id=current.tenant_id,
            user_id=current.id,
            content=body.content,
            importance=body.importance,
            recurrence=body.recurrence,
            future_relevance=body.future_relevance,
            ttl=body.ttl,
        )
    except MemoryError as exc:
        raise _memory_error(exc) from exc
    return _to_response(entry)


@router.post("/{memory_id}/correct", response_model=MemoryResponse, status_code=201)
async def memory_correct(
    memory_id: UUID,
    body: MemoryCorrectRequest,
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> MemoryResponse:
    try:
        corrected, _ = await correct_memory(
            session,
            memory_id=memory_id,
            tenant_id=current.tenant_id,
            user_id=current.id,
            content=body.content,
            importance=body.importance,
        )
    except MemoryError as exc:
        raise _memory_error(exc) from exc
    return _to_response(corrected)


@router.delete("/{memory_id}", status_code=204)
async def memory_delete(
    memory_id: UUID,
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> Response:
    try:
        await delete_memory(
            session,
            memory_id=memory_id,
            tenant_id=current.tenant_id,
            user_id=current.id,
        )
    except MemoryError as exc:
        raise _memory_error(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)