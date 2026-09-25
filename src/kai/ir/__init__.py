"""KAI-IR: the Intermediate Representation KAI's reasoning layer produces.

This package owns the *content* of a program — the intent, the plan, the
constraints, the declared permissions, the observations, the result — and
nothing about how it is carried out. The Agent Runtime still owns claims,
lifecycle, durability, and recovery; the Tool Gateway still owns policy, content,
approval, account, and verification. An IR program is the audited, deterministic,
validated description of what the runtime intends to do, and the two authorities
are unchanged by it.

Spec: ``docs/kai-ir-spec-v0.md``.

Scope of this first slice: ten of the fourteen primitives, translation of
persisted planning output, and the Gateway boundary. ``ConditionNode``,
``ControlEdge``, ``MemoryOpNode``, and ``ApprovalNode`` are deliberately absent —
the first two would have to duplicate the task state machine's control, and the
last two would duplicate the Gateway's own authority over memory and approval.
"""

from kai.ir.adapter import (
    action_for,
    attach,
    error_from_observation,
    evidence_from_observation,
    intent_from_action,
    observation_from_evidence,
    observation_from_result,
    result_for,
    tool_name_for,
)
from kai.ir.build import (
    action_id_for,
    allowed_capabilities_from_task,
    program_from_plan,
    program_id_for,
    tools_by_capability,
)
from kai.ir.errors import ErrorClass, ErrorResponse, IrValidationError
from kai.ir.nodes import (
    IR_VERSION,
    ActionNode,
    ErrorNode,
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
    "ActionNode",
    "ErrorClass",
    "ErrorNode",
    "ErrorResponse",
    "IntentNode",
    "IrValidationError",
    "KaiProgram",
    "ObservationNode",
    "PermissionNode",
    "PlanNode",
    "PolicyConstraintsNode",
    "StateSnapshotNode",
    "StepNode",
    "action_for",
    "action_id_for",
    "allowed_capabilities_from_task",
    "attach",
    "canonical_fingerprint",
    "canonical_json",
    "error_from_observation",
    "evidence_from_observation",
    "intent_from_action",
    "observation_from_evidence",
    "observation_from_result",
    "program_from_plan",
    "program_id_for",
    "require_valid",
    "result_for",
    "tool_name_for",
    "tools_by_capability",
]
