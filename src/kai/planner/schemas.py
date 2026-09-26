"""Structured plan schema + deterministic planner domain validation.

The model is asked for a *structured* JSON object, not free text. Two gates
stand between the model's raw output and a persisted plan:

1. Pydantic shape validation — the object must actually be a
   :class:`PlanProposal`. Schemas are ``extra="forbid"`` so the model cannot
   slip permission/tool/risk fields into the structure.
2. :func:`domain_errors` — exact contract fidelity (goal + success criteria
   reproduced verbatim), step budget/ordering/IDs, and no hidden conflation of
   unresolved questions into assumptions.

Branching is the one structural addition, and it is checked by the same two
gates: a ``PlanBranch`` is data in the closed schema, and whether it can become
control flow is decided by the IR itself rather than by a second set of rules
here (spec §8.9).

Neither gate trusts the model; a plan that fails either is never persisted.
"""

from __future__ import annotations

from collections.abc import Set as AbstractSet
from datetime import datetime
from typing import Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from kai.ir.build import branch_errors, condition_from_plan
from kai.ir.errors import IrValidationError
from kai.ir.nodes import ConditionOperator, FactKind, PredicateValue
from kai.tasks.models import Task

_TEXT_LIMIT = 1000
_MAX_CRITERIA = 50
_MAX_STEPS = 1000
_MAX_NOTES = 50
_MAX_BRANCHES = 50


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


class PlanCondition(BaseModel):
    """One comparison over facts the runtime already has durable.

    Deliberately the IR's own vocabulary (spec §4.4) rather than a second, looser
    one: ``kind``, ``operator``, and ``value`` are the same closed sets a
    ``ConditionNode`` accepts, and ``negate`` is the same flat flag. There is no
    expression, no arithmetic, and no way to name a fact the runtime does not
    already record — so declaring a branch cannot acquire a capability, a tool,
    or a permission.

    ``kind="observation"`` reads one recorded outcome of one earlier step, named
    by ``step_order`` and ``field``; every other kind reads a single fact about
    the run itself and must not name a step or a field. The IR's own
    ``ConditionNode`` validation is what enforces both, and it is what
    :func:`domain_errors` defers to, so there is no weaker Planner-side check to
    drift out of step with it.
    """

    model_config = ConfigDict(extra="forbid")

    kind: FactKind
    operator: ConditionOperator
    value: PredicateValue
    negate: bool = False
    step_order: int | None = Field(default=None, ge=1)
    field: str | None = Field(default=None, max_length=_TEXT_LIMIT)


class PlanBranch(BaseModel):
    """One two-way fork, declared as the step orders it separates.

    ``after_step`` is the step the fork follows, and ``then_step``/``else_step``
    are the two steps its two outcomes lead to — all three are orders from the
    plan's own ``steps``, so a branch is a statement about the plan the planner
    already wrote and not a second structure to keep in sync with it. Exactly one
    of the two targets must be the step immediately after ``after_step``: that is
    what partitions the steps between the two paths, so no planned step is left
    sitting on no path at all.

    There is no ``else``/``end``/``goto``/``loop`` vocabulary, and a plan cannot
    declare a cycle: a plan is read as a chain that is forked, never as a graph
    the planner draws freely. Both the shape and the cycle are then re-checked by
    the IR graph validator, so a declaration the Planner layer accepts still has
    to survive validation as a program.
    """

    model_config = ConfigDict(extra="forbid")

    after_step: int = Field(ge=1)
    condition: PlanCondition
    then_step: int = Field(ge=1)
    else_step: int = Field(ge=1)


class PlanProposal(BaseModel):
    """The complete validated plan proposal (KAI v0.5).

    ``goal`` and ``success_criteria`` are contract fields reproduced verbatim;
    there is no representation for permissions, allowed tools, risk policy, or
    model configuration — by construction the plan cannot change them.

    ``branches`` is the one optional structural field: absent (or empty) means
    the plan runs straight through its steps in order, exactly as before, so
    declaring a branch is a real choice the planner has to make rather than a
    default shape.
    """

    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=1, max_length=_TEXT_LIMIT)
    success_criteria: list[str] = Field(
        min_length=1, max_length=_MAX_CRITERIA
    )
    steps: list[PlanStep] = Field(min_length=1, max_length=_MAX_STEPS)
    branches: list[PlanBranch] = Field(default_factory=list, max_length=_MAX_BRANCHES)
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
        # Canonical order by source step, so two plans that declare the same
        # forks in a different order are the same plan and hash the same.
        self.branches = sorted(self.branches, key=lambda branch: branch.after_step)
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
        "branches": [
            {
                "after_step": branch.after_step,
                "condition": branch.condition.model_dump(mode="json"),
                "then_step": branch.then_step,
                "else_step": branch.else_step,
            }
            for branch in plan.branches
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

    # A branch is a declaration about the plan's own steps, so it is checked
    # against those steps here — at the point the plan is accepted, not later
    # when the runtime is already mid-run. Both halves ask the IR: the topology
    # rules in one place, and the condition's representability by compiling it
    # into the same ``ConditionNode`` a program would carry. Refusing here and
    # refusing again at build time are the same rule, not two layers.
    errors.extend(
        branch_errors(step_orders=orders, branches=plan.branches)
    )
    for branch in plan.branches:
        try:
            condition_from_plan(branch.condition)
        except (IrValidationError, ValidationError) as exc:
            errors.append(f"branch after step {branch.after_step}: {exc}")

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