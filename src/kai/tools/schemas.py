"""Validated argument schemas for every MVP tool.

Each schema uses ``extra="forbid"`` so any unknown key - including reserved
identity/credential keys the LLM might blurt out - is rejected before the
adapter ever sees the arguments. Values are normalized here (timezones parsed,
whitespace trimmed, sizes capped) so executors receive only well-formed,
bounded input.
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    field_validator,
    model_validator,
)

from kai.calendar.slots import parse_timezone

_Timezone = Annotated[str, Field(min_length=1, max_length=64)]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ContactsMatchArgs(_StrictModel):
    """Search the contact list for a free-text match."""

    query: str = Field(min_length=1, max_length=500)

    @field_validator("query")
    @classmethod
    def _clean_query(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("query must not be blank")
        return value


class CalendarReadSlotsArgs(_StrictModel):
    """Compute free time for a day on the user's calendar."""

    date: date
    timezone: _Timezone
    day_start: time = time(9, 0)
    day_end: time = time(17, 0)

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        parse_timezone(value)
        return value.strip()

    @model_validator(mode="after")
    def _day_must_be_ordered(self) -> CalendarReadSlotsArgs:
        if self.day_end <= self.day_start:
            raise ValueError("day_end must be later than day_start")
        return self


class CalendarGetEventArgs(_StrictModel):
    """Fetch a single calendar event."""

    event_id: str = Field(min_length=1, max_length=512)


class CalendarCreateEventArgs(_StrictModel):
    """Create a calendar event and verify it appeared on the calendar."""

    title: str = Field(min_length=1, max_length=255)
    date: date
    start_time: time
    end_time: time
    timezone: _Timezone
    attendees: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        parse_timezone(value)
        return value.strip()

    @field_validator("attendees")
    @classmethod
    def _clean_attendees(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for raw in value:
            address = raw.strip()
            if not address or " " in address or "@" not in address:
                raise ValueError("attendees must contain addresses like name@example.com")
            if len(address) > 320:
                raise ValueError("attendee address is too long")
            cleaned.append(address)
        return cleaned

    @model_validator(mode="after")
    def _window_must_be_ordered(self) -> CalendarCreateEventArgs:
        if self.end_time <= self.start_time:
            raise ValueError("end_time must be later than start_time")
        return self

    def as_aware_window(self) -> tuple[datetime, datetime]:
        """Build aware datetimes in the requested timezone."""
        tzinfo = parse_timezone(self.timezone)
        start = datetime.combine(self.date, self.start_time, tzinfo=tzinfo)
        end = datetime.combine(self.date, self.end_time, tzinfo=tzinfo)
        if end <= start:
            raise ValueError("end_time must be later than start_time")
        return start, end


class GmailSearchArgs(_StrictModel):
    """Search the user's mailbox."""

    query: str = Field(min_length=1, max_length=500)
    max_results: int = Field(default=10, ge=1, le=50)


class GmailReadArgs(_StrictModel):
    """Read a single message body."""

    message_id: str = Field(min_length=1, max_length=512)


class GmailSendArgs(_StrictModel):
    """Send an email and verify it left via the provider."""

    to: EmailStr
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=100_000)

    @field_validator("subject")
    @classmethod
    def _subject_no_headers(cls, value: str) -> str:
        if "\r" in value or "\n" in value:
            raise ValueError("subject must not contain line breaks")
        return value


#: Application-controlled map from tool name to its argument schema. The LLM
#: can only reference these names; it cannot register or attach a new schema.
ARGUMENT_SCHEMAS: dict[str, type[BaseModel]] = {
    "contacts.match": ContactsMatchArgs,
    "calendar.read_slots": CalendarReadSlotsArgs,
    "calendar.get_event": CalendarGetEventArgs,
    "calendar.create_event": CalendarCreateEventArgs,
    "gmail.search": GmailSearchArgs,
    "gmail.read": GmailReadArgs,
    "gmail.send": GmailSendArgs,
}


__all__ = [
    "ARGUMENT_SCHEMAS",
    "CalendarCreateEventArgs",
    "CalendarGetEventArgs",
    "CalendarReadSlotsArgs",
    "ContactsMatchArgs",
    "GmailReadArgs",
    "GmailSearchArgs",
    "GmailSendArgs",
]