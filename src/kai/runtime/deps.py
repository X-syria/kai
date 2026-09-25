"""FastAPI plumbing for the Agent Runtime (stub-injectable boundaries)."""

from __future__ import annotations

from fastapi import Depends

from kai.approvals.service import ApprovalService
from kai.planner.deps import get_planner_model
from kai.planner.model import PlannerModel
from kai.policy.content import ScheduleContentAuthorizer
from kai.policy.engine import PolicyEngine
from kai.runtime.service import AgentRuntime
from kai.tools.catalog import default_registry
from kai.tools.gateway import ToolGateway
from kai.tools.stubs import ContractVerificationEngine


def get_runtime_gateway() -> ToolGateway:
    """The runtime's own gateway with the app's real, non-LLM boundaries.

    Every dependency here is the same deterministic engine the rest of KAI
    uses: no bypasses, no stubs, no mock permissions.
    """
    from kai.identity.service import IdentityResolver

    return ToolGateway(
        registry=default_registry(),
        policy_engine=PolicyEngine(),
        content_authorizer=ScheduleContentAuthorizer(),
        approval_manager=ApprovalService(),
        verification_engine=ContractVerificationEngine(),
        identity_resolver=IdentityResolver(),
    )


def get_runtime_service(
    gateway: ToolGateway = Depends(get_runtime_gateway),
    model: PlannerModel = Depends(get_planner_model),
) -> AgentRuntime:
    return AgentRuntime(gateway=gateway, model=model)


__all__ = ["get_runtime_gateway", "get_runtime_service"]