"""Domain shapes and defensive parsing for Google Calendar data.

Calendar API payloads are external, untrusted input — values, not
instructions. Every field is parsed defensively and timezone handling is
explicit: a timed event's ``dateTime`` is normalized to a timezone-aware
:class:`datetime` (using the declared IANA ``timeZone`` when the string has no
offset, UTC as the deterministic fallback), never left naive. All-day events
(``date`` only) carry no start/end time in a timezone and are outside the
slice, so they are skipped instead of guessed at.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

UNKNOWN_TZ = "UTC"


@dataclass(frozen=True)
class Attendee:
    """A single participant; only the email is used for verification."""

    email: str
    display_name: str = ""


@dataclass(frozen=True)
class CalendarEvent:
    """A single timed event with explicit timezone-aware bounds."""

    event_id: str
    summary: str
    start: datetime
    end: datetime
    start_timezone: str
    end_timezone: str
    attendees: tuple[Attendee, ...]


@dataclass(frozen=True)
class EventsPage:
    """One page of calendar events plus pagination state."""

    events: tuple[CalendarEvent, ...]
    has_more: bool
    next_page_token: str | None


def _attach_timezone(parsed: datetime, declared: str) -> datetime | None:
    """Attach an explicit zone to a naive datetime; ``None`` when hostile.

    An offset-bearing ``dateTime`` is already unambiguous, so a hostile
    ``timeZone`` label is ignored for it. A naive one either declares an IANA
    ``timeZone`` to attach or is treated as UTC — a silent assumption is an
    explicit one; an unknown zone label is external garbage and drops the event.
    """
    if parsed.tzinfo is not None:
        return parsed
    try:
        zone = ZoneInfo(declared if declared.strip() else UNKNOWN_TZ)
    except (KeyError, ValueError):
        return None
    return parsed.replace(tzinfo=zone)


def _zone_label(parsed: datetime, declared: str) -> str:
    if declared.strip():
        return declared.strip()
    return str(parsed.tzname() or UNKNOWN_TZ)


def _parse_bound(node: Mapping[str, object]) -> tuple[datetime, str] | None:
    date_time = node.get("dateTime")
    if not isinstance(date_time, str) or not date_time.strip():
        return None
    try:
        parsed = datetime.fromisoformat(date_time.strip())
    except ValueError:
        return None
    declared = node.get("timeZone")
    declared = declared if isinstance(declared, str) else ""
    attached = _attach_timezone(parsed, declared)
    if attached is None or attached.tzinfo is None or attached.utcoffset() is None:
        return None
    return attached, _zone_label(attached, declared)


def _parse_attendees(node: object) -> tuple[Attendee, ...]:
    if not isinstance(node, list):
        return ()
    attendees: list[Attendee] = []
    for entry in node:
        if not isinstance(entry, dict):
            continue
        email = entry.get("email")
        if not isinstance(email, str) or not email.strip():
            continue
        display_name = entry.get("displayName")
        attendees.append(
            Attendee(
                email=email.strip(),
                display_name=display_name.strip() if isinstance(display_name, str) else "",
            )
        )
    return tuple(attendees)


def parse_event_item(entry: object) -> CalendarEvent | None:
    """Turn one untrusted event item into :class:`CalendarEvent`, or skip it.

    Malformed entries return ``None`` so one hostile event never breaks a
    whole read; they surface as fewer events, never as instructions.
    """
    if not isinstance(entry, dict):
        return None
    event_id = entry.get("id")
    if not isinstance(event_id, str) or not event_id.strip():
        return None

    start_node = entry.get("start")
    end_node = entry.get("end")
    if not isinstance(start_node, dict) or not isinstance(end_node, dict):
        return None
    start = _parse_bound(start_node)
    end = _parse_bound(end_node)
    if start is None or end is None:
        return None
    start_dt, start_tz = start
    end_dt, end_tz = end
    if end_dt <= start_dt:
        return None

    summary = entry.get("summary")
    return CalendarEvent(
        event_id=event_id.strip(),
        summary=summary.strip() if isinstance(summary, str) else "",
        start=start_dt,
        end=end_dt,
        start_timezone=start_tz,
        end_timezone=end_tz,
        attendees=_parse_attendees(entry.get("attendees")),
    )


def parse_events_payload(payload: object) -> EventsPage:
    """Turn an untrusted events.list payload into :class:`EventsPage`."""
    if not isinstance(payload, dict):
        return EventsPage(events=(), has_more=False, next_page_token=None)
    raw_items = payload.get("items")
    items = raw_items if isinstance(raw_items, list) else []
    events: list[CalendarEvent] = []
    for entry in items:
        parsed = parse_event_item(entry)
        if parsed is not None:
            events.append(parsed)
    next_token = payload.get("nextPageToken")
    next_page_token: str | None = None
    if isinstance(next_token, str) and next_token.strip():
        has_more = True
        next_page_token = next_token.strip()
    else:
        has_more = False
    return EventsPage(
        events=tuple(events),
        has_more=has_more,
        next_page_token=next_page_token,
    )


def parse_event_payload(payload: object) -> CalendarEvent | None:
    """Turn one untrusted events.get/create payload into :class:`CalendarEvent`."""
    return parse_event_item(payload)