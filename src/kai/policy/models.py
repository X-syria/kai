"""Policy decision vocabulary for the KAI Policy Engine.

The engine never exposes a raw boolean: every evaluation ends in exactly one of
three mutually-exclusive kinds (ALLOW / DENY / REQUIRE_APPROVAL) together with
a machine-readable reason code, the rule sequence that ran, and the risk level
the decision was made against. ``PolicyInput`` is the *only* thing the engine
is ever allowed to read; everything on it is trusted (identity from the
authenticated context, contract values from the persisted task row).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any
from uuid import UUID

if TYPE_CHECKING:
    from kai.tasks.models import Task
    from kai.tools.definition import ToolDefinition
    from kai.tools.intent import ToolIntent
    from kai.tools.risk import RiskLevel


class PolicyDecisionKind(StrEnum):
    """The three possible outcomes of one policy evaluation."""

    ALLOW = "ALLOW"
    DENY = "DENY"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"


class IdentityStatus(StrEnum):
    """What the identity resolver vouches for about the recipient.

    UNKNOWN means no verified identity was established; CONFLICTING means the
    trusted sources disagree. Both are escalation-safe states.
    """

    VERIFIED = "VERIFIED"
    UNKNOWN = "UNKNOWN"
    CONFLICTING = "CONFLICTING"


@dataclass(frozen=True)
class PolicyDecision:
    """One evaluation verdict: the outcome plus why and which rules ran.

    ``matched_rules`` names every rule the engine evaluated before reaching
    this verdict (including the rule that produced it), so an audit trail can
    see the whole deterministic path — not just the final answer.
    """

    decision: PolicyDecisionKind
    reason_code: str
    reason: str
    risk_level: RiskLevel
    matched_rules: tuple[str, ...] = ()
    decided_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def allowed(self) -> bool:
        """Convenience for callers that only need ALLOW vs anything else."""
        return self.decision is PolicyDecisionKind.ALLOW

    @property
    def deny(self) -> bool:
        return self.decision is PolicyDecisionKind.DENY

    @property
    def requires_approval(self) -> bool:
        return self.decision is PolicyDecisionKind.REQUIRE_APPROVAL

    def __str__(self) -> str:
        return self.reason

    def to_audit(self) -> dict[str, Any]:
        """Safe metadata for audit trails; never carries arguments or content."""
        return {
            "decision": self.decision.value,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "risk_level": self.risk_level.value,
            "matched_rules": list(self.matched_rules),
            "decided_at": self.decided_at.isoformat(),
        }


@dataclass(frozen=True)
class PolicyInput:
    """Everything the engine is allowed to look at.

    Identity fields come from the trusted ``ToolContext``; contract values come
    from the persisted task row. ``arguments`` is carried so the input's full
    shape is visible, but the engine NEVER reads it — an argument such as
    ``{"tenant_id": "other-tenant"}`` can never override context, contract, or
    risk, and ``content_category`` is informational only (content authorization
    happens in its own boundary). ``now`` is an injectable application clock.
    """

    tenant_id: UUID
    user_id: UUID
    task_id: UUID
    task: Task
    tool: ToolDefinition
    intent: ToolIntent
    risk_level: RiskLevel
    account_id: UUID | None = None
    arguments: Mapping[str, Any] = field(default_factory=dict)
    steps_used: int = 0
    estimated_cost: float = 0.0
    now: datetime | None = None
    recipient_identity: IdentityStatus | None = None
    recipient_class: str | None = None
    content_category: str | None = None


__all__ = ["IdentityStatus", "PolicyDecision", "PolicyDecisionKind", "PolicyInput"]