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

The public progress snapshot is a *projection*, never the stored state: tool
arguments, provider payloads, verification evidence, and unmasked attendee
addresses stay inside the runtime (see :func:`redact_evidence` and
:func:`redact_identities`).
"""

from __future__ import annotations

from datetime import date, time
from typing import Final, Literal, Self

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


#: The key under which the runtime records **its own conclusion** about what
#: happened on the provider's side, for a side-effecting run whose outcome it
#: could not establish.
#:
#: It is a separate field from ``error_code`` because the two answer different
#: questions and must never be read as one:
#:
#: * ``error_code`` says **why the gateway returned what it returned** -- an
#:   authorization refusal, a schema rejection, an unresolved provider answer.
#:   ``APPROVAL_STALE`` is such a code, and it is an authorization fact: the
#:   gateway consumes an approval immediately *before* dispatch, so that refusal
#:   provably stopped the invocation before the provider was reached.
#: * this key says **what the runtime concluded about the external world**. The
#:   only value it ever takes is that the world is unestablished.
#:
#: Deriving the second from the first is the mistake this replaces: it made
#: "this invocation was not permitted" indistinguishable from "a side effect may
#: have occurred", and would let a policy refusal be reported as an undecidable
#: external outcome. Living here, beside the record shape it annotates, it is
#: readable by the evidence predicates without importing the runtime that writes
#: it.
EXTERNAL_OUTCOME: Final = "external_outcome"
EXTERNAL_OUTCOME_UNKNOWN: Final = "UNKNOWN"


class RuntimeEvidenceEntry(BaseModel):
    """Redacted public projection of one persisted tool-run record.

    The durable record kept for resume and verification is far richer: it also
    holds the tool arguments, the provider payload, and the verification
    evidence. None of that crosses this boundary. Only the run's identity
    (which step, which tool, which request, which sealed plan) and its verdict
    are published, so a client can follow *what ran, under which approved plan,
    and how it went* without receiving the arguments it passed, the provider's
    response, or the attendee addresses it carried. ``message`` is deliberately
    absent: an adapter can surface raw provider text in it, and ``error_code``
    is a stable application string.

    ``ir_fingerprint`` is the one IR field that crosses, because a verdict is
    only auditable if it names the plan that produced it. It is a content hash
    over the sealed program, so it discloses no argument, recipient, or provider
    payload, and it lets a reader prove that two runs of the same task executed
    the same plan rather than merely asserting it. ``ir_program_id`` stays
    internal: it identifies a program *instance*, which is strictly less
    auditable than its content, and the fingerprint already pins the content.

    ``external_outcome`` is published because it changes how a reader must
    interpret the run: it says the external result is unestablished, so a
    failure here is not evidence that nothing happened. It is absent on every
    ordinary run rather than defaulted, so its presence is itself the signal.
    """

    model_config = ConfigDict(extra="forbid")

    step_order: int
    tool: str
    outcome: str
    ok: bool
    verified: bool
    error_code: str | None = None
    request_id: str | None = None
    ir_fingerprint: str | None = None
    external_outcome: Literal["UNKNOWN"] | None = None


class RuntimeIdentity(BaseModel):
    """Redacted public projection of one resolved attendee identity.

    The local part of the address is masked: resolving who an attendee is, and
    proving it, stays inside the runtime and the identity resolver. The status
    and confidence are published because they are what makes a step auditable.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    status: str
    confidence: float
    email: str | None = None


def _mask_email(value: object) -> str | None:
    """``ahmed@example.com`` -> ``a***@example.com``; anything else -> None."""
    if not isinstance(value, str):
        return None
    local, separator, domain = value.strip().partition("@")
    if not separator or not local or not domain:
        return None
    return f"{local[:1]}***@{domain}"


def redact_evidence(records: list) -> list[RuntimeEvidenceEntry]:
    """Project persisted tool-run records onto the public, redacted shape.

    Every field is re-read defensively: a record this projection cannot make
    sense of is dropped rather than passed through, so a schema drift can never
    widen what the public API exposes.
    """
    published: list[RuntimeEvidenceEntry] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        step_order = record.get("step_order")
        tool = record.get("tool")
        outcome = record.get("outcome")
        if not isinstance(step_order, int) or not isinstance(tool, str):
            continue
        if not isinstance(outcome, str):
            continue
        error_code = record.get("error_code")
        request_id = record.get("request_id")
        ir_fingerprint = record.get("ir_fingerprint")
        published.append(
            RuntimeEvidenceEntry(
                step_order=step_order,
                tool=tool,
                outcome=outcome,
                ok=bool(record.get("ok")),
                verified=bool(record.get("verified")),
                error_code=error_code if isinstance(error_code, str) else None,
                request_id=request_id if isinstance(request_id, str) else None,
                ir_fingerprint=(
                    ir_fingerprint if isinstance(ir_fingerprint, str) else None
                ),
            )
        )
    return published


def redact_identities(resolved: list) -> list[RuntimeIdentity]:
    """Project resolved attendee identities onto the public, masked shape."""
    published: list[RuntimeIdentity] = []
    for item in resolved:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        status = item.get("status")
        if not isinstance(name, str) or not isinstance(status, str):
            continue
        try:
            confidence = float(item.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        published.append(
            RuntimeIdentity(
                name=name,
                status=status,
                confidence=confidence,
                email=_mask_email(item.get("email")),
            )
        )
    return published


class RuntimeTaskResponse(BaseModel):
    task_id: str
    task_state: str
    goal: str
    timezone: str | None = None
    scenario: UnderstandingOutput | None = None
    identities: list[RuntimeIdentity] = Field(default_factory=list)
    evidence: list[RuntimeEvidenceEntry] = Field(default_factory=list)
    cursor: int = 0
    steps_used: int = 0
    next_action: str
    approval_ids: list[str] = Field(default_factory=list)
    last_error: str | None = None


__all__ = [
    "GoalSubmission",
    "MeetingWindow",
    "RuntimeEvidenceEntry",
    "RuntimeIdentity",
    "RuntimeTaskResponse",
    "SubmitResponse",
    "TriggerResponse",
    "UnderstandingOutput",
    "redact_evidence",
    "redact_identities",
]