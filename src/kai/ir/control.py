"""Deciding where a KAI-IR program goes next, and checking it is decidable.

Two responsibilities, kept apart on purpose:

* **Evaluation** turns a :class:`~kai.ir.nodes.ConditionNode` into a boolean by
  comparing one durable fact against one literal. There is no interpreter, no
  expression tree, and no way to reach anything the fact vocabulary does not
  already name.
* **Traversal** walks a validated program's graph from its entry to the single
  node that is eligible to run next, or to the end of the flow.

Both are pure functions of the program plus the facts the runtime already has
durable. Neither performs I/O, so the same call after a resume yields the same
node — the branch a program takes is a function of what already happened, not a
fresh decision taken mid-run.

The Observation -> ControlFacts -> Condition contract
---------------------------------------------------

A condition never reads a ``ToolResult``. The three stages are separate and each
one is owned elsewhere:

1. **Produced** — ``kai.ir.adapter.observation_from_result`` turns the Gateway's
   transient ``ToolResult`` into an ``ObservationNode``, inside
   ``AgentRuntime._execute``, immediately after ``ToolGateway.execute`` returns.
2. **Persisted** — ``kai.ir.adapter.evidence_from_observation`` writes that
   observation into the ``RuntimeExecution.evidence`` log, and the runtime
   assigns the row *before* any branch could read it.
3. **Read back** — ``AgentRuntime._control_facts`` rebuilds ``ControlFacts`` from
   that durable log via ``observation_from_evidence``. Only then can
   :func:`evaluate` see a fact at all.

A condition therefore reads a *projection of durable state*, never an in-memory
value. That is what makes evaluation reproducible: a resume rebuilds the same
facts from the same log and reaches the same answer. Reading the ``ToolResult``
directly would make the answer a function of process memory, and the branch
taken would no longer be derivable after a crash.

**This module's one dependency on Runtime behaviour (contract, spec §7.1).**
:meth:`ControlFacts.observation_for` treats *the last observation recorded for a
step* as *that step's outcome*. That is only true because the current Runtime
stops a step at its first non-successful intent, so the final record for a step
is the record that decided it. The IR states the rule; the Runtime supplies the
guarantee.

If the Runtime ever continues past a failing intent within a step, this module's
semantics change with no change to the IR — so that change must revise this
contract explicitly rather than inherit it silently. A step running several
intents, or a step whose intents are retried, both fall under this contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from kai.ir.errors import IrConditionError, IrControlFlowError
from kai.ir.nodes import (
    ConditionNode,
    ConditionOperator,
    ControlEdge,
    ControlNode,
    ControlNodeKind,
    FactKind,
    ObservationNode,
    StepNode,
)
from kai.ir.program import KaiProgram


@dataclass(frozen=True)
class ControlFacts:
    """The durable facts a condition may read, captured at one moment.

    Every field is something the runtime can reconstruct from the execution row
    alone, which is what makes branching resumable without new storage: replay
    the same facts, get the same branch.
    """

    cursor: int = 0
    task_state: str = ""
    steps_used: int = 0
    observations: tuple[ObservationNode, ...] = field(default_factory=tuple)

    def observation_for(self, step_order: int) -> ObservationNode:
        """The outcome of the step at ``step_order``.

        The last observation recorded for that step, under the Runtime contract
        named in this module's docstring: a step runs its intents in order and
        the runtime stops the step on the first non-successful intent, so the
        last record for a step is that step's outcome.

        Absent is not the same as unsuccessful. A step with no observation at all
        is a fact that does not exist yet, and it is refused rather than guessed
        at — only a recorded observation may be read, whatever it says.
        """
        latest: ObservationNode | None = None
        for observation in self.observations:
            if observation.step_order == step_order:
                latest = observation
        if latest is None:
            raise IrConditionError(
                f"no observation has been recorded for step {step_order}; a "
                f"condition cannot read a fact that does not exist yet"
            )
        return latest


def _fact_value(condition: ConditionNode, facts: ControlFacts) -> object:
    """Read the one fact ``condition`` names."""
    subject = condition.subject
    if subject.kind is FactKind.OBSERVATION:
        assert subject.step_order is not None and subject.field is not None
        observation = facts.observation_for(subject.step_order)
        return getattr(observation, subject.field)
    if subject.kind is FactKind.CURSOR:
        return facts.cursor
    if subject.kind is FactKind.STEPS_USED:
        return facts.steps_used
    return facts.task_state


def _compare(operator: ConditionOperator, left: object, right: object) -> bool:
    if right is None and operator not in (ConditionOperator.EQ, ConditionOperator.NEQ):
        # Unreachable through ConditionNode, which refuses a null operand with
        # any other operator. Checked again here so a program assembled without
        # that validation still fails closed instead of asking Python to order
        # "nothing" and getting an answer by accident.
        raise IrConditionError(
            f"{operator.value} is undefined for a null operand; a condition may only "
            f"ask whether a nullable fact holds a value"
        )
    if operator is ConditionOperator.EQ:
        return left == right
    if operator is ConditionOperator.NEQ:
        return left != right
    if operator is ConditionOperator.GT:
        return left > right  # type: ignore[operator]
    if operator is ConditionOperator.GTE:
        return left >= right  # type: ignore[operator]
    if operator is ConditionOperator.LT:
        return left < right  # type: ignore[operator]
    if operator is ConditionOperator.LTE:
        return left <= right  # type: ignore[operator]
    if operator is ConditionOperator.IN:
        return left in right  # type: ignore[operator]
    return left not in right  # type: ignore[operator]


def evaluate(condition: ConditionNode, facts: ControlFacts) -> bool:
    """Answer one condition against ``facts``.

    A single comparison, never a compound expression: the operator and the
    operand's type were already checked against each other when the program was
    built, so the only way this can fail here is by asking about a fact that does
    not exist yet — and that is refused rather than guessed at.
    """
    actual = _fact_value(condition, facts)
    try:
        result = _compare(condition.operator, actual, condition.value)
    except TypeError as exc:  # the operand types are validated; a missing fact is not
        raise IrConditionError(
            f"condition {condition.condition_id!r} cannot compare a "
            f"{type(actual).__name__} fact with a "
            f"{type(condition.value).__name__} operand"
        ) from exc
    return not result if condition.negate else result


def _eligible(
    program: KaiProgram, node: ControlNode, facts: ControlFacts
) -> ControlEdge:
    """The single edge control may take out of ``node``; refuse any ambiguity.

    Zero eligible edges means the program's declared branch structure does not
    cover the facts it actually produced. More than one means a fact satisfied
    two guards and the program never chose. Both are the program's error rather
    than the runtime's, and both stop the run instead of picking something.
    """
    eligible: list[ControlEdge] = []
    for edge in program.edges_from(node.node_id):
        if edge.when is None or evaluate(edge.when, facts):
            eligible.append(edge)
    if not eligible:
        raise IrControlFlowError(
            f"no control edge out of node {node.node_id!r} is eligible; the branch "
            f"structure does not cover the observed facts"
        )
    if len(eligible) > 1:
        conditions = [edge.when.condition_id for edge in eligible if edge.when is not None]
        detail = f" (guards: {conditions})" if conditions else ""
        raise IrControlFlowError(
            f"{len(eligible)} control edges out of node {node.node_id!r} are eligible; "
            f"the branch is ambiguous{detail}"
        )
    return eligible[0]


def _advance(program: KaiProgram, node: ControlNode, facts: ControlFacts) -> ControlNode:
    """The node reached by leaving ``node`` under the current facts."""
    target = program.node(_eligible(program, node, facts).to_node)
    if target is None:  # pragma: no cover - _validate_edges already guarantees this
        raise IrControlFlowError(
            f"control edge from node {node.node_id!r} targets an undeclared node"
        )
    return target


def next_node(program: KaiProgram, facts: ControlFacts) -> ControlNode | None:
    """The node eligible to run next, or ``None`` once the flow is finished.

    Reached by walking from the program's entry every time rather than by
    remembering where the last walk stopped. Walking is cheap, the graph is
    bounded and acyclic, and recomputing means a resume cannot inherit a stale
    position: the answer depends only on the facts, which are durable.
    """
    entry_id = program.entry_node_id()
    if entry_id is None:
        raise IrControlFlowError("control flow has no single entry node to start from")
    current = program.node(entry_id)
    if current is None:  # pragma: no cover - the entry is always a declared node
        raise IrControlFlowError(f"entry node {entry_id!r} is not declared")

    # The graph is validated acyclic, so a walk visits at most every node once.
    for _ in range(len(program.nodes) + 1):
        if current.kind is ControlNodeKind.RESULT:
            return None
        if current.kind is ControlNodeKind.CONDITION:
            current = _advance(program, current, facts)
            continue
        assert current.step_order is not None  # guaranteed by ControlNode
        step = program.step(current.step_order)
        if step is None:  # pragma: no cover - _validate_action_nodes guarantees this
            raise IrControlFlowError(
                f"action node {current.node_id!r} names step {current.step_order}, "
                f"which is not in the plan"
            )
        if step.order > facts.cursor:
            return current
        current = _advance(program, current, facts)

    raise IrControlFlowError(
        "control flow walk exceeded the program's own bounds; the graph cannot be "
        "traversed deterministically"
    )


def next_step(program: KaiProgram, facts: ControlFacts) -> StepNode | None:
    """The step to execute next, or ``None`` if the flow has nothing left.

    This is the whole interface the runtime needs: hand it the program and the
    durable facts, and it tells you which step is next. It never reports a
    decision, a transition, or a permission — it names the next step and stops.
    """
    node = next_node(program, facts)
    if node is None:
        return None
    assert node.step_order is not None  # next_node only returns action nodes
    step = program.step(node.step_order)
    if step is None:  # pragma: no cover - _validate_action_nodes guarantees this
        raise IrControlFlowError(
            f"action node {node.node_id!r} names step {node.step_order}, which is "
            f"not in the plan"
        )
    return step


def linear_graph(steps: list[StepNode], *, result: str = "COMPLETED") -> tuple[
    list[ControlNode], list[ControlEdge]
]:
    """The control flow for a plan that has no branches: one node per step.

    Every program has a graph; a plan that does not branch still gets one, and
    gets the same validation and the same walk as a plan that does. That is
    deliberate: branching then works by replacing a section of this chain with a
    condition node, rather than by introducing a second execution path the
    runtime has to learn about separately.
    """
    nodes: list[ControlNode] = [
        ControlNode(
            node_id=f"action-{step.order}",
            kind=ControlNodeKind.ACTION,
            step_order=step.order,
        )
        for step in steps
    ]
    nodes.append(ControlNode(node_id="result", kind=ControlNodeKind.RESULT, result=result))
    edges = [
        ControlEdge(
            from_node=f"action-{step.order}",
            to_node=(
                f"action-{steps[index + 1].order}"
                if index + 1 < len(steps)
                else "result"
            ),
        )
        for index, step in enumerate(steps)
    ]
    return nodes, edges


__all__ = [
    "ControlFacts",
    "evaluate",
    "linear_graph",
    "next_node",
    "next_step",
]
