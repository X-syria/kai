"""The KAI-IR v0 primitives this slice executes, as validated data.

Every model is ``extra="forbid"`` (spec §2.2: the contract is closed — unknown
fields are rejected, never ignored) and none of them can carry executable code,
a prompt, or a credential (spec §2.1: data, not instructions).

Twelve of the fourteen primitives are here. The two that are not —
``MemoryOpNode`` (memory is context today, not a planned operation) and
``ApprovalNode`` (the Tool Gateway already owns approval, and a second node
describing it would be a second source of permission truth) — are deliberately
deferred to a later slice rather than stubbed.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
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
_MAX_NODE_ID = 64
_MAX_PREDICATE_VALUES = 50

#: An action list is bounded by steps times the most tool calls one step can
#: issue, not by steps alone. Bounding a list of *actions* by the step budget
#: would be a category error that happens to hold only while every capability
#: issues at most one call per step — and the moment one does not, recording what
#: an execution did would start raising on runs that are otherwise fine. The
#: multiplier is today's worst case: ``calendar.read_slots`` issues one intent per
#: candidate day, capped at four.
_MAX_ACTIONS = _MAX_STEPS * 4


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

    **One action, one observation.** The unit is the *action*, not the step: a
    step that issues several tool calls produces one observation per call, all
    carrying the same ``step_order``. A condition therefore cannot address an
    individual call — it reads the step, and the step's outcome is the *last*
    observation recorded for it. That rule is a contract on the Runtime, which
    stops a step at its first non-successful intent; it is stated in full in
    ``kai.ir.control`` and in spec §4.4.1 and §7.1, and a change to the Runtime's
    per-step stop policy must revise it explicitly.

    ``error_code`` is ``str | None`` and ``None`` is the normal, meaningful
    value: it means this action recorded no error code. ``None`` is a fact about
    the value, distinct from a step having no observation at all — see
    ``NULLABLE_OBSERVATION_FACT_FIELDS`` for how a condition may test it.
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
    """How the program ended, and which observations decided it.

    Populated by the Runtime when a run actually terminates, and derived from
    the program's own observations rather than stored — so it is a record of what
    happened, never an input to anything. It is outside the fingerprint (spec
    §8.11): a program that failed hashes exactly as the program that has not run.
    """

    result: str = Field(min_length=1)
    reason: str = ""
    final_observation_ids: list[str] = Field(default_factory=list, max_length=_MAX_ACTIONS)

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


class FactKind(StrEnum):
    """The only sources a ``ConditionNode`` may read (spec §4.4).

    A closed set of already-durable runtime facts. There is no "read an
    environment variable", no "call a tool", and no "ask the model": a condition
    is a comparison over what the runtime already knows, so evaluating one can
    never acquire a new capability or take an action.
    """

    OBSERVATION = "observation"
    CURSOR = "cursor"
    STEPS_USED = "steps_used"
    TASK_STATE = "task_state"


class ConditionOperator(StrEnum):
    """The complete comparison vocabulary (spec §4.4).

    No arithmetic, no boolean algebra, no function calls. A branch is a
    comparison, and that is the whole of it.
    """

    EQ = "eq"
    NEQ = "neq"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    IN = "in"
    NOT_IN = "not_in"


#: Comparison operators that are meaningless on an ordered domain, kept as data
#: so the compatibility table and the validator cannot disagree.
_ORDERED_OPERATORS: frozenset[ConditionOperator] = frozenset(
    {ConditionOperator.GT, ConditionOperator.GTE, ConditionOperator.LT, ConditionOperator.LTE}
)
_SET_OPERATORS: frozenset[ConditionOperator] = frozenset(
    {ConditionOperator.IN, ConditionOperator.NOT_IN}
)

#: The only operators a null operand may be used with. ``eq``/``neq`` ask whether
#: the fact holds a value; ordering or enumerating "nothing" has no meaning, so
#: those combinations are refused where the condition is written.
_NULL_COMPARISON_OPERATORS: frozenset[ConditionOperator] = frozenset(
    {ConditionOperator.EQ, ConditionOperator.NEQ}
)

#: The Python type each observable field actually has. The condition's operand
#: must match it exactly, which is what lets a "verified == 'yes'" condition be
#: refused at build time instead of silently never matching at run time.
OBSERVATION_FACT_TYPES: dict[str, type] = {
    "outcome": str,
    "ok": bool,
    "verified": bool,
    "error_code": str,
}

#: Fields whose fact may legitimately hold *no* value, so a ``None`` operand is
#: a question about the fact rather than a type error.
#:
#: ``error_code`` is the only one: an action that recorded no error code has a
#: null ``error_code``, and saying so is a real, decidable fact about a
#: successful action. The distinction it keeps is between a *null value* and a
#: *missing observation* — reading a step with no observation at all is still
#: refused outright, because that is an absent fact rather than an empty one.
#:
#: A field being listed here says nothing about its type when it does hold a
#: value: ``error_code`` is still ``str`` (see ``OBSERVATION_FACT_TYPES``).
NULLABLE_OBSERVATION_FACT_FIELDS: frozenset[str] = frozenset({"error_code"})

#: Fields an ``OBSERVATION`` condition may read. Not a path expression: an
#: allow-list of scalars, so a condition cannot reach into a provider payload.
FACT_TYPE_BY_KIND: dict[FactKind, type] = {
    FactKind.CURSOR: int,
    FactKind.STEPS_USED: int,
    FactKind.TASK_STATE: str,
}


class ConditionSubject(_IrNode):
    """One observable fact, named exactly, with no way to compute a new one."""

    kind: FactKind
    step_order: int | None = Field(default=None, ge=1)
    field: str | None = Field(default=None, max_length=_MAX_TEXT)

    @model_validator(mode="after")
    def _fields_match_kind(self) -> Self:
        if self.kind is FactKind.OBSERVATION:
            if self.step_order is None or self.field is None:
                raise ValueError(
                    "an observation condition needs both step_order and field"
                )
            if self.field not in OBSERVATION_FACT_TYPES:
                raise ValueError(
                    f"observation field must be one of "
                    f"{sorted(OBSERVATION_FACT_TYPES)}"
                )
        elif self.step_order is not None or self.field is not None:
            raise ValueError(
                f"a {self.kind.value} condition reads a single fact and must not "
                f"name a step or a field"
            )
        return self

    def value_type(self) -> type:
        """The type of the fact this subject names."""
        if self.kind is FactKind.OBSERVATION:
            assert self.field is not None  # guaranteed by _fields_match_kind
            return OBSERVATION_FACT_TYPES[self.field]
        return FACT_TYPE_BY_KIND[self.kind]

    def allows_null(self) -> bool:
        """Whether this subject's fact may legitimately hold no value at all.

        Only an observation field can, and only the ones
        ``NULLABLE_OBSERVATION_FACT_FIELDS`` lists. The run counters and the task
        state always have a value, so ``None`` against them stays a type error.
        """
        return (
            self.kind is FactKind.OBSERVATION
            and self.field in NULLABLE_OBSERVATION_FACT_FIELDS
        )


#: The operand shape. Single-typed lists (not mixed) so a membership test has one
#: unambiguous meaning; the value's type is then checked against the fact's.
#: ``None`` is here only so a nullable fact can be tested for having no value —
#: ``ConditionNode`` refuses it everywhere else.
PredicateValue = bool | int | str | list[bool] | list[int] | list[str] | None


class ConditionNode(_IrNode):
    """A deterministic predicate over runtime facts, used to choose an edge.

    Conditions are *data*, never code: the operator and the operands are checked
    against each other when the program is built, and the evaluation itself is a
    single comparison performed by ``kai.ir.control``. A condition can therefore
    never become a place to smuggle in behaviour (spec §2.1).

    ``negate`` exists instead of a ``not`` operator so the canonical form keeps a
    predicate in one flat object: a condition and its complement differ by one
    boolean rather than by a nested subtree. That is what lets the graph
    validator prove a branch is exhaustive.

    A ``value`` of ``None`` is accepted only against a nullable observation field
    (``NULLABLE_OBSERVATION_FACT_FIELDS``) and only with ``eq`` or ``neq``, so
    "this step recorded no error code" is expressible without a second way of
    spelling absence. It is never inferred: a field the program does not name is
    not silently null, and a step with no observation at all is still refused at
    evaluation time.
    """

    condition_id: str = Field(min_length=1, max_length=_MAX_NODE_ID)
    subject: ConditionSubject
    operator: ConditionOperator
    value: PredicateValue
    negate: bool = False

    @field_validator("condition_id")
    @classmethod
    def _id_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("condition_id must not be blank")
        return stripped

    @model_validator(mode="after")
    def _operand_matches_fact(self) -> Self:
        expected = self.subject.value_type()
        operand = self.value

        # A null operand asks whether the fact holds a value, so it is answered
        # before any type check: ``type(None)`` matches nothing in the table, and
        # the question is only meaningful on a field that can be empty.
        if operand is None:
            if not self.subject.allows_null():
                raise ValueError(
                    f"a {self.subject.kind.value} fact always has a value, so it "
                    f"cannot be compared with null"
                )
            if self.operator not in _NULL_COMPARISON_OPERATORS:
                raise ValueError(
                    f"{self.operator.value} is undefined for a null operand; use eq or "
                    f"neq to ask whether {self.subject.field!r} holds a value"
                )
            return self

        if self.operator in _SET_OPERATORS:
            if not isinstance(operand, list):
                raise ValueError(f"{self.operator.value} needs a list operand")
            if not operand:
                raise ValueError(f"{self.operator.value} needs a non-empty list operand")
            if len(operand) > _MAX_PREDICATE_VALUES:
                raise ValueError("a membership operand carries too many values")
            for item in operand:
                if type(item) is not expected:
                    raise ValueError(
                        f"{self.operator.value} operand must hold {expected.__name__} "
                        f"values to match {self.subject.kind.value}"
                    )
            return self

        if isinstance(operand, list):
            raise ValueError(f"{self.operator.value} needs a single operand")
        if type(operand) is not expected:
            raise ValueError(
                f"{self.operator.value} operand must be {expected.__name__} to match "
                f"a {self.subject.kind.value} fact"
            )
        if self.operator in _ORDERED_OPERATORS and expected is str:
            raise ValueError(f"{self.operator.value} is undefined for a text fact")
        return self

    def is_negation_of(self, other: ConditionNode) -> bool:
        """True when exactly one of these two is true for any given fact value.

        Structural, not semantic: same fact, same comparison, opposite polarity.
        It is enough to prove a two-way branch is exhaustive, and it is
        conservative — a branch can be valid for reasons this does not detect,
        but is only ever *proven* invalid when it really is.
        """
        return (
            self.subject == other.subject
            and self.operator == other.operator
            and self.value == other.value
            and self.negate is not other.negate
        )


class ControlNodeKind(StrEnum):
    ACTION = "action"
    CONDITION = "condition"
    RESULT = "result"


class ControlNode(_IrNode):
    """A vertex of the control-flow graph.

    These are the graph's *static* vertices, deliberately not the execution
    artifacts they lead to. An ``action`` vertex names the plan step that runs
    there; the ``ActionNode`` with its concrete arguments is still built at
    execution time, because its arguments depend on what earlier observations
    established. Referring to a step instead of copying one keeps runtime-filled
    data out of the hashed control structure — the fingerprint stays a function
    of the declared flow and nothing that happened while running it.
    """

    node_id: str = Field(min_length=1, max_length=_MAX_NODE_ID)
    kind: ControlNodeKind
    step_order: int | None = Field(default=None, ge=1)
    condition: ConditionNode | None = None
    result: str | None = None

    @field_validator("node_id")
    @classmethod
    def _id_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("node_id must not be blank")
        return stripped

    @model_validator(mode="after")
    def _carries_exactly_its_own_payload(self) -> Self:
        if self.kind is ControlNodeKind.ACTION:
            if self.step_order is None:
                raise ValueError("an action node must name the step it executes")
            unexpected = self.condition is not None or self.result is not None
        elif self.kind is ControlNodeKind.CONDITION:
            if self.condition is None:
                raise ValueError("a condition node must carry its condition")
            unexpected = self.step_order is not None or self.result is not None
        else:
            if self.result is None:
                raise ValueError("a result node must declare the outcome it ends on")
            if self.result not in ALL_STATES or self.result not in RESULT_STATES:
                raise ValueError(
                    f"result must be one of the reportable task states "
                    f"{sorted(RESULT_STATES)}"
                )
            unexpected = self.step_order is not None or self.condition is not None
        if unexpected:
            raise ValueError(f"a {self.kind.value} node must not carry another kind's fields")
        return self


class ControlEdge(_IrNode):
    """One way from one node to the next, with the guard that permits it.

    An edge with no ``when`` is unconditional, and a node may have at most one of
    those — two would be a choice the program never made. Guarded edges are
    selected purely by evaluating their condition against durable facts, so which
    edge is taken is a function of what already happened, never of a fresh
    decision made mid-run.
    """

    from_node: str = Field(min_length=1, max_length=_MAX_NODE_ID)
    to_node: str = Field(min_length=1, max_length=_MAX_NODE_ID)
    when: ConditionNode | None = None

    @field_validator("from_node", "to_node")
    @classmethod
    def _endpoints_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("edge endpoints must not be blank")
        return stripped

    @model_validator(mode="after")
    def _no_self_loop(self) -> Self:
        # A self-loop is a one-node cycle, and a cycle is refused outright.
        if self.from_node == self.to_node:
            raise ValueError("a control edge must not target its own source")
        return self


__all__ = [
    "FACT_TYPE_BY_KIND",
    "IR_VERSION",
    "NULLABLE_OBSERVATION_FACT_FIELDS",
    "OBSERVATION_FACT_TYPES",
    "PLAN_STATUSES",
    "RESULT_STATES",
    "ActionNode",
    "ConditionNode",
    "ConditionOperator",
    "ConditionSubject",
    "ControlEdge",
    "ControlNode",
    "ControlNodeKind",
    "ErrorNode",
    "FactKind",
    "IntentNode",
    "ObservationNode",
    "PermissionNode",
    "PredicateValue",
    "PlanNode",
    "PolicyConstraintsNode",
    "StateSnapshotNode",
    "StepNode",
]
