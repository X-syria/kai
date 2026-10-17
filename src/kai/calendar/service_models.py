"""Request/result shapes shared by the Calendar service and wire client."""

from dataclasses import dataclass, field
from datetime import datetime

from kai.calendar.models import CalendarEvent


@dataclass(frozen=True)
class CreateEventRequest:
    """A validated event creation request; bounds are timezone-aware."""

    title: str
    start: datetime
    end: datetime
    timezone: str
    attendees: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class FieldMismatch:
    """One deterministic verification mismatch between expectation and reality."""

    field: str
    expected: str
    actual: str


@dataclass(frozen=True)
class CreateEventOutcome:
    """The read-after-create result. ``verified`` is True only when the event
    read back from Google matches every requested field exactly.

    ``created_event_id`` is the id the provider returned for the accepted write.
    It is retained even when the read-back could not confirm the event, so an
    accepted creation is never silently forgotten just because it could not be
    read back.
    """

    event: CalendarEvent | None
    mismatches: tuple[FieldMismatch, ...] = field(default_factory=tuple)
    created_event_id: str | None = None

    @property
    def verified(self) -> bool:
        return not self.mismatches