"""The Tool Gateway: the single entry point that turns an intent into a
verified, bounded execution.

Pipeline (in order, from :mod:`kai.tools` design):

1. ``authenticate`` — resolve the task through ``(task_id, tenant_id, user_id)``
   using the trusted context. Unresolvable => ``REJECTED/TASK_UNRESOLVED``.
2. ``assert identity`` — the intent's identity fields must equal the context's;
   the caller cannot name a tenant/user/task that the context does not vouch for.
3. ``resolve tool`` — registry lookup by name; unknown/variated names are
   refused at the registry, so a tool name can never escape the catalog.
4. ``guard arguments`` — reserved identity/credential keys are structurally
   impossible in arguments (schema level) and re-checked here defensively.
5. ``validate schema`` — the tool's own Pydantic schema with ``extra=forbid``
   rejects malformed, missing, wrong-typed, unknown, oversized values.
6. ``classify risk`` — the tool's declared ``RiskLevel`` (application-set).
7. ``policy boundary`` — the deterministic Policy Engine; DENY stops here
   before anything else runs, REQUIRE_APPROVAL flows to the approval boundary.
8. ``content boundary`` — Denied here stops (no implicit permissions);
   escalated content stops as APPROVAL_REQUIRED.
9. ``approval boundary`` — contacted only when the policy decision is
    REQUIRE_APPROVAL; REQUIRED (PENDING), DENIED, EXPIRED, and STALE all mean
    "do not execute". APPROVED is the only verdict that proceeds.
10. ``account guard`` — tools marked ``requires_account`` need an account_id.
11. ``final policy re-check`` — the current Policy is re-evaluated after every
    other authorization check (content, approval, account) and immediately
    before the approval's single use is consumed; a DENY here still wins and
    never consumes an approval.
12. ``consume single use`` — the APPROVED approval's one executor slot is
    claimed by an atomic compare-and-set (exact intent hash, APPROVED status,
    not expired, not already consumed all in one statement), so concurrent
    duplicate attempts can never both execute.
13. ``execute`` — the registered executor (adapter) talks to the provider.
14. ``verify`` — the tool's declared ``VerificationContract`` is re-asserted by
    the verification boundary; unverified executions are never reported as
    success.

The gateway never mutates the task or the registry; it only produces
``ToolResult`` and logs safe metadata for the audit trail.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

import structlog

from kai.approvals.errors import ApprovalError
from kai.logging import get_logger
from kai.policy.models import IdentityStatus, PolicyDecisionKind, PolicyInput
from kai.tasks.models import Task
from kai.tasks.service import TaskNotFoundError, get_task
from kai.tools.boundaries import (
    ApprovalDecision,
    ApprovalManager,
    ContentAuthorizer,
    PolicyEngine,
    VerificationEngine,
    VerificationOutcome,
)
from kai.tools.context import ToolContext
from kai.tools.definition import ToolDefinition
from kai.tools.errors import (
    ACCOUNT_REQUIRED,
    CONTEXT_MISMATCH,
    RESERVED_ARGUMENT,
    TASK_UNRESOLVED,
    UNKNOWN_TOOL,
    VALIDATION_ERROR,
    ToolContextError,
    ToolContextMismatchError,
    ToolExecutionError,
    ToolNotFoundError,
    ToolReservedArgumentError,
    ToolValidationError,
)
from kai.tools.execution import ToolExecutionOutcome
from kai.tools.intent import RESERVED_ARGUMENTS, ToolIntent
from kai.tools.registry import ToolRegistry
from kai.tools.result import ToolOutcome, ToolResult
from kai.tools.risk import RiskLevel


class ToolGateway:
    """Co-ordinates one verified tool execution, in a fixed order."""

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        policy_engine: PolicyEngine,
        content_authorizer: ContentAuthorizer,
        approval_manager: ApprovalManager,
        verification_engine: VerificationEngine,
        logger: structlog.BoundLogger | None = None,
    ) -> None:
        self.registry = registry
        self.policy_engine = policy_engine
        self.content_authorizer = content_authorizer
        self.approval_manager = approval_manager
        self.verification_engine = verification_engine
        self._log = logger or get_logger("kai.tools.gateway")

    async def execute(self, intent: ToolIntent, context: ToolContext) -> ToolResult:
        """Run the pipeline and always return a structured ``ToolResult``."""
        tool: ToolDefinition | None = None
        result: ToolResult
        approved_approval_id: uuid.UUID | None = None
        try:
            task = await self._authenticate(context)
            self._assert_identity(intent, context)
            self._assert_no_reserved_arguments(intent)
            tool = self._resolve_tool(intent)
            arguments = self._validate_arguments(tool, intent)

            risk = tool.risk_level
            policy = await self.policy_engine.evaluate(
                input=self._policy_input(
                    intent=intent,
                    context=context,
                    tool=tool,
                    task=task,
                    arguments=arguments,
                    risk=risk,
                )
            )
            if policy.decision is PolicyDecisionKind.DENY:
                return self._denied(intent, tool, risk, policy)

            content = await self.content_authorizer.authorize(
                intent=intent, context=context, tool=tool, task=task, arguments=arguments
            )
            if not content.allowed and content.escalate:
                return self._approval_required(intent, tool, risk, content)
            if not content.allowed:
                return self._denied(intent, tool, risk, content)

            if policy.decision is PolicyDecisionKind.REQUIRE_APPROVAL:
                approval = await self.approval_manager.check(
                    intent=intent,
                    context=context,
                    tool=tool,
                    task=task,
                    arguments=arguments,
                )
                if approval.decision is ApprovalDecision.REQUIRED:
                    return self._approval_required(intent, tool, risk, approval)
                if approval.decision is ApprovalDecision.DENIED:
                    return self._denied(intent, tool, risk, approval)
                if approval.decision is ApprovalDecision.EXPIRED:
                    return self._denied(intent, tool, risk, approval)
                if approval.decision is ApprovalDecision.STALE:
                    return self._denied(intent, tool, risk, approval)
                # APPROVED: exact-intent binding was re-verified by the hash
                # inside the manager's check; the single use is consumed below
                # only once this invocation is actually about to execute.
                approved_approval_id = approval.approval_id

            if tool.requires_account and context.account_id is None:
                return self._attention(
                    intent,
                    tool,
                    risk,
                    ACCOUNT_REQUIRED,
                    "a connected google account is required",
                )

            # Final authorization gate (TOCTOU hardening): the current Policy is
            # re-evaluated *after* every other authorization check (content,
            # approval, account) and immediately before the single use is
            # consumed. DENY always wins here and never consumes an approval.
            final_policy = await self.policy_engine.evaluate(
                input=self._policy_input(
                    intent=intent,
                    context=context,
                    tool=tool,
                    task=task,
                    arguments=arguments,
                    risk=risk,
                )
            )
            if final_policy.decision is PolicyDecisionKind.DENY:
                return self._denied(intent, tool, risk, final_policy)

            if (
                policy.decision is PolicyDecisionKind.REQUIRE_APPROVAL
                and approved_approval_id is not None
            ):
                await self.approval_manager.consume(
                    approval_id=approved_approval_id,
                    intent=intent,
                    context=context,
                    tool=tool,
                    task=task,
                    arguments=arguments,
                )

            execution = await tool.executor.execute(
                tool=tool,
                arguments=arguments,
                context=context,
            )
            verification = await self.verification_engine.verify(
                tool=tool, execution=execution, context=context
            )
            result = self._finalize(intent, tool, risk, execution, verification)
        except ToolContextError as exc:
            result = self._attention(
                intent,
                tool,
                tool.risk_level if tool is not None else RiskLevel.LOW,
                exc.error_code,
                str(exc),
            )
        except ToolContextMismatchError as exc:
            result = self._rejected(intent, tool, CONTEXT_MISMATCH, str(exc))
        except ToolReservedArgumentError as exc:
            result = self._rejected(intent, tool, RESERVED_ARGUMENT, str(exc))
        except ToolNotFoundError as exc:
            result = self._rejected(intent, tool, UNKNOWN_TOOL, str(exc))
        except ToolValidationError as exc:
            result = self._rejected(
                intent, tool, VALIDATION_ERROR, "; ".join(exc.messages)
            )
        except ToolExecutionError as exc:
            result = self._execution_failure(intent, tool, exc)
        except ApprovalError as exc:
            # An approval's single use could not be claimed (already consumed by
            # a concurrent attempt, settled, mismatched intent, or expired).
            # This is a denial of *this* execution, not an internal fault.
            self._log.info(
                "approval single use could not be claimed",
                tool_name=intent.tool_name,
                task_id=str(intent.task_id),
                tenant_id=str(intent.tenant_id),
                user_id=str(intent.user_id),
                request_id=intent.request_id,
                reason=str(exc),
            )
            result = self._denied(
                intent,
                tool,
                tool.risk_level if tool is not None else RiskLevel.LOW,
                exc,
            )
        except Exception as exc:
            self._log.exception(
                "tool gateway unexpected failure",
                tool_name=intent.tool_name,
                task_id=str(intent.task_id),
                tenant_id=str(intent.tenant_id),
                user_id=str(intent.user_id),
                request_id=intent.request_id,
            )
            result = self._execution_failure(
                intent,
                tool,
                ToolExecutionError(str(exc), error_code="INTERNAL_ERROR"),
            )

        self._audit(intent, tool, result)
        return result

    async def _authenticate(self, context: ToolContext) -> Task:
        try:
            return await get_task(
                context.session,
                task_id=context.task_id,
                tenant_id=context.tenant_id,
                user_id=context.user_id,
            )
        except TaskNotFoundError as exc:
            raise ToolContextError(str(exc), error_code=TASK_UNRESOLVED) from exc

    def _assert_identity(self, intent: ToolIntent, context: ToolContext) -> None:
        if (
            intent.task_id != context.task_id
            or intent.tenant_id != context.tenant_id
            or intent.user_id != context.user_id
        ):
            raise ToolContextMismatchError(
                "intent identity (task/tenant/user) does not match the "
                "trusted execution context"
            )

    def _assert_no_reserved_arguments(self, intent: ToolIntent) -> None:
        supplied = RESERVED_ARGUMENTS.intersection(intent.arguments)
        if supplied:
            raise ToolReservedArgumentError(
                f"arguments contain reserved system keys: {sorted(supplied)!r}"
            )

    def _resolve_tool(self, intent: ToolIntent) -> ToolDefinition:
        return self.registry.get(intent.tool_name)

    def _validate_arguments(
        self, tool: ToolDefinition, intent: ToolIntent
    ) -> Mapping[str, Any]:
        try:
            instance = tool.input_schema.model_validate(intent.arguments)
        except Exception as exc:  # pydantic.ValidationError
            messages = self._validation_messages(exc)
            raise ToolValidationError(messages) from exc
        return instance.model_dump()

    def _policy_input(
        self,
        *,
        intent: ToolIntent,
        context: ToolContext,
        tool: ToolDefinition,
        task: Task,
        arguments: Mapping[str, Any],
        risk: RiskLevel,
    ) -> PolicyInput:
        """Assemble the trusted engine input from context + task + tool.

        Step count, cost, and identity come from ``context.metadata`` (trusted
        execution metadata), never from the arguments. ``arguments`` is carried
        only so the input's whole shape is visible; the engine never reads it.
        """
        return PolicyInput(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            task_id=context.task_id,
            account_id=context.account_id,
            task=task,
            tool=tool,
            intent=intent,
            arguments=arguments,
            risk_level=risk,
            steps_used=_metadata_int(context, "steps_used"),
            estimated_cost=_metadata_float(context, "estimated_cost"),
            recipient_identity=_metadata_identity(context),
            content_category=_metadata_str(context, "content_category"),
        )

    def _finalize(
        self,
        intent: ToolIntent,
        tool: ToolDefinition,
        risk: RiskLevel,
        execution: ToolExecutionOutcome,
        verification: VerificationOutcome,
    ) -> ToolResult:
        verified = verification.verified
        if not execution.ok:
            return ToolResult(
                tool_name=tool.name,
                outcome=ToolOutcome.EXECUTION_FAILED,
                ok=False,
                verified=False,
                risk_level=risk,
                error_code=execution.error_code or "EXECUTION_FAILED",
                message="tool execution failed",
                request_id=intent.request_id,
            )
        if verified:
            return ToolResult(
                tool_name=tool.name,
                outcome=ToolOutcome.EXECUTION_SUCCEEDED,
                ok=True,
                verified=True,
                risk_level=risk,
                data=execution.data,
                evidence=verification.evidence,
                request_id=intent.request_id,
            )
        return ToolResult(
            tool_name=tool.name,
            outcome=ToolOutcome.REQUIRES_ATTENTION,
            ok=False,
            verified=False,
            risk_level=risk,
            error_code="VERIFICATION_FAILED",
            message="execution happened but verification failed",
            data=execution.data,
            evidence=execution.evidence,
            mismatches=verification.mismatches,
            request_id=intent.request_id,
        )

    @staticmethod
    def _denied(
        intent: ToolIntent,
        tool: ToolDefinition | None,
        risk: RiskLevel,
        decision: Any,
    ) -> ToolResult:
        return ToolResult(
            tool_name=tool.name if tool is not None else intent.tool_name,
            outcome=ToolOutcome.DENIED,
            ok=False,
            verified=False,
            risk_level=risk,
            message=str(decision),
            request_id=intent.request_id,
        )

    @staticmethod
    def _approval_required(
        intent: ToolIntent,
        tool: ToolDefinition,
        risk: RiskLevel,
        decision: Any,
    ) -> ToolResult:
        return ToolResult(
            tool_name=tool.name,
            outcome=ToolOutcome.APPROVAL_REQUIRED,
            ok=False,
            verified=False,
            risk_level=risk,
            message=f"human approval is required: {decision}",
            request_id=intent.request_id,
        )

    @staticmethod
    def _attention(
        intent: ToolIntent,
        tool: ToolDefinition | None,
        risk: RiskLevel,
        error_code: str | None,
        message: str,
    ) -> ToolResult:
        return ToolResult(
            tool_name=tool.name if tool is not None else intent.tool_name,
            outcome=ToolOutcome.REQUIRES_ATTENTION,
            ok=False,
            verified=False,
            risk_level=risk,
            error_code=error_code,
            message=message,
            request_id=intent.request_id,
        )

    @staticmethod
    def _rejected(
        intent: ToolIntent,
        tool: ToolDefinition | None,
        error_code: str,
        message: str,
    ) -> ToolResult:
        return ToolResult(
            tool_name=tool.name if tool is not None else intent.tool_name,
            outcome=ToolOutcome.REJECTED,
            ok=False,
            verified=False,
            risk_level=RiskLevel.LOW,
            error_code=error_code,
            message=message,
            request_id=intent.request_id,
        )

    @staticmethod
    def _execution_failure(
        intent: ToolIntent,
        tool: ToolDefinition | None,
        exc: ToolExecutionError,
    ) -> ToolResult:
        return ToolResult(
            tool_name=tool.name if tool is not None else intent.tool_name,
            outcome=ToolOutcome.EXECUTION_FAILED,
            ok=False,
            verified=False,
            risk_level=tool.risk_level if tool is not None else RiskLevel.LOW,
            error_code=exc.error_code or "EXECUTION_FAILED",
            message=str(exc),
            request_id=intent.request_id,
        )

    @staticmethod
    def _validation_messages(exception: Exception) -> list[str]:
        errors: list[dict[str, Any]] = getattr(exception, "errors", lambda: [])()
        messages: list[str] = []
        for error in errors:
            where = ".".join(str(part) for part in error.get("loc", ()))
            messages.append(f"{where}: {error.get('msg', 'invalid')}")
        return messages

    def _audit(
        self,
        intent: ToolIntent,
        tool: ToolDefinition | None,
        result: ToolResult,
    ) -> None:
        """Record safe metadata only: no arguments, tokens, or credentials."""
        self._log.info(
            "tool invocation completed",
            tool_name=result.tool_name,
            status=result.outcome.value,
            ok=result.ok,
            verified=result.verified,
            risk_level=result.risk_level.value,
            error_code=result.error_code,
            task_id=str(intent.task_id),
            tenant_id=str(intent.tenant_id),
            user_id=str(intent.user_id),
            request_id=intent.request_id,
        )


def _metadata_str(context: ToolContext, key: str) -> str | None:
    value = context.metadata.get(key)
    if isinstance(value, str) and value.strip():
        return value
    return None


def _metadata_int(context: ToolContext, key: str) -> int:
    try:
        return int(context.metadata.get(key, 0))
    except (TypeError, ValueError):
        return 0


def _metadata_float(context: ToolContext, key: str) -> float:
    try:
        return float(context.metadata.get(key, 0.0))
    except (TypeError, ValueError):
        return 0.0


def _metadata_identity(context: ToolContext) -> IdentityStatus | None:
    raw = context.metadata.get("recipient_identity")
    if raw is None:
        return None
    try:
        return IdentityStatus(str(raw).upper())
    except ValueError:
        # Unknown identity metadata is escalation-safe, never permissive.
        return IdentityStatus.UNKNOWN


__all__ = ["ToolGateway"]