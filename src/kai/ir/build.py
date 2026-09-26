"""Translation: persisted planning output → a validated KAI-IR program.

This is the whole of the "reasoning produces IR" direction. It reads only what
already exists — the sealed task contract, the accepted plan row, the catalog
declaration behind each capability — and emits data. It never consults the
model, never executes a tool, and never invents a capability, a permission, or a
step the plan did not contain.

A branch the plan declared is translated the same way: its condition becomes the
IR's own condition, and its two declared outcomes become the two edges leaving a
condition node. Translation adds no vocabulary of its own, so there is nothing
here a plan can say that the IR could not already say.

The program is derived, not stored: the same task, plan, and progress always
rebuild the same program with the same ``program_id`` and fingerprint (spec §3,
§5). The capability→tool mapping is *injected* rather than imported, because
which tool backs which capability is the Gateway's business (spec §2.4).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import NAMESPACE_URL, UUID, uuid5

from kai.ir.control import linear_graph
from kai.ir.errors import IrValidationError
from kai.ir.nodes import (
    IR_VERSION,
    ConditionNode,
    ConditionSubject,
    ControlEdge,
    ControlNode,
    ControlNodeKind,
    IntentNode,
    PermissionNode,
    PlanNode,
    PolicyConstraintsNode,
    StateSnapshotNode,
    StepNode,
)
from kai.ir.program import KaiProgram, require_valid
from kai.tasks.models import Task
from kai.tools.definition import ToolDefinition
from kai.tools.risk import RiskLevel

if TYPE_CHECKING:
    # The plan schema is referenced only in annotations, and importing it for
    # real would close a cycle: the plan schema asks the IR whether the branch it
    # declared is representable. Type-only is enough here and keeps the
    # dependency pointing one way — the Planner talks to the IR, not vice versa.
    from kai.planner.schemas import PlanBranch, PlanCondition, PlanProposal

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


def condition_from_plan(
    declaration: PlanCondition, *, condition_id: str = "declared"
) -> ConditionNode:
    """The IR condition one declared branch condition becomes.

    A one-to-one translation, not a second condition language: the fact, the
    comparison, and the polarity carry straight into the ``ConditionNode`` the
    graph will hold, so the vocabulary a plan is allowed to branch on is exactly
    the vocabulary the IR can evaluate — and exactly the one whose two-way
    partition a branch is expressed in terms of (spec §8.14).

    A declaration that cannot be represented is refused here, by
    ``ConditionNode``'s own validation, rather than being widened into an
    expression or left in to quietly never match at run time. The ``condition_id``
    is supplied by whoever places this condition in a graph; the one default here
    is for callers that only want to know whether it is representable at all.
    """
    return ConditionNode(
        condition_id=condition_id,
        subject=ConditionSubject(
            kind=declaration.kind,
            step_order=declaration.step_order,
            field=declaration.field,
        ),
        operator=declaration.operator,
        value=declaration.value,
        negate=declaration.negate,
    )


def branch_errors(
    *, step_orders: Sequence[int], branches: Sequence[PlanBranch]
) -> list[str]:
    """Every reason these branch declarations cannot become a control-flow graph.

    Declaration-level rules only. They are re-checked when the program is built,
    and this exists so a plan that cannot *be* a graph is refused when it is
    offered — with a message about the plan — rather than surfacing later as a
    program the runtime could not walk.

    Deliberately absent: anything that needs the whole graph. Whether a condition
    reads a fact that exists on *every* path to the branch is a property of the
    compiled program, and ``KaiProgram`` already proves it by searching
    backwards from the condition (spec §8.3). Two checks for one rule would be
    two chances to disagree about it.
    """
    known = set(step_orders)
    errors: list[str] = []
    sources: set[int] = set()

    for branch in sorted(branches, key=lambda declared: declared.after_step):
        where = f"branch after step {branch.after_step}"

        if branch.after_step in sources:
            errors.append(f"{where}: a step carries only one branch")
        sources.add(branch.after_step)

        for name, target in (
            ("after_step", branch.after_step),
            ("then_step", branch.then_step),
            ("else_step", branch.else_step),
        ):
            if target not in known:
                errors.append(f"{where}: {name} {target} is not a step in this plan")

        if branch.then_step == branch.else_step:
            errors.append(f"{where}: then_step and else_step are the same step")
            continue

        # The partition rule. Exactly one target continues the chain where it
        # stood, and the other jumps past the steps it skips. That is what keeps
        # every declared step on one acyclic path — a step that no edge reaches is
        # dead structure, and a fork that skipped nothing is not a fork at all.
        if min(branch.then_step, branch.else_step) != branch.after_step + 1:
            errors.append(
                f"{where}: one of then_step/else_step must be step "
                f"{branch.after_step + 1}, the step that follows the branch"
            )

    return errors


def graph_from_plan(
    steps: Sequence[StepNode],
    branches: Sequence[PlanBranch],
    *,
    result: str = "COMPLETED",
) -> tuple[list[ControlNode], list[ControlEdge]]:
    """The control flow a plan declares: a chain, or a chain with forks in it.

    A plan that declares no branches gets exactly the chain it always got
    (:func:`~kai.ir.control.linear_graph`), so every plan accepted before
    branching existed still compiles to the program it compiled to then.

    A declared branch replaces the continuation of ``after_step`` with a condition
    node and routes its two outcomes to the two steps the plan named: the declared
    comparison on one edge, the same comparison negated on the other. That is the
    only two-way shape the graph validator can prove covers every value of the
    fact, and the planner does not get to choose a third — it names where each
    outcome goes, and the predicate decides nothing more than that.

    Step ids, and therefore action identity, are unchanged by any of this: a
    branch decides which step runs next, never what a step is called, so a resumed
    run re-issues the same request id it issued before (spec §5).
    """
    if not branches:
        return linear_graph(list(steps), result=result)

    problems = branch_errors(
        step_orders=[step.order for step in steps], branches=branches
    )
    if problems:
        raise IrValidationError("; ".join(problems))

    by_source = {branch.after_step: branch for branch in branches}
    known_orders = {step.order for step in steps}
    # A step a path *begins* at is not entered from the step before it: control
    # arrives there from a condition, so its predecessor's chain ends instead.
    path_starts = {
        target
        for branch in branches
        for target in (branch.then_step, branch.else_step)
    }

    nodes: list[ControlNode] = [
        ControlNode(
            node_id=f"action-{step.order}",
            kind=ControlNodeKind.ACTION,
            step_order=step.order,
        )
        for step in steps
    ]
    edges: list[ControlEdge] = []
    ends: list[int] = []

    for step in steps:
        order = step.order
        branch = by_source.get(order)
        if branch is not None:
            condition_id = f"branch-{order}"
            taken = condition_from_plan(branch.condition, condition_id=condition_id)
            other = taken.model_copy(
                update={
                    "condition_id": f"{condition_id}-not",
                    "negate": not taken.negate,
                }
            )
            nodes.append(
                ControlNode(
                    node_id=f"condition-{order}",
                    kind=ControlNodeKind.CONDITION,
                    condition=taken,
                )
            )
            edges.append(
                ControlEdge(
                    from_node=f"action-{order}", to_node=f"condition-{order}"
                )
            )
            for target, guard in ((branch.then_step, taken), (branch.else_step, other)):
                edges.append(
                    ControlEdge(
                        from_node=f"condition-{order}",
                        to_node=f"action-{target}",
                        when=guard,
                    )
                )
            continue

        # The chain continues only where control can still arrive from the step
        # before. A step the plan put on a path of its own is entered from a
        # condition instead, so the step before it ends there — and so does the
        # last step, wherever the plan put the end.
        following = order + 1
        if following in known_orders and following not in path_starts:
            edges.append(
                ControlEdge(from_node=f"action-{order}", to_node=f"action-{following}")
            )
        else:
            ends.append(order)
            edges.append(
                ControlEdge(from_node=f"action-{order}", to_node=f"result-{order}")
            )

    for order in ends:
        nodes.append(
            ControlNode(
                node_id=f"result-{order}",
                kind=ControlNodeKind.RESULT,
                result=result,
            )
        )

    return nodes, edges


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

    The control flow is whatever the plan declares: the chain it has always been
    when no branch is declared, and the chain with the plan's own forks in it when
    one is. Either way the result goes through the same validation as any other
    program, and a branch declaration the graph cannot represent is refused here
    — before a step runs — rather than being executed and discovered.
    """
    risk_cap = risk_from_policy(task.risk_policy)
    tools = tools_by_capability(registry, capability_to_tool)
    steps = steps_from_plan(proposal)
    capabilities = list(dict.fromkeys(step.capability for step in steps))
    nodes, edges = graph_from_plan(steps, proposal.branches)

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
    "branch_errors",
    "condition_from_plan",
    "constraints_from_task",
    "graph_from_plan",
    "permission_for",
    "program_from_plan",
    "program_id_for",
    "risk_from_policy",
    "steps_from_plan",
    "tools_by_capability",
]
