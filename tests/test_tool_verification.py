"""Verification boundary: execution success is never the same as verified.

The gateway reports ``EXECUTION_SUCCEEDED`` only when the tool's declared
verification contract held. Sending or creating something that the provider
cannot prove (mismatches, read failures, manual contracts) yields
``REQUIRES_ATTENTION`` with concrete evidence of what did not verify.
"""

from __future__ import annotations

from pydantic import BaseModel

from kai.tools import (
    ApprovalManagerStub,
    RiskLevel,
    ToolDefinition,
    ToolExecutionOutcome,
    ToolRegistry,
    VerificationContract,
)
from kai.tools.result import ToolOutcome
from tool_harness import (
    GoogleBackend,
    close_context,
    insert_account,
    make_context,
    make_gateway,
    make_intent,
    make_task,
    make_tenant_user,
    message_payload,
    session_scope,
)


async def test_gmail_send_verified_success() -> None:
    """A send the provider accepts AND reads back as SENT is verified."""
    backend = GoogleBackend()
    tenant, user = await make_tenant_user("acme@corp.example")
    account = await insert_account(
        tenant_id=tenant.id, user_id=user.id, email="alice@example.com", sub="sub-1"
    )
    task = await make_task(tenant_id=tenant.id, user_id=user.id)

    gateway, parts = make_gateway(approval=ApprovalManagerStub())
    async with session_scope() as session:
        context = make_context(
            session=session, task=task, backend=backend, account_id=account.id
        )
        result = await gateway.execute(
            make_intent(
                "gmail.send",
                task=task,
                arguments={
                    "to": "bob@example.com",
                    "subject": "Hello Bob",
                    "body": "This is the plain text body.",
                },
            ),
            context,
        )
        await close_context(context)

    assert result.outcome is ToolOutcome.EXECUTION_SUCCEEDED
    assert result.ok is True
    assert result.verified is True
    assert result.evidence["contract"] == "read_after_send"
    assert result.evidence["message_id"] == "sent-message-1"
    assert "SENT" in result.evidence["label_ids"]
    assert result.mismatches == ()
    assert len(backend.gmail_send_calls) == 1
    # Read-after-send really happened: a GET followed the POST.
    assert len(backend.gmail_read_calls) == 1


async def test_gmail_send_readback_mismatch_is_not_verified() -> None:
    """Provider stored a different recipient: the send happened but is not
    verified, so the result is REQUIRES_ATTENTION with concrete mismatches."""
    backend = GoogleBackend()
    backend.send_return = message_payload(
        message_id="sent-message-1",
        thread_id="sent-thread-1",
        to=("carol@example.com",),
        subject="Hello Bob",
        labels=("SENT",),
    )
    tenant, user = await make_tenant_user("acme@corp.example")
    account = await insert_account(
        tenant_id=tenant.id, user_id=user.id, email="alice@example.com", sub="sub-1"
    )
    task = await make_task(tenant_id=tenant.id, user_id=user.id)

    gateway, _ = make_gateway(approval=ApprovalManagerStub())
    async with session_scope() as session:
        context = make_context(
            session=session, task=task, backend=backend, account_id=account.id
        )
        result = await gateway.execute(
            make_intent(
                "gmail.send",
                task=task,
                arguments={
                    "to": "bob@example.com",
                    "subject": "Hello Bob",
                    "body": "This is the plain text body.",
                },
            ),
            context,
        )
        await close_context(context)

    assert result.outcome is ToolOutcome.REQUIRES_ATTENTION
    assert result.ok is False
    assert result.verified is False
    assert result.error_code == "VERIFICATION_FAILED"
    assert any(m.field == "recipient" for m in result.mismatches)


async def test_gmail_send_unreadable_readback_is_not_verified() -> None:
    """Provider could not be read back (404): the message cannot be claimed."""
    backend = GoogleBackend()
    backend.readback_status = 404
    tenant, user = await make_tenant_user("acme@corp.example")
    account = await insert_account(
        tenant_id=tenant.id, user_id=user.id, email="alice@example.com", sub="sub-1"
    )
    task = await make_task(tenant_id=tenant.id, user_id=user.id)

    gateway, _ = make_gateway(approval=ApprovalManagerStub())
    async with session_scope() as session:
        context = make_context(
            session=session, task=task, backend=backend, account_id=account.id
        )
        result = await gateway.execute(
            make_intent(
                "gmail.send",
                task=task,
                arguments={
                    "to": "bob@example.com",
                    "subject": "Hello Bob",
                    "body": "This is the plain text body.",
                },
            ),
            context,
        )
        await close_context(context)

    assert result.outcome is ToolOutcome.REQUIRES_ATTENTION
    assert result.verified is False
    assert result.error_code == "VERIFICATION_FAILED"
    assert any(m.field == "read_after_send" for m in result.mismatches)
    assert len(backend.gmail_send_calls) == 1


class _ManualArgs(BaseModel):
    pass


class _FactExecutor:
    """Deterministic executor whose claim is whatever the test configures."""

    def __init__(self) -> None:
        self.runs = 0

    async def execute(self, **kwargs: object) -> ToolExecutionOutcome:
        self.runs += 1
        return ToolExecutionOutcome(
            ok=True,
            verified=True,
            data={"started": True},
            evidence={"job_id": "job-1"},
        )


async def test_manual_contract_never_claims_verification() -> None:
    """A MANUAL-verified tool executes, but the gateway still refuses to call
    it verified: a human review must happen first."""
    registry = ToolRegistry()
    executor = _FactExecutor()
    registry.register(
        ToolDefinition(
            name="demo.manual_action",
            description="demo tool requiring human verification",
            input_schema=_ManualArgs,
            risk_level=RiskLevel.LOW,
            capability="demo",
            executor=executor,
            verification_contract=VerificationContract.MANUAL,
            requires_account=False,
        )
    )
    registry.lock()

    backend = GoogleBackend()
    tenant, user = await make_tenant_user("acme@corp.example")
    task = await make_task(tenant_id=tenant.id, user_id=user.id)

    gateway, _ = make_gateway(registry=registry)
    async with session_scope() as session:
        context = make_context(
            session=session, task=task, backend=backend, account_id=None
        )
        result = await gateway.execute(
            make_intent("demo.manual_action", task=task, arguments={}), context
        )
        await close_context(context)

    assert executor.runs == 1  # the tool genuinely executed
    assert result.outcome is ToolOutcome.REQUIRES_ATTENTION
    assert result.ok is False
    assert result.verified is False
    assert any(m.field == "verification" for m in result.mismatches)


class ReadBackedExecutor:
    """Forces the boundary to judge a READ_BACKED contract on its own."""

    def __init__(self, *, verified_claim: bool, evidence: dict[str, object]) -> None:
        self.verified_claim = verified_claim
        self.evidence = evidence

    async def execute(self, **kwargs: object) -> ToolExecutionOutcome:
        return ToolExecutionOutcome(
            ok=True,
            verified=self.verified_claim,
            data={},
            evidence=self.evidence,
        )


async def test_read_contract_without_provider_id_is_not_verified() -> None:
    """Even when the executor claims verified=True, the boundary refuses a
    READ_BACKED contract that carries no provider id: the claim must be
    independently supported by evidence."""
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="demo.read_claim",
            description="demo read tool that claims success without proof",
            input_schema=_ManualArgs,
            risk_level=RiskLevel.LOW,
            capability="demo",
            executor=ReadBackedExecutor(verified_claim=True, evidence={}),
            verification_contract=VerificationContract.READ_BACKED,
            requires_account=False,
        )
    )
    registry.lock()

    backend = GoogleBackend()
    tenant, user = await make_tenant_user("acme@corp.example")
    task = await make_task(tenant_id=tenant.id, user_id=user.id)

    gateway, _ = make_gateway(registry=registry)
    async with session_scope() as session:
        context = make_context(
            session=session, task=task, backend=backend, account_id=None
        )
        result = await gateway.execute(
            make_intent("demo.read_claim", task=task, arguments={}), context
        )
        await close_context(context)

    assert result.verified is False
    assert result.outcome is ToolOutcome.REQUIRES_ATTENTION
    assert any(m.field == "evidence" for m in result.mismatches)


async def test_verified_read_claim_with_provider_id_passes() -> None:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="demo.read_proof",
            description="demo read tool with provider id evidence",
            input_schema=_ManualArgs,
            risk_level=RiskLevel.LOW,
            capability="demo",
            executor=ReadBackedExecutor(
                verified_claim=True, evidence={"message_id": "m-1"}
            ),
            verification_contract=VerificationContract.READ_BACKED,
            requires_account=False,
        )
    )
    registry.lock()

    backend = GoogleBackend()
    tenant, user = await make_tenant_user("acme@corp.example")
    task = await make_task(tenant_id=tenant.id, user_id=user.id)

    gateway, _ = make_gateway(registry=registry)
    async with session_scope() as session:
        context = make_context(
            session=session, task=task, backend=backend, account_id=None
        )
        result = await gateway.execute(
            make_intent("demo.read_proof", task=task, arguments={}), context
        )
        await close_context(context)

    assert result.outcome is ToolOutcome.EXECUTION_SUCCEEDED
    assert result.verified is True