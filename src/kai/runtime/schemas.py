"""Wire schemas for the Agent Runtime vertical slice.

Two layered boundaries, both strict:

* The user-facing submission is validated against a small, sealed vocabulary
  (``goal`` + ``timezone`` only). Nothing outside that shape is accepted, and
  the timezone is parsed with the same validator the calendar service trusts.
* The model's *understanding* output is validated as data: ``extra="forbid"``
  so a model can never smuggle permission/tool/risk fields into the scenario,
  an explicit ``intent`` (only ``schedule_meeting`` exists today), bounded
  guests, and a bounded meeting window.

Every check here is deterministic application code; nothing consults the LLM
and nothing talks to the database.
"""

from __future__ import annotations

from datetime import date, time
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from kai.calendar.slots import parse_timezone

_MAX_GUESTS = 5
_MAX_GOAL = 2000
_MAX_TITLE = 255
_MAX_TZ = 64
_MAX_WINDOW_DAYS = 14


class GoalSubmission(BaseModel):
    """The only user-supplied input the Agent Runtime accepts."""

    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=1, max_length=_MAX_GOAL)
    timezone: str = Field(min_length=1, max_length=_MAX_TZ)

    @field_validator("goal")
    @classmethod
    def _goal_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("goal must not be blank")
        return stripped

    @field_validator("timezone")
    @classmethod
    def _timezone(cls, value: str) -> str:
        parse_timezone(value)
        return value.strip()


class MeetingWindow(BaseModel):
    """Bounded meeting window; all checks deterministic."""

    model_config = ConfigDict(extra="forbid")

    start_date: date
    end_date: date
    earliest_time: time = time(9, 0)
    title: str = Field(default="KAI meeting", min_length=1, max_length=_MAX_TITLE)

    @field_validator("title")
    @classmethod
    def _title_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("title must not be blank")
        return stripped

    @model_validator(mode="after")
    def _window_sane(self) -> Self:
        if self.end_date < self.start_date:
            raise ValueError("end_date must not be before start_date")
        if (self.end_date - self.start_date).days > _MAX_WINDOW_DAYS:
            raise ValueError(f"meeting window exceeds {_MAX_WINDOW_DAYS} days")
        return self


class UnderstandingOutput(BaseModel):
    """The strictly validated scenario decoded from the model's JSON.

    The model only produces *data*: a meeting intent, guest names, and a
    bounded window. There is intentionally no field for permissions, tools,
    risk, cost, or model configuration.
    """

    model_config = ConfigDict(extra="forbid")

    intent: Literal["schedule_meeting"] = "schedule_meeting"
    guests: list[str] = Field(min_length=1, max_length=_MAX_GUESTS)
    window: MeetingWindow

    @field_validator("guests", mode="before")
    @classmethod
    def _normalize_guests(cls, value: object) -> list[str]:
        """Accept ``["Name"]`` or ``[{"name": "Name"}]``; reject anything else."""
        if not isinstance(value, list) or not value:
            raise ValueError("guests must be a non-empty list")
        normalized: list[str] = []
        for item in value:
            if isinstance(item, str):
                raw = item
            elif isinstance(item, dict) and "name" in item:
                raw = item["name"]
            else:
                raise ValueError("guests entries must be strings or {name: ...}")
            if not isinstance(raw, str):
                raise ValueError("guest names must be text")
            cleaned = " ".join(raw.split()).strip()
            if not cleaned:
                raise ValueError("guest names must not be blank")
            normalized.append(cleaned)
        return normalized


class SubmitResponse(BaseModel):
    task_id: str
    task_state: str
    goal: str


class TriggerResponse(BaseModel):
    task_id: str
    task_state: str
    outcome: str
    message: str
    approval_ids: list[str] = Field(default_factory=list)
    steps_used: int = 0


class RuntimeTaskResponse(BaseModel):
    task_id: str
    task_state: str
    goal: str
    timezone: str | None = None
    scenario: UnderstandingOutput | None = None
    identities: list[dict] = Field(default_factory=list)
    evidence: list[dict] = Field(default_factory=list)
    cursor: int = 0
    steps_used: int = 0
    next_action: str
    approval_ids: list[str] = Field(default_factory=list)
    last_error: str | None = None


__all__ = [
    "GoalSubmission",
    "MeetingWindow",
    "RuntimeTaskResponse",
    "SubmitResponse",
    "TriggerResponse",
    "UnderstandingOutput",
]