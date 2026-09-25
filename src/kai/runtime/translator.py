"""Deterministic translation for the Agent Runtime slice.

Everything in this module is pure application logic: it maps the planner's
abstract capabilities to the exact tool calls of this slice, derives the
attendee email hint from memory, and picks the meeting slot from persisted
availability — none of it consults the model and none of it executes tools.

The slice contract is fixed at submission time so a re-trigger can never widen
scope: the same ``allowed_tools``, risk policy, budget, success criteria and
fallback are used for every task created through the runtime.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from typing import Any, Literal

from kai.memory.models import MemoryType
from kai.memory.service import retrieve_memory
from kai.runtime.schemas import MeetingWindow, UnderstandingOutput
from kai.tasks.schemas import TaskContractInput

# --- the sealed slice contract ------------------------------------------- #
SLICE_ALLOWED_TOOLS: list[str] = [
    "contacts.match",
    "calendar.read_slots",
    "calendar.get_event",
    "calendar.create_event",
]
SLICE_RISK_POLICY: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"] = "MEDIUM"
SLICE_MAX_STEPS = 10
SLICE_VERIFICATION_METHOD: Literal["automatic", "manual", "ai_review"] = "automatic"
SLICE_FALLBACK = "ask the user to choose a different time"
SLICE_SUCCESS_CRITERIA: list[str] = [
    "calendar event created",
    "attendee is the requested contact",
    "event fits the requested time window",
]

#: Capabilities the slice may plan with; anything else is refused at runtime
#: even if the planner's own vocabulary would accept it.
SLICE_CAPABILITIES: frozenset[str] = frozenset(
    {
        "resolve_contact",
        "read_calendar",
        "create_calendar_event",
        "verify_calendar_event",
    }
)

_CAPABILITY_TO_TOOL: dict[str, str] = {
    "resolve_contact": "contacts.match",
    "read_calendar": "calendar.read_slots",
    "create_calendar_event": "calendar.create_event",
    "verify_calendar_event": "calendar.get_event",
}

_DAY_END = time(17, 0)
_MEETING_DURATION = timedelta(minutes=60)
_MAX_CANDIDATE_DAYS = 4

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def build_slice_contract(goal: str) -> TaskContractInput:
    """The fixed, scenario-agnostic contract every runtime task starts with."""
    return TaskContractInput(
        goal=goal,
        success_criteria=list(SLICE_SUCCESS_CRITERIA),
        allowed_tools=list(SLICE_ALLOWED_TOOLS),
        risk_policy=SLICE_RISK_POLICY,
        max_steps=SLICE_MAX_STEPS,
        verification_method=SLICE_VERIFICATION_METHOD,
        fallback=SLICE_FALLBACK,
    )


def extract_first_email(text: str) -> str | None:
    """The first plain email address in free text, or ``None``."""
    match = _EMAIL_RE.search(text)
    return match.group(0) if match else None


async def memory_email_hint(
    session: Any,
    *,
    tenant_id: Any,
    user_id: Any,
    name: str,
) -> str | None:
    """One best-effort email hint for an attendee, from PERSON memory only.

    Memory is context, never authority: the hint still has to survive the
    Identity Resolver (exact email against the connected account's contacts)
    before any tool runs.
    """
    scored = await retrieve_memory(
        session,
        tenant_id=tenant_id,
        user_id=user_id,
        query=name,
        memory_types={MemoryType.PERSON},
        limit=5,
    )
    for item in scored:
        email = extract_first_email(item.entry.content)
        if email:
            return email
    return None


def candidate_days(window: MeetingWindow) -> list[date]:
    """Up to the first four workdays (Mon-Fri) inside the requested window."""
    days: list[date] = []
    cursor = window.start_date
    while cursor <= window.end_date and len(days) < _MAX_CANDIDATE_DAYS:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


def read_intent_args(*, day: date, timezone: str, earliest_time: time) -> dict[str, Any]:
    """Arguments for one ``calendar.read_slots`` call for a candidate day."""
    return {
        "date": day.isoformat(),
        "timezone": timezone,
        "day_start": earliest_time.isoformat(),
        "day_end": _DAY_END.isoformat(),
    }


def parse_free_intervals(
    free: list[Any] | None,
) -> list[tuple[datetime, datetime]]:
    """Parse ``{"start": iso, "end": iso}`` intervals into aware datetimes."""
    parsed: list[tuple[datetime, datetime]] = []
    for item in free or []:
        if not isinstance(item, dict):
            continue
        start_raw, end_raw = item.get("start"), item.get("end")
        if not isinstance(start_raw, str) or not isinstance(end_raw, str):
            continue
        try:
            start = datetime.fromisoformat(start_raw)
            end = datetime.fromisoformat(end_raw)
        except ValueError:
            continue
        if start >= end:
            continue
        parsed.append((start, end))
    parsed.sort(key=lambda pair: pair[0])
    return parsed


def pick_slot(
    free: list[tuple[datetime, datetime]] | None,
) -> tuple[date, time, time] | None:
    """The earliest free interval long enough for the meeting, or ``None``.

    Returns ``(day, start, end)``; the chosen end uses the full meeting length
    so every candidate slot leaves at least one meeting-sized gap.
    """
    for start, end in free or []:
        if end - start >= _MEETING_DURATION:
            slot_start = start.time()
            slot_end = (start + _MEETING_DURATION).time()
            return start.date(), slot_start, slot_end
    return None


def create_intent_args(
    *,
    slot: tuple[date, time, time],
    title: str,
    timezone: str,
    attendee_emails: list[str],
) -> dict[str, Any]:
    day, start_t, end_t = slot
    return {
        "title": title,
        "date": day.isoformat(),
        "start_time": start_t.isoformat(),
        "end_time": end_t.isoformat(),
        "timezone": timezone,
        "attendees": list(attendee_emails),
    }


def capability_to_tool(action: str) -> str | None:
    """The registered tool name for a plan capability, or ``None`` when the
    slice cannot represent it (always fail closed)."""
    return _CAPABILITY_TO_TOOL.get(action)


def content_category_for(tool_name: str) -> str:
    """The trusted content category this slice attaches to each tool call.

    Both categories are in the application's ALLOW table; the value is set by
    the runtime, never parsed out of message content.
    """
    if tool_name == "calendar.create_event":
        return "confirm_meeting"
    return "propose_time"


def scenario_from_row(payload: Any) -> UnderstandingOutput:
    return UnderstandingOutput.model_validate(payload)


__all__ = [
    "SLICE_ALLOWED_TOOLS",
    "SLICE_CAPABILITIES",
    "SLICE_FALLBACK",
    "SLICE_MAX_STEPS",
    "SLICE_RISK_POLICY",
    "SLICE_SUCCESS_CRITERIA",
    "SLICE_VERIFICATION_METHOD",
    "build_slice_contract",
    "candidate_days",
    "capability_to_tool",
    "content_category_for",
    "create_intent_args",
    "extract_first_email",
    "memory_email_hint",
    "parse_free_intervals",
    "pick_slot",
    "read_intent_args",
    "scenario_from_row",
]