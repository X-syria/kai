"""The KAI-IR v0 primitives this slice executes, as validated data.

Every model is ``extra="forbid"`` (spec §2.2: the contract is closed — unknown
fields are rejected, never ignored) and none of them can carry executable code,
a prompt, or a credential (spec §2.1: data, not instructions).

Ten of the fourteen primitives are here. The four that are not — ``ConditionNode``
and ``ControlEdge`` (branching, which the task state machine must stay
authoritative for), ``MemoryOpNode`` (memory is context today, not a planned
operation), and ``ApprovalNode`` (the Tool Gateway already owns approval, and a
second node describing it would be a second source of permission truth) — are
deliberately deferred to a later slice rather than stubbed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from kai.ir.errors import ErrorClass, ErrorResponse
from kai.tasks.state import ALL_STATES
from kai.tools.result import ToolOutcome
from kai.tools.risk import RiskLevel

#: The only IR version this build speaks. A node is valid only for the version
#: the spec declares; anything else is refused rather than guessed at (§10).
IR_VERSION: str = "kai-ir/0"

#: Plan status vocabulary carried by ``PlanNode.status`` (spec §4.2). It is the
#: spec's own four-value set; the ``TaskPlan`` row uses a two-value subset.
PLAN_STATUSES: frozenset[str] = frozenset(
    {"DRAFT", "ACCEPTED", "SUPERSEDED", "EXECUTED"}
)

#: The program outcomes a ``ResultNode`` may report. These are the task
#: machine's terminal vocabulary, not a second one.
RESULT_STATES: frozenset[str] = frozenset(
    {"COMPLETED", "FAILED", "REQUIRES_ATTENTION", "ESCALATE", "RETRY"}
)

_MAX_TEXT = 1000
_MAX_CRITERIA = 50
_MAX_STEPS = 1000
_MAX_ARGS = 200


class _IrNode(BaseModel):
    """Base for every primitive: closed, bounded, never executable."""

    model_config = ConfigDict(extra="forbid")


class IntentNode(_IrNode):
    """Entry point: what the user asked for, and what "done" means.

    The goal and the criteria are the task contract reproduced verbatim — the IR
    never restates or paraphrases them, so it cannot drift from what the user
    agreed to.

    The scenario the model read out of the goal is deliberately *not* copied in
    here. It is the runtime's execution context and it is already durable in the
    execution row; a second copy inside the program would be a second version to
    fall out of date, and the program's identity is meant to be its intent, not
    the runtime's current read of it.
    """

    goal: str = Field(min_length=1, max_length=_MAX_TEXT)
    success_criteria: list[str] = Field(min_length=1, max_length=_MAX_CRITERIA)
    entry: bool = True

    @field_validator("success_criteria")
    @classmethod
    def _criteria_not_blank(cls, value: list[str]) -> list[str]:
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise ValueError("success_criteria entries must be non-blank strings")
        return [" ".join(item.split()) for item in value]

    @model_validator(mode="after")
    def _only_entry(self) -> Self:
        if not self.entry:
            raise ValueError("the IntentNode is the only entry point")
        return self


class StepNode(_IrNode):
    """One ordered unit of work — the unit the runtime cursor counts.

    ``capability`` is the spec's name for what the planner currently calls a
    step's ``action``: an abstract capability, never a tool name. Mapping a
    capability to a concrete tool stays the Gateway's concern (spec §2.4).
    """

    step_id: str = Field(min_length=1, max_length=64)
    order: int = Field(ge=1)
    capability: str = Field(min_length=1, max_length=_MAX_TEXT)
    purpose: str = Field(min_length=1, max_length=_MAX_TEXT)
    inputs: list[str] = Field(default_factory=list, max_length=_MAX_STEPS)
    expected_output: str | None = Field(default=None, max_length=_MAX_TEXT)
    verification_hint: str | None = Field(default=None, max_length=_MAX_TEXT)

    @field_validator("step_id", "capability", "purpose")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("step_id, capability, and purpose must not be blank")
        return stripped


class PlanNode(_IrNode):
    """The plan this program instantiates; execution binds to this."""

    plan_version: int = Field(ge=1)
    status: str = Field(min_length=1)
    steps: list[StepNode] = Field(min_length=1, max_length=_MAX_STEPS)

    @field_validator("status")
    @classmethod
    def _known_status(cls, value: str) -> str:
        if value not in PLAN_STATUSES:
            raise ValueError(f"plan status must be one of {sorted(PLAN_STATUSES)}")
        return value

    @model_validator(mode="after")
    def _sequential(self) -> Self:
        orders = [step.order for step in self.steps]
        if orders != list(range(1, len(self.steps) + 1)):
            raise ValueError("step orders must be sequential 1..n matching list position")
        ids = [step.step_id for step in self.steps]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate step_id values in the plan")
        return self


class PolicyConstraintsNode(_IrNode):
    """The program-wide rule envelope policy evaluates against.

    A declaration of what the program is *allowed to ask for*, assembled from
    the task contract. Declaring a constraint never grants anything and never
    widens the task: the Policy Engine re-evaluates the real task on every
    invocation (spec §2.5).
    """

    allowed_capabilities: list[str] = Field(min_length=1)
    max_risk: RiskLevel
    budget: int = Field(ge=1)
    deadline: datetime | None = None
    content_rules: list[str] = Field(default_factory=list, max_length=_MAX_CRITERIA)

    @field_validator("allowed_capabilities")
    @classmethod
    def _capabilities_not_blank(cls, value: list[str]) -> list[str]:
        cleaned = [" ".join(item.split()) for item in value]
        if any(not item for item in cleaned):
            raise ValueError("allowed_capabilities entries must be non-blank")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("duplicate allowed_capabilities entries")
        return cleaned


class PermissionNode(_IrNode):
    """A declared permission *requirement* for one capability.

    A requirement, never a grant: it says what an action would need, and only the
    Policy Engine and the Approval Manager can say whether it gets it. The
    ``max_risk`` here is the risk the tool declares in the catalog — carried so
    the boundary is machine-checkable, not trusted because the IR asked nicely.
    """

    requires_capability: str = Field(min_length=1, max_length=_MAX_TEXT)
    max_risk: RiskLevel
    requires_account: bool = False
    requires_approval: bool = False

    @field_validator("requires_capability")
    @classmethod
    def _capability_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("requires_capability must not be blank")
        return stripped


class StateSnapshotNode(_IrNode):
    """The bindable progress anchor: where the program is, and on which plan.

    This is the spec's explicit form of the existing plan-binding contract, so
    resume can only continue from a snapshot that still matches the plan it was
    bound to (spec §5).
    """

    plan_version: int = Field(ge=1)
    plan_hash: str = Field(min_length=64, max_length=64)
    cursor: int = Field(ge=0)
    steps_used: int = Field(ge=0)
    bound_at: datetime | None = None

    @field_validator("plan_hash")
    @classmethod
    def _hex_digest(cls, value: str) -> str:
        if any(char not in "0123456789abcdef" for char in value):
            raise ValueError("plan_hash must be a lowercase sha256 hex digest")
        return value


class ActionNode(_IrNode):
    """A capability invocation: the concrete thing the Gateway will execute.

    Built at execution time, not at plan time, because its arguments depend on
    what earlier observations established (the free slot, the verified attendee).
    ``action_id`` is deterministic in the task, plan version, step, and position
    (spec §5), so the same action re-issues the same id on every resume.
    """

    action_id: str = Field(min_length=1, max_length=128)
    capability: str = Field(min_length=1, max_length=_MAX_TEXT)
    arguments: dict[str, Any] = Field(default_factory=dict)
    risk_hint: RiskLevel | None = None

    @field_validator("action_id")
    @classmethod
    def _action_id_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("action_id must not be blank")
        return stripped

    @field_validator("arguments")
    @classmethod
    def _arguments_bounded(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(value) > _MAX_ARGS:
            raise ValueError("an action carries too many arguments")
        return value


class ObservationNode(_IrNode):
    """The durable, append-only record of one executed action.

    Holds the *internal* evidence — arguments, provider payload, verification
    evidence — because resume and verification need it. Nothing here is
    published directly; the public projection is a separate, redacted layer
    (spec §7).

    ``recorded_at`` is wall-clock on purpose and is why an observation is never
    part of the program's fingerprint: a deterministic plan must not hash
    differently because it ran twice.
    """

    action_id: str = Field(min_length=1, max_length=128)
    step_order: int = Field(ge=1)
    outcome: ToolOutcome
    ok: bool
    verified: bool
    error_code: str | None = None
    message: str = ""
    evidence: dict[str, Any] = Field(default_factory=dict)
    data: Any = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    request_id: str = ""
    recorded_at: datetime | None = None

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        # The Gateway guarantees this coherence; the IR re-asserts it so a
        # malformed observation can never be written to the durable log.
        if self.ok and not self.verified:
            raise ValueError("an observation cannot be ok without being verified")
        return self


class ErrorNode(_IrNode):
    """A structured, typed failure with its declared response (spec §6)."""

    error_code: str = Field(min_length=1, max_length=_MAX_TEXT)
    error_class: ErrorClass
    response: ErrorResponse

    @model_validator(mode="after")
    def _class_matches_response(self) -> Self:
        from kai.ir.errors import RESPONSE_BY_CLASS

        if RESPONSE_BY_CLASS[self.error_class] is not self.response:
            raise ValueError(
                f"error class {self.error_class.value} declares response "
                f"{RESPONSE_BY_CLASS[self.error_class].value}, not "
                f"{self.response.value}"
            )
        return self


class ResultNode(_IrNode):
    """How the program ended, and which observations decided it."""

    result: str = Field(min_length=1)
    reason: str = ""
    final_observation_ids: list[str] = Field(default_factory=list, max_length=_MAX_STEPS)

    @field_validator("result")
    @classmethod
    def _known_result(cls, value: str) -> str:
        # A program's outcome must be a state the task machine can actually be
        # in, so the IR can never invent a terminal state of its own.
        if value not in ALL_STATES or value not in RESULT_STATES:
            raise ValueError(
                f"result must be one of the reportable task states "
                f"{sorted(RESULT_STATES)}"
            )
        return value


__all__ = [
    "IR_VERSION",
    "PLAN_STATUSES",
    "RESULT_STATES",
    "ActionNode",
    "ErrorNode",
    "IntentNode",
    "ObservationNode",
    "PermissionNode",
    "PlanNode",
    "PolicyConstraintsNode",
    "StateSnapshotNode",
    "StepNode",
]
