"""The deterministic Policy Engine.

``PolicyEngine.evaluate`` runs a fixed rule sequence on a trusted
``PolicyInput`` and stops at the first non-ALLOW rule. Precedence is therefore
structural: an earlier DENY can never be overruled by a later ALLOW, and a
REQUIRE_APPROVAL can never be downgraded by the rules that come after it.

The engine keeps a ``decisions`` list (one entry per evaluation) so callers,
tests, and the audit trail can see the exact deterministic path that was
taken.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Final

from kai.policy import rules
from kai.policy.models import PolicyDecision, PolicyInput

#: Fixed, ordered rule pipeline. Ordering is load-bearing: contract and
#: identity-prerequisite failures (DENY) are decided before any approval
#: consideration so a REQUIRE_APPROVAL can never mask a hard deny.
_RULE_SEQUENCE: Final[
    tuple[tuple[str, Callable[[PolicyInput], PolicyDecision | None]], ...]
] = (
    ("binding", rules.rule_binding),
    ("task_state", rules.rule_task_state),
    ("allowed_tools", rules.rule_allowed_tools),
    ("critical_tool", rules.rule_critical_tool),
    ("risk_policy", rules.rule_risk_policy),
    ("deadline", rules.rule_deadline),
    ("step_budget", rules.rule_step_budget),
    ("cost_budget", rules.rule_cost_budget),
    ("identity", rules.rule_identity),
    ("risk_approval", rules.rule_risk_approval),
)


class PolicyEngine:
    """The single production policy implementation for the MVP slice.

    Interpretable, auditable, and sealed: the rule sequence is a module-level
    constant; nothing at runtime can add or reorder rules.
    """

    def __init__(self) -> None:
        self.decisions: list[PolicyDecision] = []

    async def evaluate(self, *, input: PolicyInput) -> PolicyDecision:
        """Evaluate one invocation and return the first conclusive verdict."""
        matched: list[str] = []
        for name, rule in _RULE_SEQUENCE:
            matched.append(name)
            decision = rule(input)
            if decision is not None:
                finalized = replace(decision, matched_rules=tuple(matched))
                self.decisions.append(finalized)
                return finalized

        decision = replace(
            rules.allow_decision(input),
            matched_rules=tuple(matched),
        )
        self.decisions.append(decision)
        return decision

    @staticmethod
    def rule_names() -> tuple[str, ...]:
        """The sealed evaluation order, for documentation and tests."""
        return tuple(name for name, _ in _RULE_SEQUENCE)


__all__ = ["PolicyEngine"]