"""Deterministic policy rules.

Every rule is a pure function of a trusted ``PolicyInput``. A rule returns
``None`` when it passes (evaluation continues) or a final ``PolicyDecision``
when it fails (evaluation stops). Rules never read ``input.arguments`` — the
untrusted surface — and never consult an LLM or random source: identical input
always yields identical output.

The evaluation order lives in :mod:`kai.policy.engine`; rules are intentionally
kept ignorant of their siblings so precedence cannot leak between them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

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
)
from kai.policy.models import (
    IdentityStatus,
    PolicyDecision,
    PolicyDecisionKind,
    PolicyInput,
)
from kai.tools.risk import RiskLevel

if TYPE_CHECKING:
    from kai.tasks.models import Task
    from kai.tools.definition import ToolDefinition

#: States in which a tool execution may happen at all. The Task State Machine
#: (kept outside this package) owns every transition; the engine only consults
#: the current state and refuses anything else.
_EXECUTION_STATES: frozenset[str] = frozenset({"EXECUTING"})

#: Risk levels whose ordinary handling is "ask a human first".
_APPROVAL_BY_RISK: frozenset[RiskLevel] = frozenset(
    {RiskLevel.MEDIUM, RiskLevel.HIGH}
)


def rule_binding(input: PolicyInput) -> PolicyDecision | None:
    """The task in the input must belong to the trusted (tenant, user)."""
    if (
        input.task.tenant_id != input.tenant_id
        or input.task.user_id != input.user_id
    ):
        return PolicyDecision(
            decision=PolicyDecisionKind.DENY,
            reason_code=BINDING_MISMATCH,
            reason="task does not belong to the trusted (tenant, user) context",
            risk_level=input.risk_level,
        )
    return None


def rule_task_state(input: PolicyInput) -> PolicyDecision | None:
    """An intent may only execute while the task is actually executing."""
    if input.task.state not in _EXECUTION_STATES:
        return PolicyDecision(
            decision=PolicyDecisionKind.DENY,
            reason_code=INVALID_TASK_STATE,
            reason=(
                f"tool execution requires task state EXECUTING, "
                f"not {input.task.state!r}"
            ),
            risk_level=input.risk_level,
        )
    return None


def rule_allowed_tools(input: PolicyInput) -> PolicyDecision | None:
    """The task contract's allowed_tools always wins over the intent's name."""
    if not _task_allows(input.tool, input.task):
        return PolicyDecision(
            decision=PolicyDecisionKind.DENY,
            reason_code=TOOL_NOT_ALLOWED,
            reason=(
                f"task contract does not permit capability "
                f"{input.tool.capability!r}"
            ),
            risk_level=input.risk_level,
        )
    return None


def rule_critical_tool(input: PolicyInput) -> PolicyDecision | None:
    """CRITICAL capabilities exist outside the MVP catalog and are refused."""
    if input.tool.risk_level is RiskLevel.CRITICAL:
        return PolicyDecision(
            decision=PolicyDecisionKind.DENY,
            reason_code=CRITICAL_TOOL,
            reason="CRITICAL tools are not part of the MVP tool catalog",
            risk_level=input.risk_level,
        )
    return None


def rule_risk_policy(input: PolicyInput) -> PolicyDecision | None:
    """The task's risk_policy caps the tool's declared risk; a task set to
    LOW can never silently invoke a MEDIUM tool."""
    cap = _task_risk_policy(input.task)
    if input.tool.risk_level.rank > cap.rank:
        return PolicyDecision(
            decision=PolicyDecisionKind.REQUIRE_APPROVAL,
            reason_code=RISK_POLICY_CAP,
            reason=(
                f"tool risk {input.tool.risk_level.value} exceeds the task's "
                f"risk policy {cap.value}"
            ),
            risk_level=input.risk_level,
        )
    return None


def rule_deadline(input: PolicyInput) -> PolicyDecision | None:
    """An expired task deadline means the invocation is too late, period."""
    deadline = input.task.deadline
    if deadline is None:
        return None
    now = input.now if input.now is not None else datetime.now(UTC)
    if now > deadline:
        return PolicyDecision(
            decision=PolicyDecisionKind.DENY,
            reason_code=DEADLINE_EXPIRED,
            reason=f"task deadline {deadline.isoformat()} has passed",
            risk_level=input.risk_level,
        )
    return None


def rule_step_budget(input: PolicyInput) -> PolicyDecision | None:
    """``steps_used`` reaching the contract's max_steps closes the budget."""
    if input.steps_used >= input.task.max_steps:
        return PolicyDecision(
            decision=PolicyDecisionKind.DENY,
            reason_code=STEP_BUDGET_EXHAUSTED,
            reason=(
                f"step budget exhausted "
                f"({input.steps_used}/{input.task.max_steps})"
            ),
            risk_level=input.risk_level,
        )
    return None


def rule_cost_budget(input: PolicyInput) -> PolicyDecision | None:
    """An estimated cost over the contract cap is refused (capped = no rule)."""
    cap = input.task.max_cost
    if cap > 0 and input.estimated_cost > cap:
        return PolicyDecision(
            decision=PolicyDecisionKind.DENY,
            reason_code=COST_BUDGET_EXCEEDED,
            reason=(
                f"estimated cost {input.estimated_cost} exceeds the task's "
                f"max_cost {cap}"
            ),
            risk_level=input.risk_level,
        )
    return None


def rule_identity(input: PolicyInput) -> PolicyDecision | None:
    """Recipient identity is enforced deterministically at MEDIUM+ risk.

    VERIFIED passes through; CONFLICTING is a hard deny (the trusted sources
    disagree); UNKNOWN escalates because nobody can stand behind the action.
    Below MEDIUM there is nothing yet to be careful about.
    """
    identity = input.recipient_identity
    if identity is None or input.risk_level.rank < RiskLevel.MEDIUM.rank:
        return None
    if identity is IdentityStatus.CONFLICTING:
        return PolicyDecision(
            decision=PolicyDecisionKind.DENY,
            reason_code=IDENTITY_CONFLICTING,
            reason="recipient identity is conflicting; cannot authorize the action",
            risk_level=input.risk_level,
        )
    if identity is IdentityStatus.UNKNOWN:
        return PolicyDecision(
            decision=PolicyDecisionKind.REQUIRE_APPROVAL,
            reason_code=IDENTITY_UNKNOWN,
            reason="recipient identity is unknown; escalate for human review",
            risk_level=input.risk_level,
        )
    return None


def rule_risk_approval(input: PolicyInput) -> PolicyDecision | None:
    """Ordinary risk handling: MEDIUM/HIGH need a human approval step."""
    if input.risk_level in _APPROVAL_BY_RISK:
        return PolicyDecision(
            decision=PolicyDecisionKind.REQUIRE_APPROVAL,
            reason_code=APPROVAL_REQUIRED_BY_RISK,
            reason=f"risk level {input.risk_level.value} requires human approval",
            risk_level=input.risk_level,
        )
    return None


def allow_decision(input: PolicyInput) -> PolicyDecision:
    """The single ALLOW verdict the engine can emit, reached only when every
    rule in the sequence passed."""
    return PolicyDecision(
        decision=PolicyDecisionKind.ALLOW,
        reason_code=ALLOWED,
        reason="allowed by KAI policy",
        risk_level=input.risk_level,
    )


def _task_allows(tool: ToolDefinition, task: Task) -> bool:
    """Whether ``task.allowed_tools`` mentions the tool.

    An empty list is a contract without a capability fixture and permits any
    application-registered tool. Otherwise the list is compared by capability
    prefix or exact tool/capability name, mirroring the old stub semantics.
    """
    allowed = task.allowed_tools or ()
    if not allowed:
        return True
    for permitted in allowed:
        if permitted == tool.capability or permitted == tool.name:
            return True
        if permitted.endswith(".") and tool.capability.startswith(permitted):
            return True
        if tool.name.startswith(f"{permitted}."):
            return True
    return False


def _task_risk_policy(task: Task) -> RiskLevel:
    """The task's declared risk_policy as the shared RiskLevel vocabulary."""
    try:
        return RiskLevel(task.risk_policy)
    except ValueError:
        # A task must persist a valid risk policy; anything else is a raise.
        raise ValueError(f"task has an unknown risk_policy {task.risk_policy!r}") from None


__all__ = [
    "allow_decision",
    "rule_allowed_tools",
    "rule_binding",
    "rule_cost_budget",
    "rule_critical_tool",
    "rule_deadline",
    "rule_identity",
    "rule_risk_approval",
    "rule_risk_policy",
    "rule_step_budget",
    "rule_task_state",
]