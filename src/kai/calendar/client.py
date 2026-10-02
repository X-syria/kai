"""Google Calendar API client for the vertical slice.

Network traffic only flows through an injected ``httpx.AsyncClient`` so tests
can isolate Google's network behind ``httpx.MockTransport`` while the
database and credential decryption stay real. The slice reads and writes only
the user's primary calendar — the smallest surface that supports availability
and event creation — under the ``calendar.events`` scope granted at connect.
"""

from datetime import datetime
from urllib.parse import quote

import httpx

from kai.calendar.models import (
    CalendarEvent,
    EventsPage,
    parse_event_payload,
    parse_events_payload,
)
from kai.calendar.service_models import CreateEventRequest
from kai.observability import metrics
from kai.observability.providers import provider_call

EVENTS_COLLECTION_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
MAX_RESULTS = 250
MAX_PAGES = 50
MAX_EVENTS = 10_000


class GoogleApiError(RuntimeError):
    """Raised when Google's Calendar API rejects or fails a request."""


class GoogleInvalidTokenError(GoogleApiError):
    """HTTP 401: the stored access token is invalid or expired."""


class GoogleForbiddenError(GoogleApiError):
    """HTTP 403: scope is missing or consent was revoked."""


class GoogleRateLimitError(GoogleApiError):
    """HTTP 429: quota/rate limit exceeded."""


class GoogleNotFoundError(GoogleApiError):
    """HTTP 404: the requested event does not exist."""


def _raise_for_status(resp: httpx.Response) -> None:
    """Map Google's failure modes onto typed errors (never echoes tokens)."""
    if resp.status_code == 401:
        raise GoogleInvalidTokenError(
            "google access token is invalid or expired (consent may be revoked)"
        )
    if resp.status_code == 403:
        raise GoogleForbiddenError(
            "google denied the request; calendar.events scope may be missing"
        )
    if resp.status_code == 404:
        raise GoogleNotFoundError("google calendar event was not found")
    if resp.status_code == 429:
        raise GoogleRateLimitError("google api rate limit exceeded, try again later")
    if resp.status_code >= 400:
        raise GoogleApiError(f"google calendar api request failed ({resp.status_code})")


async def fetch_events_page(
    transport: httpx.AsyncClient,
    *,
    access_token: str,
    start: datetime,
    end: datetime,
    page_token: str | None = None,
) -> EventsPage:
    """Fetch one page of timed events within ``[start, end)`` on the primary calendar.

    ``singleEvents=true`` expands recurring events and enables
    ``orderBy=startTime``, so busy intervals are resolved, not repeated rules.
    """
    params: dict[str, str] = {
        "timeMin": start.isoformat(),
        "timeMax": end.isoformat(),
        "singleEvents": "true",
        "orderBy": "startTime",
        "maxResults": str(MAX_RESULTS),
    }
    if page_token:
        params["pageToken"] = page_token
    with provider_call(provider=metrics.PROVIDER_GOOGLE, operation="list_events"):
        resp = await transport.get(
            EVENTS_COLLECTION_URL,
            params=params,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        _raise_for_status(resp)
        try:
            payload = resp.json()
        except ValueError as exc:
            raise GoogleApiError(
                "google calendar api returned a malformed response"
            ) from exc
        return parse_events_payload(payload)


async def fetch_events(
    transport: httpx.AsyncClient,
    *,
    access_token: str,
    start: datetime,
    end: datetime,
) -> list[CalendarEvent]:
    """Fetch every timed event in the window, bounded against bad pagination."""
    events: list[CalendarEvent] = []
    page_token: str | None = None
    for _ in range(MAX_PAGES):
        page = await fetch_events_page(
            transport,
            access_token=access_token,
            start=start,
            end=end,
            page_token=page_token,
        )
        events.extend(page.events)
        if not page.has_more or len(events) >= MAX_EVENTS:
            break
        page_token = page.next_page_token
    return events


async def create_event(
    transport: httpx.AsyncClient,
    *,
    access_token: str,
    req: CreateEventRequest,
) -> CalendarEvent:
    """Create a timed event on the primary calendar and parse it back."""
    body: dict = {
        "summary": req.title,
        "start": {"dateTime": req.start.isoformat(), "timeZone": req.timezone},
        "end": {"dateTime": req.end.isoformat(), "timeZone": req.timezone},
        "attendees": [{"email": email} for email in req.attendees],
    }
    with provider_call(provider=metrics.PROVIDER_GOOGLE, operation="create_event"):
        resp = await transport.post(
            EVENTS_COLLECTION_URL,
            json=body,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        _raise_for_status(resp)
        try:
            payload = resp.json()
        except ValueError as exc:
            raise GoogleApiError(
                "google calendar create returned a malformed response"
            ) from exc
        event = parse_event_payload(payload)
        if event is None:
            raise GoogleApiError("google calendar create returned an unparseable event")
        return event


async def fetch_event(
    transport: httpx.AsyncClient, *, access_token: str, event_id: str
) -> CalendarEvent:
    """Read one event back from the primary calendar (read-after-create)."""
    url = f"{EVENTS_COLLECTION_URL}/{quote(event_id, safe='')}"
    with provider_call(provider=metrics.PROVIDER_GOOGLE, operation="read_event"):
        resp = await transport.get(
            url, headers={"Authorization": f"Bearer {access_token}"}
        )
        _raise_for_status(resp)
        try:
            payload = resp.json()
        except ValueError as exc:
            raise GoogleApiError(
                "google calendar read returned a malformed response"
            ) from exc
        event = parse_event_payload(payload)
        if event is None:
            raise GoogleApiError("google calendar read returned an unparseable event")
        return event