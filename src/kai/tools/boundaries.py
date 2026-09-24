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

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

from kai.policy.models import PolicyDecision, PolicyInput

if TYPE_CHECKING:
    from kai.identity.models import IdentityResult
    from kai.identity.schemas import IdentityQuery
    from kai.tasks.models import Task
    from kai.tools.definition import ToolDefinition
    from kai.tools.execution import ToolExecutionOutcome, VerificationMismatch
    from kai.tools.intent import ToolIntent

from kai.tools.context import ToolContext


@dataclass(frozen=True)
class ContentAuthorization:
    """Outcome of the content authorization boundary.

    ``escalate`` marks verdicts a human must look at before any execution;
    ``allowed=False, escalate=False`` is a hard deny. The message body is never
    part of the verdict — content decisions come from the trusted context.
    """

    allowed: bool
    reason: str
    escalate: bool = False

    def __str__(self) -> str:
        return self.reason


class ApprovalDecision(StrEnum):
    """What the approval manager says about this invocation.

    APPROVED is the only verdict that may proceed; REQUIRED means a PENDING
    request is open, DENIED/EXPIRED/STALE all mean "do not execute".
    """

    APPROVED = "APPROVED"
    REQUIRED = "REQUIRED"
    DENIED = "DENIED"
    EXPIRED = "EXPIRED"
    STALE = "STALE"


@dataclass(frozen=True)
class ApprovalDecisionRecord:
    """Full approval verdict, including who decided it.

    ``approval_id`` names the exact persisted approval behind the verdict so
    the gateway (or the runtime) can act on it later.
    """

    decision: ApprovalDecision
    reason: str
    decided_by: str
    approval_id: uuid.UUID | None = None

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

    async def evaluate(
        self, *, input: PolicyInput
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
    """Decides whether a human approval step must block execution.

    ``check`` evaluates one invocation against the persisted approval manager;
    ``consume`` records the single use of an APPROVED approval right before an
    execution is authorized to proceed.
    """

    async def check(
        self,
        *,
        intent: ToolIntent,
        context: ToolContext,
        tool: ToolDefinition,
        task: Task,
        arguments: Mapping[str, Any],
    ) -> ApprovalDecisionRecord: ...

    async def consume(
        self,
        *,
        approval_id: uuid.UUID,
        intent: ToolIntent,
        context: ToolContext,
        tool: ToolDefinition,
        task: Task,
        arguments: Mapping[str, Any],
    ) -> None: ...


class IdentityResolver(Protocol):
    """Tells the gateway *who* an invocation is about (never what is allowed).

    ``resolve`` derives an ``IdentityResult`` deterministically from trusted
    sources scoped to ``context``. The result is a verdict about identity;
    ALLOW/DENY continues to belong to the Policy Engine alone.
    """

    async def resolve(
        self,
        *,
        query: IdentityQuery,
        context: ToolContext,
    ) -> IdentityResult: ...


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
    "IdentityResolver",
    "PolicyDecision",
    "PolicyEngine",
    "PolicyInput",
    "VerificationEngine",
    "VerificationOutcome",
]