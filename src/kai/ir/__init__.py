"""KAI-IR: the Intermediate Representation KAI's reasoning layer produces.

This package owns the *content* of a program — the intent, the plan, the declared
control flow, the constraints, the declared permissions, the observations, the
result — and nothing about how it is carried out. The Agent Runtime still owns
claims, lifecycle, durability, and recovery; the Tool Gateway still owns policy,
content, approval, account, and verification. An IR program is the audited,
deterministic, validated description of what the runtime intends to do, and the
two authorities are unchanged by it.

A program may say which of its steps is eligible to run next given what has
already been observed, and the runtime may ask it. It may not decide anything:
branching is a comparison over facts the runtime already has durable, and the
first thing a branch can do is name a step for the runtime to run through the
same Gateway path as any other.

Spec: ``docs/kai-ir-spec-v0.md``.

Scope of this slice: twelve of the fourteen primitives, translation of persisted
planning output — including the branches a plan declares — deterministic
control-flow traversal, and the Gateway boundary.
``MemoryOpNode`` and ``ApprovalNode`` are deliberately absent — the first would
duplicate a memory subsystem, and the second would duplicate the Gateway's own
authority over approval.
"""

from kai.ir.adapter import (
    action_for,
    attach,
    error_from_observation,
    evidence_from_observation,
    intent_from_action,
    observation_from_evidence,
    observation_from_result,
    record_error,
    record_result,
    result_for,
    tool_name_for,
)
from kai.ir.build import (
    action_id_for,
    allowed_capabilities_from_task,
    branch_errors,
    condition_from_plan,
    graph_from_plan,
    program_from_plan,
    program_id_for,
    tools_by_capability,
)
from kai.ir.control import (
    ControlFacts,
    evaluate,
    linear_graph,
    next_node,
    next_step,
)
from kai.ir.errors import (
    ErrorClass,
    ErrorResponse,
    IrConditionError,
    IrControlFlowError,
    IrValidationError,
)
from kai.ir.nodes import (
    IR_VERSION,
    NULLABLE_OBSERVATION_FACT_FIELDS,
    OBSERVATION_FACT_TYPES,
    ActionNode,
    ConditionNode,
    ConditionOperator,
    ConditionSubject,
    ControlEdge,
    ControlNode,
    ControlNodeKind,
    ErrorNode,
    FactKind,
    IntentNode,
    ObservationNode,
    PermissionNode,
    PlanNode,
    PolicyConstraintsNode,
    StateSnapshotNode,
    StepNode,
)
from kai.ir.program import (
    KaiProgram,
    canonical_fingerprint,
    canonical_json,
    require_valid,
)

__all__ = [
    "IR_VERSION",
    "NULLABLE_OBSERVATION_FACT_FIELDS",
    "OBSERVATION_FACT_TYPES",
    "ActionNode",
    "ConditionNode",
    "ConditionOperator",
    "ConditionSubject",
    "ControlEdge",
    "ControlFacts",
    "ControlNode",
    "ControlNodeKind",
    "ErrorClass",
    "ErrorNode",
    "ErrorResponse",
    "FactKind",
    "IntentNode",
    "IrConditionError",
    "IrControlFlowError",
    "IrValidationError",
    "KaiProgram",
    "ObservationNode",
    "PermissionNode",
    "PlanNode",
    "PolicyConstraintsNode",
    "PredicateValue",
    "StateSnapshotNode",
    "StepNode",
    "action_for",
    "action_id_for",
    "allowed_capabilities_from_task",
    "attach",
    "branch_errors",
    "canonical_fingerprint",
    "canonical_json",
    "condition_from_plan",
    "error_from_observation",
    "evaluate",
    "evidence_from_observation",
    "graph_from_plan",
    "intent_from_action",
    "linear_graph",
    "next_node",
    "next_step",
    "observation_from_evidence",
    "observation_from_result",
    "program_from_plan",
    "program_id_for",
    "record_error",
    "record_result",
    "require_valid",
    "result_for",
    "tool_name_for",
    "tools_by_capability",
]
