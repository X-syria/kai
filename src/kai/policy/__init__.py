"""KAI Policy Engine (deterministic authorization).

The engine turns a trusted :class:`~kai.policy.models.PolicyInput` into one of
three verdicts — ALLOW, DENY, REQUIRE_APPROVAL — using a fixed, sealed rule
sequence. Model output is never the security decision; no LLM, no heuristics,
no booleans. Content authorization lives in
:class:`~kai.policy.content.ScheduleContentAuthorizer` as a separate boundary.
"""

from kai.policy.content import CONTENT_CATEGORY_KEY, ScheduleContentAuthorizer
from kai.policy.engine import PolicyEngine
from kai.policy.errors import (
    ALLOWED,
    APPROVAL_REQUIRED_BY_RISK,
    BINDING_MISMATCH,
    COST_BUDGET_EXCEEDED,
    CRITICAL_TOOL,
    DEADLINE_EXPIRED,
    IDENTITY_CONFLICTING,
    IDENTITY_UNKNOWN,
    INVALID_TASK_STATE,
    RISK_POLICY_CAP,
    STEP_BUDGET_EXHAUSTED,
    TOOL_NOT_ALLOWED,
    PolicyError,
    PolicyInputError,
)
from kai.policy.models import (
    IdentityStatus,
    PolicyDecision,
    PolicyDecisionKind,
    PolicyInput,
)
from kai.policy.schemas import (
    CATEGORY_MODE,
    ContentCategory,
    ContentMode,
)

__all__ = [
    "ALLOWED",
    "APPROVAL_REQUIRED_BY_RISK",
    "BINDING_MISMATCH",
    "CATEGORY_MODE",
    "CONTENT_CATEGORY_KEY",
    "COST_BUDGET_EXCEEDED",
    "CRITICAL_TOOL",
    "ContentCategory",
    "ContentMode",
    "DEADLINE_EXPIRED",
    "IDENTITY_CONFLICTING",
    "IDENTITY_UNKNOWN",
    "INVALID_TASK_STATE",
    "IdentityStatus",
    "PolicyDecision",
    "PolicyDecisionKind",
    "PolicyEngine",
    "PolicyError",
    "PolicyInput",
    "PolicyInputError",
    "RISK_POLICY_CAP",
    "STEP_BUDGET_EXHAUSTED",
    "ScheduleContentAuthorizer",
    "TOOL_NOT_ALLOWED",
]