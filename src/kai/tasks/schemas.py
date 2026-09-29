"""Pydantic wire schemas for the Task Manager.

Validation is two-layered: Pydantic enforces shape/types at the boundary, and
the small domain checks below (deadline awareness, contract cross-field rules)
enforce business meaning. Nothing here talks to the database or the machine;
state transitions are validated in :mod:`kai.tasks.service`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, Field, field_validator, model_validator

from kai.tasks.state import ALL_STATES

RiskPolicy = Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
VerificationMethod = Literal["automatic", "manual", "ai_review"]

#: The closed set of fulfillment semantics a Sealed Contract may name.
#:
#: This names *which* question is being asked, and it is part of the sealed
#: contract, so a resumed run re-derives the same evaluator from persisted state
#: rather than from anything mutable. It says nothing about the conditions inside
#: that question: those live in the typed ``SuccessCriteria``, which is the
#: authority the evaluator actually reads. ``success_criteria`` below is prose —
#: a description the planner reproduces verbatim — and is never interpreted.
#:
#: There are two members because two domains are implemented. Each one was added
#: as a deliberate amendment to the contract vocabulary, not as an accident of a
#: new task's tools — which is why this is an explicit identifier and never
#: derived from ``allowed_tools`` or from the goal text.
#:
#: ``calendar_meeting`` was closed on its own evidence, in
#: ``kai.runtime.fulfillment.evaluate_calendar_meeting``.
#: ``gmail_send`` is the second-domain experiment
#: (:func:`kai.runtime.fulfillment.evaluate_gmail_send`): a second sealed
#: question, sealed the same way, with its own semantics. Naming a second member
#: is a statement that the *binding* is the part that generalizes. It is not a
#: claim that fulfillment is a general framework, that any further domain is
#: supported, or that a profile can be derived rather than declared — the
#: runtime still seals ``calendar_meeting`` for every task it drives, and
#: ``gmail_send`` is reachable only where a contract already names it.
FulfillmentProfile = Literal["calendar_meeting", "gmail_send"]

_MAX_CRITERIA = 50
_MAX_TOOLS = 50
_TEXT_LIMIT = 1000


class TaskContractInput(BaseModel):
    """The KAI v0.5 Task Contract, validated before persistence."""

    goal: str = Field(min_length=1, max_length=_TEXT_LIMIT)
    success_criteria: list[str] = Field(
        min_length=1, max_length=_MAX_CRITERIA
    )
    fulfillment_profile: FulfillmentProfile
    allowed_tools: list[str] = Field(default_factory=list, max_length=_MAX_TOOLS)
    risk_policy: RiskPolicy = "LOW"
    deadline: datetime | None = None
    max_cost: float = Field(default=0.0, ge=0.0, le=1_000_000_000.0)
    max_steps: int = Field(default=20, ge=1, le=1000)
    verification_method: VerificationMethod = "automatic"
    fallback: str = Field(min_length=1, max_length=_TEXT_LIMIT)

    @field_validator("success_criteria")
    @classmethod
    def _criteria_not_blank(cls, value: list[str]) -> list[str]:
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise ValueError("success_criteria entries must be non-blank strings")
            if len(item) > _TEXT_LIMIT:
                raise ValueError("success_criteria entries exceed the length limit")
        return value

    @field_validator("allowed_tools")
    @classmethod
    def _tools_sanitized(cls, value: list[str]) -> list[str]:
        return [item.strip() for item in value if item.strip()]

    @field_validator("goal")
    @classmethod
    def _goal_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("goal must not be blank")
        return stripped

    @field_validator("fallback")
    @classmethod
    def _fallback_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("fallback must not be blank")
        return stripped

    @field_validator("deadline")
    @classmethod
    def _deadline_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError(
                "deadline must be timezone-aware (e.g. 2026-06-24T10:00:00Z)"
            )
        return value

    @model_validator(mode="after")
    def _high_risk_requires_guardrails(self) -> Self:
        if self.risk_policy in ("HIGH", "CRITICAL") and self.max_steps > 100:
            raise ValueError(
                "HIGH/CRITICAL risk tasks must keep max_steps at or below 100"
            )
        return self


class TaskCreateRequest(BaseModel):
    tenant_id: str
    user_id: str
    contract: TaskContractInput


class AssignRequest(BaseModel):
    tenant_id: str
    user_id: str


class StateTransitionRequest(BaseModel):
    target_state: str = Field(min_length=1, max_length=64)

    @field_validator("target_state")
    @classmethod
    def _known_state(cls, value: str) -> str:
        if value not in ALL_STATES:
            raise ValueError(f"unknown task state: {value!r}")
        return value


class TransitionFailure(BaseModel):
    current_state: str
    target_state: str
    error: str


class TaskResponse(BaseModel):
    """Task representation; deliberately flat and free of internal metadata.

    ``attempts``/``last_error`` live in the persistence layer and are omitted
    from wire responses so failure internals never leak to callers.
    """

    id: str
    tenant_id: str
    user_id: str
    state: str
    contract: TaskContractInput


class TaskListResponse(BaseModel):
    tasks: list[TaskResponse]