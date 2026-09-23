"""Deterministic boundary implementations for the MVP.

These are *reference* implementations: they make explicit, reproducible
decisions so the gateway's ordering and stop conditions can be verified in
tests. Production engines (real policy, human approval queues, content
classifiers) swap in behind the same protocols without touching the gateway.

Rigor properties the stubs guarantee:

* ``ProductPolicyEngine`` refuses CRITICAL tools (the MVP catalog ships no
  CRITICAL capability) and refuses tools the task contract did not permit.
* ``ApprovalManagerStub`` behaves exactly as configured (APPROVED/REQUIRED/
  DENIED) and records every consult for audit/tests.
* ``ContentAuthorizerStub`` allows or denies exactly as configured; it never
  silently lets anything through.
* ``ContractVerificationEngine`` enforces that an execution may only be
  reported ``verified`` when the tool's declared verification contract was
  honored; a read contract with no evidence is treated as not verified.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from kai.tasks.models import Task
from kai.tools.boundaries import (
    ApprovalDecision,
    ApprovalDecisionRecord,
    ContentAuthorization,
    PolicyDecision,
    VerificationOutcome,
)
from kai.tools.context import ToolContext
from kai.tools.definition import ToolDefinition
from kai.tools.execution import (
    ToolExecutionOutcome,
    VerificationContract,
    VerificationMismatch,
)
from kai.tools.intent import ToolIntent
from kai.tools.risk import RiskLevel

__all__ = [
    "ApprovalManagerStub",
    "ContentAuthorizerStub",
    "ContractVerificationEngine",
    "ProductPolicyEngine",
]


class ProductPolicyEngine:
    """Deterministic MVP policy: CRITICAL is never allowed and the task
    contract always wins on permitted capability. No inference, no heuristics."""

    def __init__(self) -> None:
        self.decisions: list[PolicyDecision] = []

    def _task_allows(self, tool: ToolDefinition, task: Task) -> bool:
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

    async def authorize(
        self,
        *,
        intent: ToolIntent,
        context: ToolContext,
        tool: ToolDefinition,
        task: Task,
        arguments: Mapping[str, Any],
    ) -> PolicyDecision:
        if tool.risk_level is RiskLevel.CRITICAL:
            decision = PolicyDecision(
                False, "CRITICAL tools are not part of the MVP tool catalog"
            )
        elif not self._task_allows(tool, task):
            decision = PolicyDecision(
                False,
                f"task contract does not permit capability {tool.capability!r}",
            )
        else:
            decision = PolicyDecision(True, "permitted by MVP policy")
        self.decisions.append(decision)
        return decision


class ContentAuthorizerStub:
    """Allows/denies exactly what it is told to, recording every consult."""

    def __init__(self, *, allowed: bool = True) -> None:
        self.allowed = allowed
        self.calls: list[ContentAuthorization] = []

    async def authorize(
        self,
        *,
        intent: ToolIntent,
        context: ToolContext,
        tool: ToolDefinition,
        task: Task,
        arguments: Mapping[str, Any],
    ) -> ContentAuthorization:
        decision = ContentAuthorization(self.allowed, "stub content policy")
        self.calls.append(decision)
        return decision


class ApprovalManagerStub:
    """Returns the configured decision; never invents one on its own."""

    def __init__(
        self,
        *,
        decision: ApprovalDecision = ApprovalDecision.APPROVED,
        decided_by: str = "stub.approval_manager",
    ) -> None:
        self.decision = decision
        self.decided_by = decided_by
        self.decisions: list[ApprovalDecisionRecord] = []

    async def decide(
        self,
        *,
        intent: ToolIntent,
        context: ToolContext,
        tool: ToolDefinition,
        task: Task,
        arguments: Mapping[str, Any],
    ) -> ApprovalDecisionRecord:
        record = ApprovalDecisionRecord(
            decision=self.decision,
            reason="stub approval policy",
            decided_by=self.decided_by,
        )
        self.decisions.append(record)
        return record


class ContractVerificationEngine:
    """Refuses to report an execution verified unless the tool's declared
    contract was satisfied. This is the boundary's privacy check: the adapter
    may *claim* a read, but the engine re-asserts the contract held.

    Rules (all deterministic):

    * MANUAL never verifies: a human review must happen first.
    * A create/send tool claiming verified=True must carry the provider id
      (``event_id`` / ``message_id``) that the read-after-* actually used.
    * A read tool claiming verified=True must carry some provider evidence
      (a search with refs, an availability summary, matched entities, ...).
    * When the adapter itself reports ``verified=False``, its concrete
      mismatches are surfaced as-is — the engine never invents praise.
    """

    async def verify(
        self,
        *,
        tool: ToolDefinition,
        execution: ToolExecutionOutcome,
        context: ToolContext,
    ) -> VerificationOutcome:
        contract = tool.verification_contract

        if contract is VerificationContract.MANUAL:
            return VerificationOutcome(
                verified=False,
                evidence={"contract": contract.value},
                mismatches=(
                    VerificationMismatch(
                        field="verification",
                        expected="manual human verification",
                        actual="no human review yet",
                    ),
                ),
            )

        if execution.verified:
            missing = _missing_contract_evidence(contract, execution.evidence)
            if missing:
                return VerificationOutcome(
                    verified=False,
                    evidence={"contract": contract.value},
                    mismatches=(
                        VerificationMismatch(
                            field="evidence",
                            expected=f"{contract.value} proof ({', '.join(missing)})",
                            actual="no provider evidence in execution outcome",
                        ),
                    ),
                )

        return VerificationOutcome(
            verified=execution.verified,
            evidence={"contract": contract.value, **execution.evidence},
            mismatches=execution.mismatches,
        )


def _missing_contract_evidence(
    contract: VerificationContract, evidence: Mapping[str, Any]
) -> tuple[str, ...]:
    if contract is VerificationContract.READ_AFTER_CREATE:
        return () if "event_id" in evidence else ("event_id",)
    if contract is VerificationContract.READ_AFTER_SEND:
        return () if "message_id" in evidence else ("message_id",)
    # READ_BACKED: verified reads must carry at least some provider evidence.
    return () if evidence else ("provider_evidence",)