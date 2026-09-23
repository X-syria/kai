"""The four evaluator boundaries of the Tool Gateway.

These are *interfaces*, not implementations. The MVP ships deterministic
stubs (``kai.tools.stubs``) so behavior is reproducible in tests; production
implementations (real policy engine, approval manager backed by user queues,
content classifiers) can be plugged in without changing the gateway.

Each boundary makes an explicit decision and is recorded by the gateway for
audit purposes. Nothing in a boundary can change the registry or the tool
definition.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from kai.tasks.models import Task
    from kai.tools.definition import ToolDefinition
    from kai.tools.execution import ToolExecutionOutcome, VerificationMismatch
    from kai.tools.intent import ToolIntent

from kai.tools.context import ToolContext


@dataclass(frozen=True)
class PolicyDecision:
    """Outcome of the policy boundary."""

    allowed: bool
    reason: str


@dataclass(frozen=True)
class ContentAuthorization:
    """Outcome of the content authorization boundary."""

    allowed: bool
    reason: str


class ApprovalDecision(StrEnum):
    """What the approval manager says about this invocation."""

    APPROVED = "APPROVED"
    REQUIRED = "REQUIRED"
    DENIED = "DENIED"


@dataclass(frozen=True)
class ApprovalDecisionRecord:
    """Full approval verdict, including who decided it."""

    decision: ApprovalDecision
    reason: str
    decided_by: str

    def require_execution_stop_pending_user(self) -> bool:
        return self.decision is ApprovalDecision.REQUIRED


@dataclass(frozen=True)
class VerificationOutcome:
    """Verdict of the verification boundary on top of a raw execution."""

    verified: bool
    evidence: Mapping[str, Any] = field(default_factory=dict)
    mismatches: tuple[VerificationMismatch, ...] = field(default_factory=tuple)


class PolicyEngine(Protocol):
    """Decides whether the invocation may happen at all."""

    async def authorize(
        self,
        *,
        intent: ToolIntent,
        context: ToolContext,
        tool: ToolDefinition,
        task: Task,
        arguments: Mapping[str, Any],
    ) -> PolicyDecision: ...


class ContentAuthorizer(Protocol):
    """Decides whether the *content* of the arguments is acceptable."""

    async def authorize(
        self,
        *,
        intent: ToolIntent,
        context: ToolContext,
        tool: ToolDefinition,
        task: Task,
        arguments: Mapping[str, Any],
    ) -> ContentAuthorization: ...


class ApprovalManager(Protocol):
    """Decides whether a human approval step must block execution."""

    async def decide(
        self,
        *,
        intent: ToolIntent,
        context: ToolContext,
        tool: ToolDefinition,
        task: Task,
        arguments: Mapping[str, Any],
    ) -> ApprovalDecisionRecord: ...


class VerificationEngine(Protocol):
    """Decides whether a raw execution outcome may be trusted."""

    async def verify(
        self,
        *,
        tool: ToolDefinition,
        execution: ToolExecutionOutcome,
        context: ToolContext,
    ) -> VerificationOutcome: ...


__all__ = [
    "ApprovalDecision",
    "ApprovalDecisionRecord",
    "ApprovalManager",
    "ContentAuthorization",
    "ContentAuthorizer",
    "PolicyDecision",
    "PolicyEngine",
    "VerificationEngine",
    "VerificationOutcome",
]