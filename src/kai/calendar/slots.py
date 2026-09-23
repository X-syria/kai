"""Candidate time slots from primary-calendar busy events.

Availability is computed in one explicit timezone from tz-aware busy events
clipped to the caller's working window. Every slot that leaves this module is
tz-aware so no consumer can mistake a naive timestamp for local time.
"""

from dataclasses import dataclass
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from kai.calendar.models import CalendarEvent

DEFAULT_DAY_START = time(9, 0)
DEFAULT_DAY_END = time(17, 0)


@dataclass(frozen=True)
class TimeSlot:
    """A contiguous interval; both bounds are timezone-aware."""

    start: datetime
    end: datetime


@dataclass(frozen=True)
class Availability:
    """Free (candidate) intervals and busy intervals for one working day."""

    free: tuple[TimeSlot, ...]
    busy: tuple[TimeSlot, ...]
    timezone: str
    day_start: time
    day_end: time


def parse_timezone(timezone: str) -> ZoneInfo:
    """Validate a client-supplied IANA timezone; ``ValueError`` when unknown."""
    try:
        return ZoneInfo(timezone.strip())
    except (KeyError, ValueError) as exc:
        raise ValueError(f"unknown or invalid timezone: {timezone}") from exc


def compute_availability(
    events: list[CalendarEvent],
    *,
    day: date,
    timezone: str,
    day_start: time = DEFAULT_DAY_START,
    day_end: time = DEFAULT_DAY_END,
) -> Availability:
    """Compute free/busy slots for ``day`` in ``timezone``.

    Busy events are clipped to the working window, merged when overlapping,
    and the free slots are the complement. The day is bounded by
    ``[day_start, day_end)``; an all-day-or-empty calendar yields the full
    window as one free slot, an all-day-busy calendar yields none.
    """
    tz = parse_timezone(timezone)
    if day_end <= day_start:
        raise ValueError("day_end must be after day_start")
    window_start = datetime.combine(day, day_start, tzinfo=tz)
    window_end = datetime.combine(day, day_end, tzinfo=tz)

    busy: list[TimeSlot] = []
    for event in events:
        start = max(event.start, window_start)
        end = min(event.end, window_end)
        if end <= start:
            continue
        busy.append(TimeSlot(start=start, end=end))
    busy.sort(key=lambda slot: slot.start)

    merged: list[TimeSlot] = []
    for slot in busy:
        if merged and slot.start <= merged[-1].end:
            merged[-1] = TimeSlot(
                start=merged[-1].start, end=max(merged[-1].end, slot.end)
            )
        else:
            merged.append(slot)

    free: list[TimeSlot] = []
    cursor = window_start
    for slot in merged:
        if slot.start > cursor:
            free.append(TimeSlot(start=cursor, end=slot.start))
        cursor = max(cursor, slot.end)
    if cursor < window_end:
        free.append(TimeSlot(start=cursor, end=window_end))

    return Availability(
        free=tuple(free),
        busy=tuple(merged),
        timezone=timezone.strip(),
        day_start=day_start,
        day_end=day_end,
    )


def iso_interval(slot: TimeSlot) -> dict[str, str]:
    """Serialize a slot with explicit, offset-bearing ISO timestamps."""
    return {"start": slot.start.isoformat(), "end": slot.end.isoformat()}