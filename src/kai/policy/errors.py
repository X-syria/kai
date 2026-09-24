"""Policy Engine error vocabulary and machine-readable reason codes.

Reason codes are *decision*, not log, artifacts: the engine stamps each
``PolicyDecision`` with exactly one code so callers and audit trails can
refer to the deterministic rule that produced it without parsing prose.
"""

from __future__ import annotations

from typing import Final

# --- reason codes ----------------------------------------------------------
# Binding: the task passed to the engine is not the (tenant, user) the trusted
# context vouches for.
BINDING_MISMATCH: Final[str] = "BINDING_MISMATCH"
# The task's state does not permit tool execution (see kai.tasks.state).
INVALID_TASK_STATE: Final[str] = "INVALID_TASK_STATE"
# The tool is valid but the task contract's allowed_tools does not include it.
TOOL_NOT_ALLOWED: Final[str] = "TOOL_NOT_ALLOWED"
# The task's risk_policy is below the tool's declared risk (no silent downgrade).
RISK_POLICY_CAP: Final[str] = "RISK_POLICY_CAP"
# CRITICAL tools are not part of the MVP catalog.
CRITICAL_TOOL: Final[str] = "CRITICAL_TOOL"
# The task deadline has passed.
DEADLINE_EXPIRED: Final[str] = "DEADLINE_EXPIRED"
# The step budget is exhausted.
STEP_BUDGET_EXHAUSTED: Final[str] = "STEP_BUDGET_EXHAUSTED"
# The estimated cost exceeds the task's max_cost.
COST_BUDGET_EXCEEDED: Final[str] = "COST_BUDGET_EXCEEDED"
# The recipient identity is conflicting; the action cannot be authorized.
IDENTITY_CONFLICTING: Final[str] = "IDENTITY_CONFLICTING"
# The recipient identity is unknown; the action must be escalated.
IDENTITY_UNKNOWN: Final[str] = "IDENTITY_UNKNOWN"
# The tool's risk level demands a human approval step.
APPROVAL_REQUIRED_BY_RISK: Final[str] = "APPROVAL_REQUIRED_BY_RISK"
# All deterministic rules passed.
ALLOWED: Final[str] = "ALLOWED"


class PolicyError(ValueError):
    """Base error for the Policy Engine domain."""


class PolicyInputError(PolicyError):
    """Raised when a caller supplies malformed policy input (never by the LLM)."""


__all__ = [
    "ALLOWED",
    "APPROVAL_REQUIRED_BY_RISK",
    "BINDING_MISMATCH",
    "COST_BUDGET_EXCEEDED",
    "CRITICAL_TOOL",
    "DEADLINE_EXPIRED",
    "IDENTITY_CONFLICTING",
    "IDENTITY_UNKNOWN",
    "INVALID_TASK_STATE",
    "RISK_POLICY_CAP",
    "STEP_BUDGET_EXHAUSTED",
    "TOOL_NOT_ALLOWED",
    "PolicyError",
    "PolicyInputError",
]