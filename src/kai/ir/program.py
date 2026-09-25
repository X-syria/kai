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

from kai.ir.errors import IrValidationError
from kai.ir.nodes import (
    IR_VERSION,
    ActionNode,
    IntentNode,
    ObservationNode,
    PermissionNode,
    PlanNode,
    PolicyConstraintsNode,
    StateSnapshotNode,
    StepNode,
)


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
    """One KAI-IR program: an intent, the plan it runs, and its constraints.

    A program is a *derived, deterministic* value. It is not persisted and owns
    no table: it is rebuilt from the task contract, the persisted plan, and the
    execution row, and the same inputs always produce the same
    :attr:`fingerprint`. That is what makes a plan auditable and replayable
    without any new storage (spec §3, §5).

    ``observations`` and ``result`` are populated as execution proceeds. They are
    excluded from the fingerprint on purpose — a program's identity is what it
    intends to do, not what happened when it ran.
    """

    model_config = ConfigDict(extra="forbid")

    ir_version: str = IR_VERSION
    program_id: UUID
    intent: IntentNode
    plan: PlanNode
    constraints: PolicyConstraintsNode
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
        """The hashed content: intent, plan, constraints, permissions.

        Declaration order of steps and permissions is part of the canonical form
        (spec §11, resolved for v0), so it is emitted exactly as declared and not
        re-sorted here.
        """
        return {
            "ir_version": self.ir_version,
            "intent": self.intent.model_dump(mode="json"),
            "plan": self.plan.model_dump(mode="json"),
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
    "KaiProgram",
    "StepNode",
    "canonical_fingerprint",
    "canonical_json",
    "require_valid",
]
