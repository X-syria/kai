"""Calendar read endpoints.

Every operation resolves the user's Google account through
``(current.tenant_id, current.user_id)`` — an ``account_id`` from another
tenant simply does not resolve. Times are validated into explicit,
timezone-aware datetimes; availability is candidate free slots in the request
timezone. No token or credential is ever echoed.

**This module performs no external side effect.** Event creation was removed
from the public API because it reached ``create_and_verify_event`` directly,
skipping the ToolGateway — and with it policy, approval, identity resolution,
durable evidence, audit and alerting. ``POST /calendar/events`` now answers
``410 Gone`` and names the governed path. Calendar writes go through
``POST /runtime/tasks``; see ``kai.runtime`` for the only execution boundary.
"""

from datetime import date as date_type
from datetime import datetime, time
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
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
    list_events,
)
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
from kai.google.client import GoogleOAuthError
from kai.google.deps import get_google_http_client
from kai.google.service import GOOGLE_RECONNECT_DETAIL, GoogleReconnectRequiredError

router = APIRouter(prefix="/calendar", tags=["calendar"])


#: The single governed path to an external calendar write. Named here so a
#: caller that used this endpoint is told where to go rather than only that it
#: failed.
GOVERNED_WRITE_PATH = "/runtime/tasks"


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
    except GoogleReconnectRequiredError as exc:
        # The stored grant can never be renewed. This is the one refresh
        # outcome a retry cannot fix, so it is not a 5xx.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=GOOGLE_RECONNECT_DETAIL,
        ) from exc
    except GoogleOAuthError as exc:
        # Transient token-endpoint failure: no provider detail is reflected.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="google provider temporarily unavailable",
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
async def calendar_create_event_retired() -> None:
    """Refuse the retired direct calendar write and name the governed path.

    This endpoint used to call ``create_and_verify_event`` directly. That made
    it a second, ungoverned route to the same provider write the runtime
    already performs through ``ToolGateway``: no policy, no approval, no
    identity resolution, no durable evidence, no audit, no alerting. It is
    kept as an explicit ``410`` rather than deleted so a caller is told where
    the capability went instead of only receiving a bare ``404``.

    It performs no external side effect, so it cannot reintroduce the bypass.
    """
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail={
            "error": "direct_calendar_write_removed",
            "reason": (
                "this endpoint bypassed the governed execution boundary "
                "(policy, approval, identity, evidence, audit)"
            ),
            "use": GOVERNED_WRITE_PATH,
        },
    )
