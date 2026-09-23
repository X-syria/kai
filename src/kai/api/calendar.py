"""Calendar vertical slice endpoints.

Every operation resolves the user's Google account through
``(current.tenant_id, current.user_id)`` — an ``account_id`` from another
tenant simply does not resolve. Times are validated into explicit,
timezone-aware datetimes; availability is candidate free slots in the request
timezone; event creation is reported as success only after a deterministic
read-after-create verification passes. No token or credential is ever echoed.
"""

from datetime import date as date_type
from datetime import datetime, time
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kai.auth.deps import get_current_user
from kai.calendar.client import (
    GoogleApiError,
    GoogleForbiddenError,
    GoogleInvalidTokenError,
    GoogleRateLimitError,
)
from kai.calendar.models import CalendarEvent
from kai.calendar.service import (
    AccountNotFoundError,
    create_and_verify_event,
    list_events,
)
from kai.calendar.service_models import CreateEventRequest, FieldMismatch
from kai.calendar.slots import (
    DEFAULT_DAY_END,
    DEFAULT_DAY_START,
    Availability,
    compute_availability,
    iso_interval,
    parse_timezone,
)
from kai.db.engine import get_session
from kai.db.models import GoogleAccount, User
from kai.google.deps import get_google_http_client

router = APIRouter(prefix="/calendar", tags=["calendar"])


class CreateEventBody(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    date: str = Field(examples=["2026-06-24"])
    start_time: str = Field(examples=["10:00"])
    end_time: str = Field(examples=["11:00"])
    timezone: str = Field(examples=["Europe/Berlin"])
    attendees: list[EmailStr] = Field(default_factory=list, max_length=50)


async def _resolve_account(
    session: AsyncSession, current: User, account_id: UUID | None
) -> GoogleAccount:
    query = select(GoogleAccount).where(
        GoogleAccount.tenant_id == current.tenant_id,
        GoogleAccount.user_id == current.id,
    )
    if account_id is not None:
        query = query.where(GoogleAccount.id == account_id)
    else:
        query = query.order_by(GoogleAccount.created_at, GoogleAccount.id).limit(1)
    account = (await session.execute(query)).scalar_one_or_none()
    if account is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no connected google account",
        )
    return account


def _serialize_event(event: CalendarEvent) -> dict:
    return {
        "event_id": event.event_id,
        "title": event.summary,
        "start": event.start.isoformat(),
        "end": event.end.isoformat(),
        "start_timezone": event.start_timezone,
        "end_timezone": event.end_timezone,
        "attendees": [attendee.email for attendee in event.attendees],
    }


def _build_create_request(body: CreateEventBody) -> CreateEventRequest:
    try:
        day = date_type.fromisoformat(body.date)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="date must be a valid YYYY-MM-DD date",
        ) from exc
    try:
        start_time = time.fromisoformat(body.start_time)
        end_time = time.fromisoformat(body.end_time)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="start_time/end_time must be valid HH:MM times",
        ) from exc
    try:
        tz = parse_timezone(body.timezone)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    start = datetime.combine(day, start_time, tzinfo=tz)
    end = datetime.combine(day, end_time, tzinfo=tz)
    if end <= start:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="end_time must be after start_time",
        )
    return CreateEventRequest(
        title=body.title.strip(),
        start=start,
        end=end,
        timezone=body.timezone.strip(),
        attendees=tuple(str(email) for email in body.attendees),
    )


def _serialize_availability(availability: Availability, day: date_type) -> dict:
    return {
        "timezone": availability.timezone,
        "date": day.isoformat(),
        "day_start": availability.day_start.isoformat(),
        "day_end": availability.day_end.isoformat(),
        "free": [iso_interval(slot) for slot in availability.free],
        "busy": [iso_interval(slot) for slot in availability.busy],
    }


@router.get("/slots")
async def calendar_slots(
    date: str,
    timezone: str,
    day_start: str = DEFAULT_DAY_START.isoformat(),
    day_end: str = DEFAULT_DAY_END.isoformat(),
    account_id: UUID | None = None,
    current: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    transport: httpx.AsyncClient = Depends(get_google_http_client),
) -> dict:
    try:
        day = date_type.fromisoformat(date)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="date must be a valid YYYY-MM-DD date",
        ) from exc
    try:
        tz = parse_timezone(timezone)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    try:
        start_o = time.fromisoformat(day_start)
        end_o = time.fromisoformat(day_end)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="day_start/day_end must be valid HH:MM times",
        ) from exc
    if end_o <= start_o:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="day_end must be after day_start",
        )

    account = await _resolve_account(session, current, account_id)
    window_start = datetime.combine(day, start_o, tzinfo=tz)
    window_end = datetime.combine(day, end_o, tzinfo=tz)
    try:
        events = await list_events(
            session,
            transport,
            account_id=account.id,
            tenant_id=current.tenant_id,
            start=window_start,
            end=window_end,
        )
    except AccountNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except GoogleInvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)
        ) from exc
    except GoogleRateLimitError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc)
        ) from exc
    except GoogleForbiddenError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)
        ) from exc
    except GoogleApiError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        ) from exc

    availability = compute_availability(
        events, day=day, timezone=timezone, day_start=start_o, day_end=end_o
    )
    return _serialize_availability(availability, day)


@router.post("/events")
async def calendar_create_event(
    body: CreateEventBody,
    account_id: UUID | None = None,
    current: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    transport: httpx.AsyncClient = Depends(get_google_http_client),
) -> dict:
    account = await _resolve_account(session, current, account_id)
    req = _build_create_request(body)
    try:
        outcome = await create_and_verify_event(
            session,
            transport,
            account_id=account.id,
            tenant_id=current.tenant_id,
            req=req,
        )
    except AccountNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except GoogleInvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)
        ) from exc
    except GoogleRateLimitError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc)
        ) from exc
    except GoogleForbiddenError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)
        ) from exc
    except GoogleApiError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        ) from exc

    if not outcome.verified:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "verified": False,
                "mismatches": [
                    _mismatch_payload(mismatch) for mismatch in outcome.mismatches
                ],
            },
        )
    return {
        "verified": True,
        "event": _serialize_event(outcome.event) if outcome.event else None,
    }


def _mismatch_payload(mismatch: FieldMismatch) -> dict:
    return {
        "field": mismatch.field,
        "expected": mismatch.expected,
        "actual": mismatch.actual,
    }