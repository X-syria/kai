"""``KaiProgram`` — the KAI-IR top-level shape, its canonical form, and its
fingerprint.

The canonical serialization is the one the spec fixes in §3: JSON with sorted
keys, compact separators, UTF-8 — the same normalization the runtime already
used for ``plan_fingerprint``, now owned here so there is exactly one
implementation of "the bytes of this plan".
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from kai.ir.errors import IrConditionError, IrControlFlowError, IrValidationError
from kai.ir.nodes import (
    IR_VERSION,
    ActionNode,
    ConditionSubject,
    ControlEdge,
    ControlNode,
    ControlNodeKind,
    FactKind,
    IntentNode,
    ObservationNode,
    PermissionNode,
    PlanNode,
    PolicyConstraintsNode,
    StateSnapshotNode,
    StepNode,
)

#: A control-flow graph may not be larger than the step budget it serves, so a
#: graph can never be used to smuggle in unbounded work.
_MAX_GRAPH = 1000


def canonical_json(value: Any) -> str:
    """The canonical form: sorted keys, compact separators, UTF-8 preserved."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def canonical_fingerprint(value: Any) -> str:
    """SHA-256 hex digest of :func:`canonical_json`."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class KaiProgram(BaseModel):
    """One KAI-IR program: an intent, the plan it runs, how it branches, and its
    constraints.

    A program is a *derived, deterministic* value. It is not persisted and owns
    no table: it is rebuilt from the task contract, the persisted plan, and the
    execution row, and the same inputs always produce the same
    :attr:`fingerprint`. That is what makes a plan auditable and replayable
    without any new storage (spec §3, §5).

    ``nodes`` and ``edges`` are the declared control flow, in declaration order.
    They are static: the graph says which step may run next given what has
    already been observed, and it never carries the result of running it.

    ``observations``, ``result``, and ``snapshot`` are populated as execution
    proceeds. They are excluded from the fingerprint on purpose — a program's
    identity is what it intends to do, not what happened when it ran.
    """

    model_config = ConfigDict(extra="forbid")

    ir_version: str = IR_VERSION
    program_id: UUID
    intent: IntentNode
    plan: PlanNode
    constraints: PolicyConstraintsNode
    nodes: list[ControlNode] = Field(default_factory=list, max_length=_MAX_GRAPH)
    edges: list[ControlEdge] = Field(default_factory=list, max_length=_MAX_GRAPH)
    permissions: list[PermissionNode] = Field(default_factory=list)
    snapshot: StateSnapshotNode | None = None
    observations: list[ObservationNode] = Field(default_factory=list)
    fingerprint: str = Field(default="", max_length=64)

    @model_validator(mode="after")
    def _version_is_declared(self) -> Self:
        if self.ir_version != IR_VERSION:
            raise IrValidationError(
                f"unsupported ir_version {self.ir_version!r}; this build declares "
                f"{IR_VERSION!r} only"
            )
        return self

    def canonical_form(self) -> dict[str, Any]:
        """The hashed content: intent, plan, control flow, constraints, permissions.

        Declaration order of steps, nodes, edges, and permissions is part of the
        canonical form (spec §11, resolved for v0), so each is emitted exactly as
        declared and not re-sorted here. The graph is included whole: changing
        which branch a program can take changes what it intends to do, and must
        change its identity.
        """
        return {
            "ir_version": self.ir_version,
            "intent": self.intent.model_dump(mode="json"),
            "plan": self.plan.model_dump(mode="json"),
            "nodes": [node.model_dump(mode="json") for node in self.nodes],
            "edges": [edge.model_dump(mode="json") for edge in self.edges],
            "constraints": self.constraints.model_dump(mode="json"),
            "permissions": [
                permission.model_dump(mode="json") for permission in self.permissions
            ],
        }

    def computed_fingerprint(self) -> str:
        """The fingerprint this program's content implies."""
        return canonical_fingerprint(self.canonical_form())

    def seal(self) -> KaiProgram:
        """Stamp the content fingerprint onto the program and return it."""
        return self.model_copy(update={"fingerprint": self.computed_fingerprint()})

    def validate_executable(self) -> KaiProgram:
        """Check the program is executable; raise :class:`IrValidationError`.

        Everything here is a structural check on data the program carries. None
        of it can grant a permission, widen a contract, or decide an outcome —
        those stay with the Policy Engine, the Approval Manager, and the task
        state machine.
        """
        if self.ir_version != IR_VERSION:
            raise IrValidationError(
                f"unsupported ir_version {self.ir_version!r}; this build declares "
                f"{IR_VERSION!r} only"
            )

        if self.intent.entry is not True:
            raise IrValidationError("a program must have exactly one entry point")

        if self.fingerprint and self.fingerprint != self.computed_fingerprint():
            raise IrValidationError(
                "fingerprint does not match the program's content; the program was "
                "edited after it was sealed"
            )

        allowed = set(self.constraints.allowed_capabilities)
        for step in self.plan.steps:
            if step.capability not in allowed:
                raise IrValidationError(
                    f"step {step.step_id} uses capability {step.capability!r}, which "
                    f"is not in the program's allowed capabilities"
                )

        declared = {permission.requires_capability for permission in self.permissions}
        undeclared = sorted(
            {step.capability for step in self.plan.steps} - allowed - declared
        )
        if undeclared:
            raise IrValidationError(
                f"capabilities used by the plan have no declared permission: {undeclared}"
            )

        if len(self.plan.steps) > self.constraints.budget:
            raise IrValidationError(
                f"plan has {len(self.plan.steps)} steps, exceeding the program's "
                f"budget of {self.constraints.budget}"
            )

        if self.snapshot is not None:
            self._validate_snapshot()

        self._validate_observations()
        self._validate_graph()
        return self

    def _validate_snapshot(self) -> None:
        snapshot = self.snapshot
        assert snapshot is not None  # guarded by the caller
        if snapshot.plan_version != self.plan.plan_version:
            raise IrValidationError(
                f"the snapshot is bound to plan version {snapshot.plan_version}, but "
                f"the program instantiates version {self.plan.plan_version}"
            )
        if snapshot.cursor > len(self.plan.steps):
            raise IrValidationError(
                f"the snapshot's cursor ({snapshot.cursor}) is past the end of a "
                f"{len(self.plan.steps)}-step plan"
            )

    def _validate_observations(self) -> None:
        """The observation log is append-only: one record per action id.

        A duplicate would mean an action's outcome was rewritten rather than
        appended, which would break the audit trail the log exists to provide.
        """
        if not self.observations:
            return
        seen: set[str] = set()
        for observation in self.observations:
            if observation.action_id in seen:
                raise IrValidationError(
                    f"duplicate observation for action {observation.action_id}; "
                    "the log is append-only"
                )
            seen.add(observation.action_id)
            if observation.step_order > len(self.plan.steps):
                raise IrValidationError(
                    f"observation for step {observation.step_order} does not exist in "
                    "this plan"
                )

    def step(self, order: int) -> StepNode | None:
        """The step at ``order``, or ``None`` if the program has no such step."""
        for step in self.plan.steps:
            if step.order == order:
                return step
        return None

    def node(self, node_id: str) -> ControlNode | None:
        """The control-flow node with ``node_id``, if the program declares one."""
        for node in self.nodes:
            if node.node_id == node_id:
                return node
        return None

    def edges_from(self, node_id: str) -> list[ControlEdge]:
        """The edges leaving ``node_id``, in declaration order."""
        return [edge for edge in self.edges if edge.from_node == node_id]

    def entry_node_id(self) -> str | None:
        """The node execution starts at: the unique node nothing leads into.

        Derived from the edges rather than declared alongside them. A separate
        declaration could disagree with the graph, and a program that starts
        somewhere other than where its own edges say it starts is exactly the
        ambiguity worth refusing.
        """
        targets = {edge.to_node for edge in self.edges}
        sources = [node.node_id for node in self.nodes if node.node_id not in targets]
        if len(sources) != 1:
            return None
        return sources[0]

    def action_node_for_step(self, order: int) -> ControlNode | None:
        """The control-flow node that executes the step at ``order``."""
        for node in self.nodes:
            if node.kind is ControlNodeKind.ACTION and node.step_order == order:
                return node
        return None

    def _validate_graph(self) -> None:
        """Refuse a graph the program could not safely be walked along.

        Each check exists because, without it, some program would be executable
        while having no defined next step — which is the one thing a control-flow
        representation may not leave undecided.
        """
        if not self.nodes:
            raise IrControlFlowError("a program must declare its control flow")
        if not self.edges:
            raise IrControlFlowError("a program's control flow must declare an edge")

        self._validate_node_identity()
        self._validate_edges()
        self._validate_degree()
        entry = self.entry_node_id()
        if entry is None:
            raise IrControlFlowError(
                "control flow must have exactly one entry node, the single node "
                "nothing leads into"
            )
        self._validate_reachable(entry)
        self._validate_acyclic()
        self._validate_guards()
        self._validate_action_nodes()
        self._validate_condition_sources()

    def _validate_node_identity(self) -> None:
        seen_nodes: set[str] = set()
        seen_conditions: set[str] = set()
        for node in self.nodes:
            if node.node_id in seen_nodes:
                raise IrControlFlowError(
                    f"duplicate control node {node.node_id!r}; node ids must be unique"
                )
            seen_nodes.add(node.node_id)
            if node.condition is not None:
                if node.condition.condition_id in seen_conditions:
                    raise IrConditionError(
                        f"duplicate condition id {node.condition.condition_id!r}; "
                        f"condition ids must be unique"
                    )
                seen_conditions.add(node.condition.condition_id)

    def _validate_edges(self) -> None:
        seen_pairs: set[tuple[str, str]] = set()
        for edge in self.edges:
            if self.node(edge.from_node) is None:
                raise IrControlFlowError(
                    f"control edge starts at undeclared node {edge.from_node!r}"
                )
            if self.node(edge.to_node) is None:
                raise IrControlFlowError(
                    f"control edge targets undeclared node {edge.to_node!r}"
                )
            pair = (edge.from_node, edge.to_node)
            if pair in seen_pairs:
                raise IrControlFlowError(
                    f"duplicate control edge {edge.from_node!r} -> {edge.to_node!r}; "
                    f"the branch between two nodes is declared once"
                )
            seen_pairs.add(pair)

    def _validate_degree(self) -> None:
        """Every node either ends the program or has exactly one way onward."""
        for node in self.nodes:
            outgoing = self.edges_from(node.node_id)
            if node.kind is ControlNodeKind.RESULT:
                if outgoing:
                    raise IrControlFlowError(
                        f"result node {node.node_id!r} must not have an outgoing edge; "
                        f"it ends the flow"
                    )
                continue
            if not outgoing:
                raise IrControlFlowError(
                    f"node {node.node_id!r} has no outgoing edge and is not a result "
                    f"node, so control reaching it has nowhere to go"
                )
            if node.kind is ControlNodeKind.ACTION:
                if len(outgoing) != 1:
                    raise IrControlFlowError(
                        f"action node {node.node_id!r} has {len(outgoing)} outgoing "
                        f"edges; a step's continuation is declared once"
                    )
                if outgoing[0].when is not None:
                    raise IrControlFlowError(
                        f"action node {node.node_id!r} has a guarded edge; a step is "
                        f"followed by a condition node, not by a guard"
                    )

    def _validate_reachable(self, entry: str) -> None:
        seen = {entry}
        frontier = [entry]
        while frontier:
            current = frontier.pop()
            for edge in self.edges_from(current):
                if edge.to_node not in seen:
                    seen.add(edge.to_node)
                    frontier.append(edge.to_node)
        unreachable = sorted({node.node_id for node in self.nodes} - seen)
        if unreachable:
            raise IrControlFlowError(
                f"control flow contains nodes nothing can reach: {unreachable}"
            )

    def _validate_acyclic(self) -> None:
        """Refuse a cycle, so walking the graph always terminates.

        v0 declares the graph acyclic. A cycle would let a program revisit a
        step, and a revisited step re-issues an action id — the same id the
        Gateway already used once, so the program's own identity would stop
        meaning "exactly once".
        """
        indegree = {node.node_id: 0 for node in self.nodes}
        for edge in self.edges:
            indegree[edge.to_node] += 1
        ready = [node_id for node_id, count in indegree.items() if count == 0]
        settled = 0
        while ready:
            current = ready.pop()
            settled += 1
            for edge in self.edges_from(current):
                indegree[edge.to_node] -= 1
                if indegree[edge.to_node] == 0:
                    ready.append(edge.to_node)
        if settled != len(self.nodes):
            raise IrControlFlowError(
                f"control flow contains a cycle among {len(self.nodes) - settled} "
                f"nodes; v0 requires an acyclic graph"
            )

    def _validate_guards(self) -> None:
        """A branch must be a decision the facts can actually make.

        Two rules, both provable without running anything. A condition node reads
        exactly one fact, so its outgoing guards must all name that same fact —
        otherwise the node is a compound condition in disguise, and the honest
        form of that is a chain of condition nodes. And no two guards may be
        identical, because a fact that satisfies two guards has made no choice at
        all.

        Exhaustive partitioning of a fact's domain is deliberately *not* decided
        here: that is a decision procedure, not a structural check. It is
        enforced at run time instead, where the facts are known and a program
        that selected zero or many edges fails closed.
        """
        for node in self.nodes:
            if node.kind is not ControlNodeKind.CONDITION:
                continue
            outgoing = self.edges_from(node.node_id)
            guards = [edge.when for edge in outgoing if edge.when is not None]
            # Compared by value, not collected in a set: the subject is a model,
            # and a program's identity should depend on what it compares, not on
            # whether its representation happens to be hashable.
            subjects: list[ConditionSubject] = []
            for guard in guards:
                if not any(guard.subject == seen for seen in subjects):
                    subjects.append(guard.subject)
            if len(subjects) > 1:
                raise IrControlFlowError(
                    f"condition node {node.node_id!r} guards on "
                    f"{len(subjects)} different facts; a branch reads one fact and "
                    f"chains through further condition nodes"
                )
            for index, guard in enumerate(guards):
                if guard in guards[index + 1 :]:
                    raise IrControlFlowError(
                        f"condition node {node.node_id!r} has two edges guarded by the "
                        f"same condition {guard.condition_id!r}; a fact satisfying it "
                        f"selects neither branch"
                    )

    def _validate_action_nodes(self) -> None:
        """Every planned step appears exactly once as an action node."""
        orders = [
            node.step_order
            for node in self.nodes
            if node.kind is ControlNodeKind.ACTION and node.step_order is not None
        ]
        planned = [step.order for step in self.plan.steps]
        if sorted(orders) != sorted(planned):
            raise IrControlFlowError(
                f"control flow executes steps {sorted(orders)}, but the plan is "
                f"{planned}; the graph must account for every step exactly once"
            )

    def _validate_condition_sources(self) -> None:
        """A condition may only read a fact that already exists where it is read.

        ``cursor``, ``steps_used``, and ``task_state`` are always available. An
        observation is not: it exists only once its step has run, so a condition
        that reads one has to sit downstream of the step that produces it.
        """
        for node in self.nodes:
            if node.condition is None or node.condition.subject.kind is not FactKind.OBSERVATION:
                continue
            step_order = node.condition.subject.step_order
            if step_order is None:  # pragma: no cover - the subject guarantees this
                raise IrConditionError(
                    f"condition {node.condition.condition_id!r} names an observation "
                    f"without a step"
                )
            if self.step(step_order) is None:
                raise IrConditionError(
                    f"condition {node.condition.condition_id!r} reads the observation "
                    f"for step {step_order}, which is not in this plan"
                )
            source = self.action_node_for_step(step_order)
            assert source is not None  # guaranteed by _validate_action_nodes
            if not self._reaches(source.node_id, node.node_id):
                raise IrConditionError(
                    f"condition {node.condition.condition_id!r} reads the observation "
                    f"for step {step_order}, which is not upstream of node "
                    f"{node.node_id!r}; the fact cannot exist yet"
                )

    def _reaches(self, source: str, target: str) -> bool:
        """Whether ``target`` is downstream of ``source``, searching backwards.

        A backward search from the reader is what makes the check honest: it asks
        whether every path to the condition passed through the step, rather than
        whether *some* path did.
        """
        seen = {target}
        frontier = [target]
        while frontier:
            current = frontier.pop()
            if current == source:
                return True
            for edge in self.edges:
                if edge.to_node == current and edge.from_node not in seen:
                    seen.add(edge.from_node)
                    frontier.append(edge.from_node)
        return False

    def permission_for(self, capability: str) -> PermissionNode | None:
        """The declared permission for ``capability``, if the program has one."""
        for permission in self.permissions:
            if permission.requires_capability == capability:
                return permission
        return None

    def record(self, observation: ObservationNode) -> ObservationNode:
        """Append one observation to this program's durable record."""
        self.observations.append(observation)
        return observation

    def to_json(self) -> str:
        """The full program, canonical form included, for storage or audit."""
        return canonical_json(
            {
                **self.model_dump(mode="json"),
                "canonical": self.canonical_form(),
            }
        )


def require_valid(program: KaiProgram) -> KaiProgram:
    """Return ``program`` only if it is executable; otherwise refuse it.

    Spec §2.1: a model may *propose* IR, but only validated IR may be executed.
    The one call site that matters is the Gateway boundary, where a program
    crosses from being a description into being something about to run.
    """
    program.validate_executable()
    return program


__all__ = [
    "ActionNode",
    "ControlEdge",
    "ControlNode",
    "KaiProgram",
    "StepNode",
    "canonical_fingerprint",
    "canonical_json",
    "require_valid",
]
