"""Translation: persisted planning output → a validated KAI-IR program.

This is the whole of the "reasoning produces IR" direction. It reads only what
already exists — the sealed task contract, the accepted plan row, the catalog
declaration behind each capability — and emits data. It never consults the
model, never executes a tool, and never invents a capability, a permission, or a
step the plan did not contain.

The program is derived, not stored: the same task, plan, and progress always
rebuild the same program with the same ``program_id`` and fingerprint (spec §3,
§5). The capability→tool mapping is *injected* rather than imported, because
which tool backs which capability is the Gateway's business (spec §2.4).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from kai.ir.control import linear_graph
from kai.ir.errors import IrValidationError
from kai.ir.nodes import (
    IR_VERSION,
    IntentNode,
    PermissionNode,
    PlanNode,
    PolicyConstraintsNode,
    StateSnapshotNode,
    StepNode,
)
from kai.ir.program import KaiProgram, require_valid
from kai.planner.schemas import PlanProposal
from kai.tasks.models import Task
from kai.tools.definition import ToolDefinition
from kai.tools.risk import RiskLevel

#: Fixed namespace for every program id, so identity is reproducible across
#: processes and restarts rather than depending on a random v4.
KAI_PROGRAM_NAMESPACE: UUID = uuid5(NAMESPACE_URL, "kai.kai-ir.program")

#: Risk levels whose ordinary handling is "ask a human first". Mirrors the
#: Policy Engine's rule so a program's *declaration* matches what the Gateway
#: will actually do. It is a declaration only: the Policy Engine remains the
#: authority, and any disagreement is resolved in its favour (spec §2.5).
APPROVAL_BY_RISK: frozenset[RiskLevel] = frozenset(
    {RiskLevel.MEDIUM, RiskLevel.HIGH}
)


def action_id_for(
    *, task_id: UUID, plan_version: int, step_order: int, index: int
) -> str:
    """Deterministic, resume-stable identity for exactly one action.

    Built only from the task, the bound plan version, the step, and the action's
    position inside that step — never from wall-clock time or randomness
    (spec §5). A resumed trigger re-issues the identical id for the same action,
    so evidence records and the audit trail stay correlatable, and two different
    plan versions can never collide.

    This is the identity the runtime already used for a tool invocation's
    request id; the IR now owns it, and the id's value is unchanged so existing
    evidence, approval intent hashes, and request correlation all keep working.
    """
    return f"runtime-{task_id}-{plan_version}-{step_order}-{index}"


def program_id_for(*, task_id: UUID, plan_version: int) -> UUID:
    """Deterministic program identity from the task and the plan it runs.

    Derived rather than random, so a resumed execution rebuilds the *same*
    program id and the correlation carried in the evidence log stays continuous.
    """
    return uuid5(KAI_PROGRAM_NAMESPACE, f"{task_id}:{plan_version}")


def steps_from_plan(proposal: PlanProposal) -> list[StepNode]:
    """The plan's steps as ``StepNode``s, in declaration order.

    The planner's ``action`` is the spec's ``capability``: both name the same
    abstract capability, and neither names a tool.
    """
    return [
        StepNode(
            step_id=step.step_id,
            order=step.order,
            capability=step.action,
            purpose=step.purpose,
            inputs=list(step.inputs),
            expected_output=step.expected_output,
            verification_hint=step.verification_hint,
        )
        for step in proposal.steps
    ]


def allowed_capabilities_from_task(
    task: Task, capability_to_tool: Mapping[str, str]
) -> list[str]:
    """The capabilities the task contract permits, derived from its tools.

    The contract names tools and each tool backs exactly one declared
    capability, so the allowed set is the reverse of the injected mapping —
    derived rather than hand-maintained, and empty-safe (a task that allows
    nothing this slice can represent is refused, not silently widened).
    """
    reverse = {tool: capability for capability, tool in capability_to_tool.items()}
    allowed: list[str] = []
    for tool_name in task.allowed_tools:
        capability = reverse.get(tool_name)
        if capability is not None and capability not in allowed:
            allowed.append(capability)
    if not allowed:
        raise IrValidationError(
            "the task contract allows no capability this slice can represent"
        )
    return allowed


def risk_from_policy(risk_policy: str) -> RiskLevel:
    """The task's risk ceiling as a ``RiskLevel``, or a refusal."""
    try:
        return RiskLevel(str(risk_policy).upper())
    except ValueError as exc:
        raise IrValidationError(
            f"task risk policy {risk_policy!r} is not a known risk level"
        ) from exc


def permission_for(
    *,
    capability: str,
    tool: ToolDefinition | None,
    risk_cap: RiskLevel,
) -> PermissionNode:
    """The declared permission requirement for one capability.

    Read from the catalog's own declaration for the tool behind the capability.
    When the capability has no registered tool the declaration fails closed: the
    ceiling is ``CRITICAL``, an account is required, and approval is required —
    so an unbacked capability can never look harmless.
    """
    if tool is None:
        return PermissionNode(
            requires_capability=capability,
            max_risk=RiskLevel.CRITICAL,
            requires_account=True,
            requires_approval=True,
        )
    return PermissionNode(
        requires_capability=capability,
        max_risk=tool.risk_level,
        requires_account=tool.requires_account,
        requires_approval=tool.risk_level in APPROVAL_BY_RISK
        or tool.risk_level.rank > risk_cap.rank,
    )


def constraints_from_task(
    task: Task, *, capability_to_tool: Mapping[str, str]
) -> PolicyConstraintsNode:
    """The program-wide rule envelope, read from the sealed task contract."""
    return PolicyConstraintsNode(
        allowed_capabilities=allowed_capabilities_from_task(
            task, capability_to_tool
        ),
        max_risk=risk_from_policy(task.risk_policy),
        budget=task.max_steps,
        deadline=task.deadline,
        content_rules=[task.verification_method, task.fallback],
    )


def program_from_plan(
    *,
    task: Task,
    plan_version: int,
    plan_status: str,
    proposal: PlanProposal,
    plan_hash: str,
    registry: Any,
    capability_to_tool: Mapping[str, str],
    cursor: int = 0,
    steps_used: int = 0,
    bound_at: datetime | None = None,
) -> KaiProgram:
    """Build the sealed, validated IR program for one task's current plan.

    Takes the plan's version and status as plain values rather than the ORM row:
    the IR describes a plan, it does not reach into storage for it.

    The catalog and the capability→tool mapping are injected together, and the
    tool index is derived from that one pair — so a program can never be built
    from a capability that means one thing to the mapping and another to the
    catalog, which would let a permission be declared for the wrong tool.

    A persisted plan declares an ordered list of steps and no branches, so the
    program is given the control flow that actually describes it: one action node
    per step, chained, ending at a result node. That is a real graph, validated
    and walked exactly like a branching one — a branch is a later change to this
    chain, not a separate execution mode.
    """
    risk_cap = risk_from_policy(task.risk_policy)
    tools = tools_by_capability(registry, capability_to_tool)
    steps = steps_from_plan(proposal)
    capabilities = list(dict.fromkeys(step.capability for step in steps))
    nodes, edges = linear_graph(steps)

    program = KaiProgram(
        ir_version=IR_VERSION,
        program_id=program_id_for(task_id=task.id, plan_version=plan_version),
        intent=IntentNode(
            goal=task.goal,
            success_criteria=list(task.success_criteria),
        ),
        plan=PlanNode(
            plan_version=plan_version,
            status=plan_status,
            steps=steps,
        ),
        nodes=nodes,
        edges=edges,
        constraints=constraints_from_task(
            task, capability_to_tool=capability_to_tool
        ),
        permissions=[
            permission_for(
                capability=capability,
                tool=tools.get(capability),
                risk_cap=risk_cap,
            )
            for capability in capabilities
        ],
        snapshot=StateSnapshotNode(
            plan_version=plan_version,
            plan_hash=plan_hash,
            cursor=cursor,
            steps_used=steps_used,
            bound_at=bound_at,
        ),
    )
    return require_valid(program.seal())


def tools_by_capability(
    registry: Any, capability_to_tool: Mapping[str, str]
) -> dict[str, ToolDefinition]:
    """Index the catalog by each *slice* capability, through the injected map.

    Note that ``ToolDefinition.capability`` is the tool's domain grouping
    (``calendar``, ``contacts``), not the capability a plan names
    (``create_calendar_event``). So the index is built by resolving each
    capability to its tool and looking *that* up in the sealed registry: a
    capability with no registered tool is simply absent, and the caller's fail
    closed. Looking the capability up directly would miss every tool and make
    each permission declaration a silent refusal.
    """
    indexed: dict[str, ToolDefinition] = {}
    for capability, tool_name in capability_to_tool.items():
        definition = registry.get_if_present(tool_name)
        if definition is not None:
            indexed[capability] = definition
    return indexed


__all__ = [
    "APPROVAL_BY_RISK",
    "KAI_PROGRAM_NAMESPACE",
    "action_id_for",
    "allowed_capabilities_from_task",
    "constraints_from_task",
    "permission_for",
    "program_from_plan",
    "program_id_for",
    "risk_from_policy",
    "steps_from_plan",
    "tools_by_capability",
]
