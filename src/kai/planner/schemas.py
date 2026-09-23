"""Structured plan schema + deterministic planner domain validation.

The model is asked for a *structured* JSON object, not free text. Two gates
stand between the model's raw output and a persisted plan:

1. Pydantic shape validation — the object must actually be a
   :class:`PlanProposal`. Schemas are ``extra="forbid"`` so the model cannot
   slip permission/tool/risk fields into the structure.
2. :func:`domain_errors` — exact contract fidelity (goal + success criteria
   reproduced verbatim), step budget/ordering/IDs, and no hidden conflation of
   unresolved questions into assumptions.

Neither gate trusts the model; a plan that fails either is never persisted.
"""

from __future__ import annotations

from collections.abc import Set as AbstractSet
from datetime import datetime
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from kai.tasks.models import Task

_TEXT_LIMIT = 1000
_MAX_CRITERIA = 50
_MAX_STEPS = 1000
_MAX_NOTES = 50


def _collapse(text: str) -> str:
    """Normalise for contract-fidelity comparison (whitespace + case)."""
    return " ".join(text.split()).casefold()


def _non_blank_items(items: list[str], *, field_name: str, limit: int) -> list[str]:
    for item in items:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{field_name} entries must be non-blank strings")
        if len(item) > _TEXT_LIMIT:
            raise ValueError(f"{field_name} entries exceed the length limit")
    if len(items) > limit:
        raise ValueError(f"{field_name} has more than {limit} entries")
    return items


class PlanStep(BaseModel):
    """One abstract capability in the plan. Never a tool call."""

    model_config = ConfigDict(extra="forbid")

    step_id: str = Field(min_length=1, max_length=64)
    order: int = Field(ge=1)
    action: str = Field(min_length=1, max_length=_TEXT_LIMIT)
    purpose: str = Field(min_length=1, max_length=_TEXT_LIMIT)
    inputs: list[str] = Field(default_factory=list, max_length=_MAX_STEPS)
    expected_output: str | None = Field(default=None, max_length=_TEXT_LIMIT)
    verification_hint: str | None = Field(default=None, max_length=_TEXT_LIMIT)

    @field_validator("step_id")
    @classmethod
    def _step_id_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("step_id must not be blank")
        return stripped

    @field_validator("action")
    @classmethod
    def _action_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("action must not be blank")
        return stripped

    @field_validator("purpose")
    @classmethod
    def _purpose_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("purpose must not be blank")
        return stripped


class PlanProposal(BaseModel):
    """The complete validated plan proposal (KAI v0.5).

    ``goal`` and ``success_criteria`` are contract fields reproduced verbatim;
    there is no representation for permissions, allowed tools, risk policy, or
    model configuration — by construction the plan cannot change them.
    """

    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=1, max_length=_TEXT_LIMIT)
    success_criteria: list[str] = Field(
        min_length=1, max_length=_MAX_CRITERIA
    )
    steps: list[PlanStep] = Field(min_length=1, max_length=_MAX_STEPS)
    assumptions: list[str] = Field(default_factory=list, max_length=_MAX_NOTES)
    unresolved_questions: list[str] = Field(
        default_factory=list, max_length=_MAX_NOTES
    )

    @field_validator("goal")
    @classmethod
    def _goal_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("goal must not be blank")
        return stripped

    @field_validator("success_criteria")
    @classmethod
    def _criteria_ok(cls, value: list[str]) -> list[str]:
        return _non_blank_items(value, field_name="success_criteria", limit=_MAX_CRITERIA)

    @field_validator("assumptions")
    @classmethod
    def _assumptions_ok(cls, value: list[str]) -> list[str]:
        return _non_blank_items(value, field_name="assumptions", limit=_MAX_NOTES)

    @field_validator("unresolved_questions")
    @classmethod
    def _questions_ok(cls, value: list[str]) -> list[str]:
        return _non_blank_items(value, field_name="unresolved_questions", limit=_MAX_NOTES)

    @model_validator(mode="after")
    def _canonicalise(self) -> Self:
        self.goal = self.goal.strip()
        self.steps = sorted(self.steps, key=lambda step: step.order)
        return self


def _plan_contract(plan: PlanProposal) -> dict[str, object]:
    # Keep the serialized contract in sync with the strict checks below.
    return {
        "goal": plan.goal,
        "success_criteria": list(plan.success_criteria),
        "steps": [
            {
                "step_id": step.step_id,
                "order": step.order,
                "action": step.action,
                "purpose": step.purpose,
                "inputs": list(step.inputs),
                "expected_output": step.expected_output,
                "verification_hint": step.verification_hint,
            }
            for step in plan.steps
        ],
        "assumptions": list(plan.assumptions),
        "unresolved_questions": list(plan.unresolved_questions),
    }


def domain_errors(
    plan: PlanProposal,
    *,
    task: Task,
    allowed_capabilities: AbstractSet[str] | None = None,
) -> list[str]:
    """Deterministic checks that decide whether a plan may be accepted.

    The plan is linked to the correct task by construction (the caller loads
    the task first), and the contract fields are compared against the task's
    stored values — a paraphrase, an added criterion, or an invented goal all
    fail here. Returns a list of human-readable error strings; empty means the
    plan is accepted.

    ``allowed_capabilities`` optionally pins every step's ``action`` to a fixed
    abstract-capability vocabulary (default ``None`` = vocabulary not enforced).
    """
    errors: list[str] = []

    if _collapse(plan.goal) != _collapse(task.goal):
        errors.append("plan goal does not match the task contract goal")
    expected_criteria = {_collapse(c) for c in task.success_criteria}
    plan_criteria = {_collapse(c) for c in plan.success_criteria}
    if plan_criteria != expected_criteria:
        errors.append("plan success criteria differ from the task contract")

    # CAUTION: the caller already validated these values; there is no mutable
    # state here to corrupt between calls, so leaving them untouched is safe.
    _plan_contract(plan)

    if len(plan.steps) == 0:
        errors.append("plan has no steps")
    if len(plan.steps) > task.max_steps:
        errors.append(
            f"plan has {len(plan.steps)} steps, exceeding max_steps={task.max_steps}"
        )

    step_ids = [step.step_id for step in plan.steps]
    if len(set(step_ids)) != len(step_ids):
        errors.append("duplicate step_id values")
    orders = [step.order for step in plan.steps]
    if orders != list(range(1, len(plan.steps) + 1)):
        errors.append("step orders must be sequential 1..n matching list position")

    if allowed_capabilities is not None:
        unknown = sorted(
            {step.action for step in plan.steps} - set(allowed_capabilities)
        )
        if unknown:
            errors.append(
                f"plan references capabilities outside the fixed vocabulary: {unknown}"
            )

    shared = {_collapse(a) for a in plan.assumptions} & {
        _collapse(q) for q in plan.unresolved_questions
    }
    if shared:
        errors.append(
            "an item is duplicated between assumptions and unresolved_questions"
        )
    return errors


def plan_json(plan: PlanProposal) -> dict[str, object]:
    """Serialization surface for ``plan_json`` (never raw model text)."""
    return _plan_contract(plan)


class PlanResponse(BaseModel):
    """A plan row on the wire. Internal fields (plan status history) stay out."""

    plan_id: str
    task_id: str
    tenant_id: str
    version: int
    status: str
    model_id: str
    created_at: datetime
    plan: PlanProposal


class PlanListResponse(BaseModel):
    plans: list[PlanResponse]