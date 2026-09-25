"""Approval Manager tests — real ApprovalService/API against real PostgreSQL.

Covers the security contract end to end: exact-intent binding (SHA-256),
single use, lazy/deterministic expiration, deduplication, immutability of
terminal states, tenant/user isolation, TOCTOU re-validation, approval fatigue
(non-authorizing, once, cooldown), injection resistance, and gateway
integration. No SQLite, no mocks: the only fake is Google's HTTP via
``GoogleBackend``/``httpx.MockTransport`` exactly like the rest of the suite.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select

from kai.approvals.errors import (
    ApprovalNotFoundError,
    ExpiredApprovalError,
    InvalidApprovalTransition,
    InvalidSuggestionTransition,
)
from kai.approvals.models import Approval, ApprovalRuleSuggestion, ApprovalStatus
from kai.approvals.service import (
    ApprovalAction,
    ApprovalService,
    build_request_summary,
    intent_hash,
)
from kai.auth.jwt import TokenClaims, decode_access_token
from kai.db.engine import async_session_factory
from kai.db.models import Tenant, User
from kai.main import app
from kai.tools import ApprovalDecision, RiskLevel, ToolContext, default_registry
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

API_PREFIX = "/api/v1"
GM_SEND = "gmail.send"

_T0 = datetime(2026, 9, 24, 10, 0, 0, tzinfo=UTC)


def _tool(name: str = GM_SEND):
    return default_registry().get(name)


def _arguments(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "to": "bob@example.com",
        "subject": "Quarterly report",
        "body": "Please review the attached numbers.",
    }
    data.update(overrides)
    return data


def _send_backend() -> GoogleBackend:
    """A Gmail backend whose readback matches :func:`_arguments`."""
    backend = GoogleBackend()
    backend.send_return = message_payload(
        message_id="sent-message-1",
        subject="Quarterly report",
        body_text="Please review the attached numbers.",
        labels=("SENT",),
    )
    return backend


def _context(
    session: Any,
    task: Any,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    account_id: uuid.UUID | None = None,
    metadata: dict[str, Any] | None = None,
) -> ToolContext:
    return ToolContext(
        session=session,
        task_id=task.id,
        tenant_id=tenant_id,
        user_id=user_id,
        account_id=account_id,
        metadata=metadata or {},
    )


async def _same_tenant_users(
    email_a: str, email_b: str
) -> tuple[Tenant, User, User]:
    """Two users inside one tenant (service-level isolation fixture)."""
    async with async_session_factory() as session:
        tenant = Tenant(name=f"tenant-{uuid.uuid4()}")
        user_a = User(email=email_a, password_hash="test", tenant=tenant)
        user_b = User(email=email_b, password_hash="test", tenant=tenant)
        session.add_all([tenant, user_a, user_b])
        await session.commit()
        await session.refresh(tenant)
        await session.refresh(user_a)
        await session.refresh(user_b)
        return tenant, user_a, user_b


async def _registered_with_account(
    email: str = "approver@corp.example",
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    tenant, user = await make_tenant_user(email)
    account = await insert_account(
        tenant_id=tenant.id,
        user_id=user.id,
        email="alice@example.com",
        sub=f"sub-{uuid.uuid4()}",
    )
    return tenant.id, user.id, account.id


async def _count_rows(model: Any, **filters: Any) -> int:
    async with async_session_factory() as session:
        result = await session.execute(
            select(func.count()).select_from(model).filter_by(**filters)
        )
        return int(result.scalar_one())


# ---------------------------------------------------------------------------
# 1. Exact intent binding (SHA-256 canonical intent)
# ---------------------------------------------------------------------------


def test_intent_hash_is_deterministic() -> None:
    tool = _tool()
    args = _arguments()
    first = intent_hash(tool=tool, arguments=args, account_id=None)
    second = intent_hash(tool=tool, arguments=dict(args), account_id=None)
    assert first == second
    assert len(first) == 64


@pytest.mark.parametrize(
    "changed, description",
    [
        ({"to": "carol@example.com"}, "recipient"),
        ({"subject": "Changed subject"}, "subject"),
        ({"body": "Changed body"}, "body"),
        ({"start_time": datetime(2026, 10, 1, 9, 0, tzinfo=UTC)}, "calendar time"),
        ({"attendees": ["carol@example.com"]}, "attendees"),
    ],
)
def test_any_security_argument_change_changes_the_hash(
    changed: dict[str, Any], description: str
) -> None:
    tool = _tool()
    base = intent_hash(tool=tool, arguments=_arguments(), account_id=None)
    altered = intent_hash(
        tool=tool, arguments={**_arguments(), **changed}, account_id=None
    )
    assert altered != base, f"changing {description} must change the intent hash"


def test_different_tool_and_account_produce_different_hashes() -> None:
    tool = _tool()
    base = intent_hash(tool=tool, arguments=_arguments(), account_id=None)
    other_tool = intent_hash(
        tool=default_registry().get("calendar.create_event"),
        arguments=_arguments(),
        account_id=None,
    )
    other_account = intent_hash(
        tool=tool,
        arguments=_arguments(),
        account_id=uuid.uuid4(),
    )
    assert other_tool != base
    assert other_account != base


async def test_request_summary_never_exposes_the_body() -> None:
    tool = _tool()
    summary = build_request_summary(
        tool=tool,
        arguments=_arguments(body="TOP-SECRET-MARKER-BODY"),
    )
    assert "TOP-SECRET-MARKER-BODY" not in summary
    assert "bob@example.com" in summary
    assert "Quarterly report" in summary


# ---------------------------------------------------------------------------
# 2. Request, persistence, deduplication
# ---------------------------------------------------------------------------


async def test_request_persists_a_pending_approval() -> None:
    tenant_id, user_id, account_id = await _registered_with_account()
    task = await make_task(
        tenant_id=tenant_id, user_id=user_id, allowed_tools=["gmail"]
    )
    service = ApprovalService()

    async with session_scope() as session:
        context = _context(
            session=session,
            task=task,
            tenant_id=tenant_id,
            user_id=user_id,
            account_id=account_id,
        )
        approval = await service.request(
            session=session,
            intent=make_intent(GM_SEND, task=task),
            context=context,
            tool=_tool(),
            task=task,
            arguments=_arguments(),
        )
        await session.refresh(approval)

    assert approval.status == ApprovalStatus.PENDING.value
    assert approval.user_id == user_id
    assert approval.tenant_id == tenant_id
    assert approval.task_id == task.id
    assert approval.tool_name == GM_SEND
    assert approval.risk_level == RiskLevel.MEDIUM.value
    assert approval.decided_at is None
    assert (await _count_rows(Approval)) == 1


async def test_same_pending_intent_is_deduplicated() -> None:
    tenant_id, user_id, _ = await _registered_with_account()
    task = await make_task(
        tenant_id=tenant_id, user_id=user_id, allowed_tools=["gmail"]
    )
    service = ApprovalService()

    async with session_scope() as session:
        context = _context(
            session=session, task=task, tenant_id=tenant_id, user_id=user_id
        )
        first = await service.request(
            session=session,
            intent=make_intent(GM_SEND, task=task),
            context=context,
            tool=_tool(),
            task=task,
            arguments=_arguments(),
        )
        second = await service.request(
            session=session,
            intent=make_intent(GM_SEND, task=task),
            context=context,
            tool=_tool(),
            task=task,
            arguments=_arguments(),
        )

    assert first.id == second.id
    assert (await _count_rows(Approval)) == 1


async def test_different_intent_gets_a_distinct_approval() -> None:
    tenant_id, user_id, _ = await _registered_with_account()
    task = await make_task(
        tenant_id=tenant_id, user_id=user_id, allowed_tools=["gmail"]
    )
    service = ApprovalService()

    async with session_scope() as session:
        context = _context(
            session=session, task=task, tenant_id=tenant_id, user_id=user_id
        )
        first = await service.request(
            session=session,
            intent=make_intent(GM_SEND, task=task),
            context=context,
            tool=_tool(),
            task=task,
            arguments=_arguments(),
        )
        second = await service.request(
            session=session,
            intent=make_intent(GM_SEND, task=task),
            context=context,
            tool=_tool(),
            task=task,
            arguments=_arguments(to="carol@example.com"),
        )

    assert first.id != second.id
    assert first.request_summary != second.request_summary
    assert (await _count_rows(Approval)) == 2


# ---------------------------------------------------------------------------
# 3. Human decisions and immutability
# ---------------------------------------------------------------------------


async def test_pending_can_be_approved() -> None:
    tenant_id, user_id, _ = await _registered_with_account()
    task = await make_task(
        tenant_id=tenant_id, user_id=user_id, allowed_tools=["gmail"]
    )
    service = ApprovalService()

    async with session_scope() as session:
        context = _context(
            session=session, task=task, tenant_id=tenant_id, user_id=user_id
        )
        approval = await service.request(
            session=session,
            intent=make_intent(GM_SEND, task=task),
            context=context,
            tool=_tool(),
            task=task,
            arguments=_arguments(),
        )
        decided = await service.decide(
            session=session,
            approval_id=approval.id,
            decision=ApprovalAction.APPROVE,
            tenant_id=tenant_id,
            user_id=user_id,
            reason="  looks   right   ",
        )

    assert decided.status == ApprovalStatus.APPROVED.value
    assert decided.decided_by == user_id
    assert decided.decided_at is not None
    assert decided.decision_reason == "looks right"


async def test_pending_can_be_rejected() -> None:
    tenant_id, user_id, _ = await _registered_with_account()
    task = await make_task(
        tenant_id=tenant_id, user_id=user_id, allowed_tools=["gmail"]
    )
    service = ApprovalService()

    async with session_scope() as session:
        context = _context(
            session=session, task=task, tenant_id=tenant_id, user_id=user_id
        )
        approval = await service.request(
            session=session,
            intent=make_intent(GM_SEND, task=task),
            context=context,
            tool=_tool(),
            task=task,
            arguments=_arguments(),
        )
        decided = await service.decide(
            session=session,
            approval_id=approval.id,
            decision=ApprovalAction.REJECT,
            tenant_id=tenant_id,
            user_id=user_id,
            reason="not now",
        )

    assert decided.status == ApprovalStatus.DENIED.value
    assert decided.decision_reason == "not now"


async def test_terminal_approval_cannot_be_re_decided() -> None:
    tenant_id, user_id, _ = await _registered_with_account()
    task = await make_task(
        tenant_id=tenant_id, user_id=user_id, allowed_tools=["gmail"]
    )
    service = ApprovalService()

    async with session_scope() as session:
        context = _context(
            session=session, task=task, tenant_id=tenant_id, user_id=user_id
        )
        approval = await service.request(
            session=session,
            intent=make_intent(GM_SEND, task=task),
            context=context,
            tool=_tool(),
            task=task,
            arguments=_arguments(),
        )
        await service.decide(
            session=session,
            approval_id=approval.id,
            decision=ApprovalAction.REJECT,
            tenant_id=tenant_id,
            user_id=user_id,
        )
        with pytest.raises(InvalidApprovalTransition):
            await service.decide(
                session=session,
                approval_id=approval.id,
                decision=ApprovalAction.APPROVE,
                tenant_id=tenant_id,
                user_id=user_id,
            )


async def test_approved_cannot_be_rejected_directly() -> None:
    tenant_id, user_id, _ = await _registered_with_account()
    task = await make_task(
        tenant_id=tenant_id, user_id=user_id, allowed_tools=["gmail"]
    )
    service = ApprovalService()

    async with session_scope() as session:
        context = _context(
            session=session, task=task, tenant_id=tenant_id, user_id=user_id
        )
        approval = await service.request(
            session=session,
            intent=make_intent(GM_SEND, task=task),
            context=context,
            tool=_tool(),
            task=task,
            arguments=_arguments(),
        )
        await service.decide(
            session=session,
            approval_id=approval.id,
            decision=ApprovalAction.APPROVE,
            tenant_id=tenant_id,
            user_id=user_id,
        )
        with pytest.raises(InvalidApprovalTransition):
            await service.decide(
                session=session,
                approval_id=approval.id,
                decision=ApprovalAction.REJECT,
                tenant_id=tenant_id,
                user_id=user_id,
            )


# ---------------------------------------------------------------------------
# 4. Lazy, deterministic expiration
# ---------------------------------------------------------------------------


async def test_expired_pending_is_lazily_flagged_and_blocked() -> None:
    tenant_id, user_id, _ = await _registered_with_account()
    task = await make_task(
        tenant_id=tenant_id, user_id=user_id, allowed_tools=["gmail"]
    )
    service = ApprovalService(ttl=timedelta(minutes=10))

    async with session_scope() as session:
        context = _context(
            session=session, task=task, tenant_id=tenant_id, user_id=user_id
        )
        approval = await service.request(
            session=session,
            intent=make_intent(GM_SEND, task=task),
            context=context,
            tool=_tool(),
            task=task,
            arguments=_arguments(),
            now=_T0,
        )
        record = await service.check(
            intent=make_intent(GM_SEND, task=task),
            context=context,
            tool=_tool(),
            task=task,
            arguments=_arguments(),
            now=_T0 + timedelta(minutes=11),
        )
        refreshed = await service.get(
            session=session,
            approval_id=approval.id,
            tenant_id=tenant_id,
            user_id=user_id,
            now=_T0 + timedelta(minutes=11),
        )

    assert record.decision is ApprovalDecision.EXPIRED
    assert refreshed.status == ApprovalStatus.EXPIRED.value


async def test_expired_approval_never_authorizes_and_cannot_be_decided() -> None:
    tenant_id, user_id, _ = await _registered_with_account()
    task = await make_task(
        tenant_id=tenant_id, user_id=user_id, allowed_tools=["gmail"]
    )
    service = ApprovalService(ttl=timedelta(minutes=10))

    async with session_scope() as session:
        context = _context(
            session=session, task=task, tenant_id=tenant_id, user_id=user_id
        )
        approval = await service.request(
            session=session,
            intent=make_intent(GM_SEND, task=task),
            context=context,
            tool=_tool(),
            task=task,
            arguments=_arguments(),
            now=_T0,
        )
        await service.decide(
            session=session,
            approval_id=approval.id,
            decision=ApprovalAction.APPROVE,
            tenant_id=tenant_id,
            user_id=user_id,
            now=_T0,
        )
        # The single use lapsed before it was executed: no authorization.
        record = await service.check(
            intent=make_intent(GM_SEND, task=task),
            context=context,
            tool=_tool(),
            task=task,
            arguments=_arguments(),
            now=_T0 + timedelta(minutes=11),
        )
        # And it can no longer be "approved" into use.
        with pytest.raises(ExpiredApprovalError):
            await service.decide(
                session=session,
                approval_id=approval.id,
                decision=ApprovalAction.APPROVE,
                tenant_id=tenant_id,
                user_id=user_id,
                now=_T0 + timedelta(minutes=11),
            )

    assert record.decision is ApprovalDecision.EXPIRED


# ---------------------------------------------------------------------------
# 5. Tenant and user isolation
# ---------------------------------------------------------------------------


async def test_foreign_tenant_and_foreign_user_resolve_to_not_found() -> None:
    tenant, user_a, user_b = await _same_tenant_users(
        "owner@corp.example", "peer@corp.example"
    )
    other_tenant_id, other_user_id, _ = await _registered_with_account(
        "outsider@other.example"
    )
    task = await make_task(
        tenant_id=tenant.id, user_id=user_a.id, allowed_tools=["gmail"]
    )
    service = ApprovalService()

    async with session_scope() as session:
        context = _context(
            session=session, task=task, tenant_id=tenant.id, user_id=user_a.id
        )
        approval = await service.request(
            session=session,
            intent=make_intent(GM_SEND, task=task),
            context=context,
            tool=_tool(),
            task=task,
            arguments=_arguments(),
        )
        # Same tenant, different user: invisible.
        with pytest.raises(ApprovalNotFoundError):
            await service.get(
                session=session,
                approval_id=approval.id,
                tenant_id=tenant.id,
                user_id=user_b.id,
            )
        with pytest.raises(ApprovalNotFoundError):
            await service.decide(
                session=session,
                approval_id=approval.id,
                decision=ApprovalAction.APPROVE,
                tenant_id=tenant.id,
                user_id=user_b.id,
            )
        # Different tenant, same-looking user: invisible.
        with pytest.raises(ApprovalNotFoundError):
            await service.get(
                session=session,
                approval_id=approval.id,
                tenant_id=other_tenant_id,
                user_id=other_user_id,
            )
        # The owner can still decide it, so scope is not the test's artifact.
        decided = await service.decide(
            session=session,
            approval_id=approval.id,
            decision=ApprovalAction.APPROVE,
            tenant_id=tenant.id,
            user_id=user_a.id,
        )
        assert decided.status == ApprovalStatus.APPROVED.value


# ---------------------------------------------------------------------------
# 6. Single use, replay, and changed-intent protection (gateway)
# ---------------------------------------------------------------------------


async def test_approval_authorizes_exactly_one_execution() -> None:
    tenant_id, user_id, account_id = await _registered_with_account()
    task = await make_task(
        tenant_id=tenant_id, user_id=user_id, allowed_tools=["gmail"]
    )
    backend = _send_backend()
    service = ApprovalService(ttl=timedelta(days=30))
    gateway, parts = make_gateway(approval=service)
    intent = make_intent(
        GM_SEND, task=task, arguments=_arguments()
    )

    async with session_scope() as session:
        context = make_context(
            session=session, task=task, backend=backend, account_id=account_id
        )
        # 1) First attempt: held for human review, nothing sent.
        held = await gateway.execute(intent, context)
        assert held.outcome is ToolOutcome.APPROVAL_REQUIRED
        assert not backend.gmail_send_calls

        approvals = await service.list_approvals(
            session=session, tenant_id=tenant_id, user_id=user_id
        )
        assert len(approvals) == 1
        approval = approvals[0]
        assert approval.status == ApprovalStatus.PENDING.value

        # 2) Human approves; the same exact intent executes exactly once.
        await service.decide(
            session=session,
            approval_id=approval.id,
            decision=ApprovalAction.APPROVE,
            tenant_id=tenant_id,
            user_id=user_id,
        )
        done = await gateway.execute(intent, context)
        assert done.outcome is ToolOutcome.EXECUTION_SUCCEEDED
        assert len(backend.gmail_send_calls) == 1

        consumed = await service.get(
            session=session,
            approval_id=approval.id,
            tenant_id=tenant_id,
            user_id=user_id,
        )
        assert consumed.consumed_at is not None

        # 3) Replay of the identical intent: the single use is exhausted.
        replay = await gateway.execute(intent, context)
        assert replay.outcome is ToolOutcome.DENIED
        assert len(backend.gmail_send_calls) == 1
        assert "stale" in replay.message.lower()
        # A stale/consumed intent never consumes an approval again — the single
        # use stays consumed exactly once, and nothing new was consumed.
        consumed_rows = await _consumed_rows(tenant_id, user_id)
        assert len(consumed_rows) == 1
        assert consumed_rows[0].id == approval.id


async def test_changed_intent_cannot_reuse_an_old_approval() -> None:
    tenant_id, user_id, account_id = await _registered_with_account()
    task = await make_task(
        tenant_id=tenant_id, user_id=user_id, allowed_tools=["gmail"]
    )
    backend = _send_backend()
    service = ApprovalService(ttl=timedelta(days=30))
    gateway, _ = make_gateway(approval=service)
    original = make_intent(GM_SEND, task=task, arguments=_arguments())

    async with session_scope() as session:
        context = make_context(
            session=session, task=task, backend=backend, account_id=account_id
        )
        assert (await gateway.execute(original, context)).outcome is (
            ToolOutcome.APPROVAL_REQUIRED
        )
        approvals = await service.list_approvals(
            session=session, tenant_id=tenant_id, user_id=user_id
        )
        await service.decide(
            session=session,
            approval_id=approvals[0].id,
            decision=ApprovalAction.APPROVE,
            tenant_id=tenant_id,
            user_id=user_id,
        )
        first = await gateway.execute(original, context)
        assert first.outcome is ToolOutcome.EXECUTION_SUCCEEDED
        assert len(backend.gmail_send_calls) == 1

        # Same tool/task, but a new recipient: an old approval must not carry.
        altered = make_intent(
            GM_SEND, task=task, arguments=_arguments(to="carol@example.com")
        )
        blocked = await gateway.execute(altered, context)
        assert blocked.outcome is ToolOutcome.APPROVAL_REQUIRED
        assert len(backend.gmail_send_calls) == 1

        tracks = await service.list_approvals(
            session=session, tenant_id=tenant_id, user_id=user_id
        )
        assert len(tracks) == 2
        assert tracks[0].status == ApprovalStatus.PENDING.value


async def test_policy_deny_wins_even_after_human_approval() -> None:
    """Approval is consulted only under REQUIRE_APPROVAL; DENY always stops."""
    tenant_id, user_id, account_id = await _registered_with_account()
    task = await make_task(
        tenant_id=tenant_id, user_id=user_id, allowed_tools=["gmail"]
    )
    backend = GoogleBackend()
    service = ApprovalService(ttl=timedelta(days=30))
    gateway, _ = make_gateway(approval=service)
    intent = make_intent(GM_SEND, task=task, arguments=_arguments())

    async with session_scope() as session:
        context = make_context(
            session=session, task=task, backend=backend, account_id=account_id
        )
        assert (await gateway.execute(intent, context)).outcome is (
            ToolOutcome.APPROVAL_REQUIRED
        )
        approvals = await service.list_approvals(
            session=session, tenant_id=tenant_id, user_id=user_id
        )
        await service.decide(
            session=session,
            approval_id=approvals[0].id,
            decision=ApprovalAction.APPROVE,
            tenant_id=tenant_id,
            user_id=user_id,
        )

        # The contract changes between approval and execution: DENY now wins.
        task.allowed_tools = ["calendar"]
        session.add(task)
        await session.commit()

        blocked = await gateway.execute(intent, context)
        assert blocked.outcome is ToolOutcome.DENIED
        assert not backend.gmail_send_calls

        # The approved single use was NOT consumed (execution never ran).
        refreshed = await service.get(
            session=session,
            approval_id=approvals[0].id,
            tenant_id=tenant_id,
            user_id=user_id,
        )
        assert refreshed.consumed_at is None


async def test_denial_is_stable_no_automatic_re_approval() -> None:
    tenant_id, user_id, account_id = await _registered_with_account()
    task = await make_task(
        tenant_id=tenant_id, user_id=user_id, allowed_tools=["gmail"]
    )
    backend = GoogleBackend()
    service = ApprovalService(ttl=timedelta(days=30))
    gateway, _ = make_gateway(approval=service)
    intent = make_intent(GM_SEND, task=task, arguments=_arguments())

    async with session_scope() as session:
        context = make_context(
            session=session, task=task, backend=backend, account_id=account_id
        )
        assert (await gateway.execute(intent, context)).outcome is (
            ToolOutcome.APPROVAL_REQUIRED
        )
        approvals = await service.list_approvals(
            session=session, tenant_id=tenant_id, user_id=user_id
        )
        await service.decide(
            session=session,
            approval_id=approvals[0].id,
            decision=ApprovalAction.REJECT,
            tenant_id=tenant_id,
            user_id=user_id,
        )

        retried = await gateway.execute(intent, context)
        assert retried.outcome is ToolOutcome.DENIED
        assert not backend.gmail_send_calls
        # The denial did not silently mint a fresh PENDING for the same intent.
        assert (await _count_rows(Approval, status="PENDING")) == 0

        # An explicit re-request is a brand-new human-decision window.
        fresh = await service.request(
            session=session,
            intent=intent,
            context=context,
            tool=_tool(),
            task=task,
            arguments=_arguments(),
        )
        assert fresh.id != approvals[0].id
        assert fresh.status == ApprovalStatus.PENDING.value


async def test_consume_refuses_a_different_intent() -> None:
    tenant_id, user_id, _ = await _registered_with_account()
    task = await make_task(
        tenant_id=tenant_id, user_id=user_id, allowed_tools=["gmail"]
    )
    service = ApprovalService()

    async with session_scope() as session:
        context = _context(
            session=session, task=task, tenant_id=tenant_id, user_id=user_id
        )
        approval = await service.request(
            session=session,
            intent=make_intent(GM_SEND, task=task),
            context=context,
            tool=_tool(),
            task=task,
            arguments=_arguments(),
        )
        await service.decide(
            session=session,
            approval_id=approval.id,
            decision=ApprovalAction.APPROVE,
            tenant_id=tenant_id,
            user_id=user_id,
        )
        with pytest.raises(ApprovalNotFoundError):
            await service.consume(
                approval_id=approval.id,
                intent=make_intent(GM_SEND, task=task),
                context=context,
                tool=_tool(),
                task=task,
                arguments=_arguments(to="carol@example.com"),
            )
        # A mismatched intent failed atomically: it never consumed the
        # approval, so the single use is still available.
        untouched = await service.get(
            session=session,
            approval_id=approval.id,
            tenant_id=tenant_id,
            user_id=user_id,
        )
        assert untouched.consumed_at is None


async def test_only_approved_approval_can_be_consumed() -> None:
    tenant_id, user_id, _ = await _registered_with_account()
    task = await make_task(
        tenant_id=tenant_id, user_id=user_id, allowed_tools=["gmail"]
    )
    service = ApprovalService()

    async with session_scope() as session:
        context = _context(
            session=session, task=task, tenant_id=tenant_id, user_id=user_id
        )
        approval = await service.request(
            session=session,
            intent=make_intent(GM_SEND, task=task),
            context=context,
            tool=_tool(),
            task=task,
            arguments=_arguments(),
        )
        with pytest.raises(InvalidApprovalTransition):
            await service.consume(
                approval_id=approval.id,
                intent=make_intent(GM_SEND, task=task),
                context=context,
                tool=_tool(),
                task=task,
                arguments=_arguments(),
            )


# ---------------------------------------------------------------------------
# 7. Task State Machine integration (best-effort, never invented)
# ---------------------------------------------------------------------------


async def test_requesting_and_deciding_moves_the_task_through_the_machine() -> None:
    tenant_id, user_id, _ = await _registered_with_account()
    task = await make_task(
        tenant_id=tenant_id, user_id=user_id, allowed_tools=["gmail"], state="POLICY_CHECK"
    )
    service = ApprovalService()

    async with session_scope() as session:
        context = _context(
            session=session, task=task, tenant_id=tenant_id, user_id=user_id
        )
        approval = await service.request(
            session=session,
            intent=make_intent(GM_SEND, task=task),
            context=context,
            tool=_tool(),
            task=task,
            arguments=_arguments(),
        )
        from kai.tasks.models import Task as TaskRow
        from kai.tasks.service import get_task

        after_request = await get_task(
            session, task_id=task.id, tenant_id=tenant_id, user_id=user_id
        )
        assert after_request.state == "WAITING_APPROVAL"

        await service.decide(
            session=session,
            approval_id=approval.id,
            decision=ApprovalAction.APPROVE,
            tenant_id=tenant_id,
            user_id=user_id,
        )
        after_approve = await get_task(
            session, task_id=task.id, tenant_id=tenant_id, user_id=user_id
        )
        assert after_approve.state == "EXECUTING"

        rejection_task = await make_task(
            tenant_id=tenant_id,
            user_id=user_id,
            allowed_tools=["gmail"],
            state="WAITING_APPROVAL",
        )
        context.task_id = rejection_task.id
        pending = await service.request(
            session=session,
            intent=make_intent(GM_SEND, task=rejection_task),
            context=context,
            tool=_tool(),
            task=rejection_task,
            arguments=_arguments(),
        )
        await service.decide(
            session=session,
            approval_id=pending.id,
            decision=ApprovalAction.REJECT,
            tenant_id=tenant_id,
            user_id=user_id,
        )
        after_reject = await get_task(
            session,
            task_id=rejection_task.id,
            tenant_id=tenant_id,
            user_id=user_id,
        )
        assert after_reject.state == "FAILED"
        assert (await _count_rows(TaskRow)) >= 2  # tasks were never invented


async def test_running_task_pauses_for_approval() -> None:
    """An EXECUTING task that needs a human really parks in WAITING_APPROVAL."""
    tenant_id, user_id, _ = await _registered_with_account()
    task = await make_task(
        tenant_id=tenant_id, user_id=user_id, allowed_tools=["gmail"]
    )
    service = ApprovalService()

    async with session_scope() as session:
        context = _context(
            session=session, task=task, tenant_id=tenant_id, user_id=user_id
        )
        approval = await service.request(
            session=session,
            intent=make_intent(GM_SEND, task=task),
            context=context,
            tool=_tool(),
            task=task,
            arguments=_arguments(),
        )
        from kai.tasks.service import get_task

        after = await get_task(
            session, task_id=task.id, tenant_id=tenant_id, user_id=user_id
        )
        assert after.state == "WAITING_APPROVAL"

        await service.decide(
            session=session,
            approval_id=approval.id,
            decision=ApprovalAction.APPROVE,
            tenant_id=tenant_id,
            user_id=user_id,
        )
        resumed = await get_task(
            session, task_id=task.id, tenant_id=tenant_id, user_id=user_id
        )
        assert resumed.state == "EXECUTING"


# ---------------------------------------------------------------------------
# 8. Approval fatigue (suggestion is NEVER a permission)
# ---------------------------------------------------------------------------


async def test_approval_fatigue_offers_one_suggestion_at_the_threshold() -> None:
    tenant_id, user_id, _ = await _registered_with_account()
    task = await make_task(
        tenant_id=tenant_id, user_id=user_id, allowed_tools=["gmail"]
    )
    service = ApprovalService(
        ttl=timedelta(days=30),
        fatigue_threshold=3,
        fatigue_window=timedelta(days=14),
        fatigue_cooldown=timedelta(days=30),
    )

    for index in range(4):
        async with session_scope() as session:
            context = _context(
                session=session,
                task=task,
                tenant_id=tenant_id,
                user_id=user_id,
                metadata={"recipient_class": "external"},
            )
            intent = make_intent(GM_SEND, task=task)
            approval = await service.request(
                session=session,
                intent=intent,
                context=context,
                tool=_tool(),
                task=task,
                arguments=_arguments(),
                now=_T0 + timedelta(days=index),
            )
            await service.decide(
                session=session,
                approval_id=approval.id,
                decision=ApprovalAction.APPROVE,
                tenant_id=tenant_id,
                user_id=user_id,
                now=_T0 + timedelta(days=index),
            )
            await service.consume(
                approval_id=approval.id,
                intent=intent,
                context=context,
                tool=_tool(),
                task=task,
                arguments=_arguments(),
                now=_T0 + timedelta(days=index),
            )

    suggestions = await _list_via_factory(service, tenant_id, user_id)

    offered = [s for s in suggestions if s.status == "OFFERED"]
    assert len(offered) == 1  # the threshold produced exactly one offer
    assert offered[0].tool_name == GM_SEND
    assert offered[0].recipient_class == "external"
    assert offered[0].risk_level == RiskLevel.MEDIUM.value


async def _list_via_factory(
    service: ApprovalService, tenant_id: uuid.UUID, user_id: uuid.UUID
) -> list[ApprovalRuleSuggestion]:
    async with session_scope() as session:
        return await service.list_suggestions(
            session=session, tenant_id=tenant_id, user_id=user_id
        )


async def test_suggestion_is_non_authorizing() -> None:
    """An OFFERED rule grants nothing: policy/approval still gate execution."""
    tenant_id, user_id, _ = await _registered_with_account()
    task = await make_task(
        tenant_id=tenant_id, user_id=user_id, allowed_tools=["gmail"]
    )
    service = ApprovalService(
        ttl=timedelta(days=30),
        fatigue_threshold=2,
        fatigue_window=timedelta(days=14),
        fatigue_cooldown=timedelta(days=30),
    )

    for index in range(2):
        async with session_scope() as session:
            context = _context(
                session=session,
                task=task,
                tenant_id=tenant_id,
                user_id=user_id,
                metadata={"recipient_class": "external"},
            )
            intent = make_intent(GM_SEND, task=task)
            approval = await service.request(
                session=session,
                intent=intent,
                context=context,
                tool=_tool(),
                task=task,
                arguments=_arguments(subject=f"batch {index}"),
                now=_T0 + timedelta(days=index),
            )
            await service.decide(
                session=session,
                approval_id=approval.id,
                decision=ApprovalAction.APPROVE,
                tenant_id=tenant_id,
                user_id=user_id,
                now=_T0 + timedelta(days=index),
            )
            await service.consume(
                approval_id=approval.id,
                intent=intent,
                context=context,
                tool=_tool(),
                task=task,
                arguments=_arguments(subject=f"batch {index}"),
                now=_T0 + timedelta(days=index),
            )

    assert await _count_rows(ApprovalRuleSuggestion, status="OFFERED") == 1

    # A brand-new intent still requires a human decision despite the offer.
    async with session_scope() as session:
        context = _context(
            session=session,
            task=task,
            tenant_id=tenant_id,
            user_id=user_id,
            metadata={"recipient_class": "external"},
        )
        record = await service.check(
            intent=make_intent(GM_SEND, task=task),
            context=context,
            tool=_tool(),
            task=task,
            arguments=_arguments(subject="never auto-approved"),
        )
    assert record.decision is ApprovalDecision.REQUIRED


async def test_rejected_suggestion_starts_a_cooldown() -> None:
    tenant_id, user_id, _ = await _registered_with_account()
    task = await make_task(
        tenant_id=tenant_id, user_id=user_id, allowed_tools=["gmail"]
    )
    service = ApprovalService(
        ttl=timedelta(days=30),
        fatigue_threshold=2,
        fatigue_window=timedelta(days=14),
        fatigue_cooldown=timedelta(days=30),
    )

    async def cycle(day_offset: int) -> None:
        async with session_scope() as session:
            context = _context(
                session=session,
                task=task,
                tenant_id=tenant_id,
                user_id=user_id,
                metadata={"recipient_class": "external"},
            )
            intent = make_intent(GM_SEND, task=task)
            approval = await service.request(
                session=session,
                intent=intent,
                context=context,
                tool=_tool(),
                task=task,
                arguments=_arguments(subject=f"cycle {day_offset}"),
                now=_T0 + timedelta(days=day_offset),
            )
            await service.decide(
                session=session,
                approval_id=approval.id,
                decision=ApprovalAction.APPROVE,
                tenant_id=tenant_id,
                user_id=user_id,
                now=_T0 + timedelta(days=day_offset),
            )
            await service.consume(
                approval_id=approval.id,
                intent=intent,
                context=context,
                tool=_tool(),
                task=task,
                arguments=_arguments(subject=f"cycle {day_offset}"),
                now=_T0 + timedelta(days=day_offset),
            )

    # Threshold reached on day 1 -> one OFFERED suggestion.
    await cycle(1)
    await cycle(2)
    assert await _count_rows(ApprovalRuleSuggestion, status="OFFERED") == 1

    # User rejects it -> REJECTED, cooldown starts.
    async with session_scope() as session:
        suggestion = (await service.list_suggestions(
            session=session, tenant_id=tenant_id, user_id=user_id
        ))[0]
        rejected = await service.reject_suggestion(
            session=session,
            suggestion_id=suggestion.id,
            tenant_id=tenant_id,
            user_id=user_id,
            reason="prefer not",
            now=_T0 + timedelta(days=3),
        )
        assert rejected.status == "REJECTED"
        assert rejected.rejected_at == _T0 + timedelta(days=3)

    # Approvals inside the 30-day cooldown: no re-offer.
    await cycle(10)
    assert (await _count_rows(ApprovalRuleSuggestion, status="OFFERED")) == 0

    # After the cooldown lapses (day 3 + 30) fresh approvals may re-offer.
    await cycle(40)
    await cycle(41)
    assert (await _count_rows(ApprovalRuleSuggestion, status="OFFERED")) == 1


async def test_rejecting_a_suggestion_only_once() -> None:
    tenant_id, user_id, _ = await _registered_with_account()
    task = await make_task(
        tenant_id=tenant_id, user_id=user_id, allowed_tools=["gmail"]
    )
    service = ApprovalService(
        ttl=timedelta(minutes=10),
        fatigue_threshold=1,
        fatigue_window=timedelta(days=14),
        fatigue_cooldown=timedelta(days=30),
    )

    async with session_scope() as session:
        context = _context(
            session=session,
            task=task,
            tenant_id=tenant_id,
            user_id=user_id,
            metadata={"recipient_class": "external"},
        )
        approval = await service.request(
            session=session,
            intent=make_intent(GM_SEND, task=task),
            context=context,
            tool=_tool(),
            task=task,
            arguments=_arguments(),
            now=_T0,
        )
        await service.decide(
            session=session,
            approval_id=approval.id,
            decision=ApprovalAction.APPROVE,
            tenant_id=tenant_id,
            user_id=user_id,
            now=_T0,
        )
        suggestions = await service.list_suggestions(
            session=session, tenant_id=tenant_id, user_id=user_id
        )
        assert len(suggestions) == 1
        await service.reject_suggestion(
            session=session,
            suggestion_id=suggestions[0].id,
            tenant_id=tenant_id,
            user_id=user_id,
            now=_T0,
        )
        with pytest.raises(InvalidSuggestionTransition):
            await service.reject_suggestion(
                session=session,
                suggestion_id=suggestions[0].id,
                tenant_id=tenant_id,
                user_id=user_id,
                now=_T0,
            )


# ---------------------------------------------------------------------------
# 9. POST /approvals/execute does not exist; API only takes decisions
# ---------------------------------------------------------------------------


def test_no_execution_endpoint_is_exposed() -> None:
    paths = app.openapi()["paths"]
    assert "/api/v1/approvals/execute" not in paths


async def test_approval_endpoints_require_authentication(client: httpx.AsyncClient) -> None:
    assert (await client.get(f"{API_PREFIX}/approvals")).status_code == 401
    assert (await client.get(f"{API_PREFIX}/approvals/suggestions")).status_code == 401


def test_decision_request_rejects_foreign_fields() -> None:
    from pydantic import ValidationError

    from kai.approvals.schemas import ApprovalDecisionRequest

    with pytest.raises(ValidationError):
        ApprovalDecisionRequest.model_validate(
            {
                "reason": "ok",
                "status": "APPROVED",
                "tenant_id": str(uuid.uuid4()),
                "user_id": str(uuid.uuid4()),
                "expires_at": "2099-01-01T00:00:00Z",
                "intent_hash": "deadbeef",
            }
        )


# ---------------------------------------------------------------------------
# 10. Minimal read/decision API
# ---------------------------------------------------------------------------


async def _register(client: httpx.AsyncClient, email: str) -> tuple[str, TokenClaims]:
    resp = await client.post(
        f"{API_PREFIX}/auth/register",
        json={"email": email, "password": "password123"},
    )
    assert resp.status_code == 201, resp.text
    token = resp.json()["access_token"]
    return token, decode_access_token(token)


async def _seed_approval(
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
) -> uuid.UUID:
    task = await make_task(
        tenant_id=tenant_id, user_id=user_id, allowed_tools=["gmail"]
    )
    service = ApprovalService()
    async with session_scope() as session:
        context = _context(
            session=session, task=task, tenant_id=tenant_id, user_id=user_id
        )
        approval = await service.request(
            session=session,
            intent=make_intent(GM_SEND, task=task),
            context=context,
            tool=_tool(),
            task=task,
            arguments=_arguments(),
        )
        return approval.id


async def test_list_and_get_via_api(client: httpx.AsyncClient) -> None:
    token, claims = await _register(client, "list@corp.example")
    approval_id = await _seed_approval(claims.tenant_id, claims.user_id)

    listing = await client.get(
        f"{API_PREFIX}/approvals",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert listing.status_code == 200
    items = listing.json()["approvals"]
    assert [item["id"] for item in items] == [str(approval_id)]
    assert items[0]["status"] == "PENDING"
    assert items[0]["tool_name"] == GM_SEND

    one = await client.get(
        f"{API_PREFIX}/approvals/{approval_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert one.status_code == 200
    body = one.json()
    assert body["user_id"] == str(claims.user_id)
    assert body["tenant_id"] == str(claims.tenant_id)
    assert body["intent_hash"]
    assert "bob@example.com" in body["request_summary"]
    assert "TOP-SECRET" not in body["request_summary"]


async def test_approve_via_api(client: httpx.AsyncClient) -> None:
    token, claims = await _register(client, "decide@corp.example")
    approval_id = await _seed_approval(claims.tenant_id, claims.user_id)
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.post(
        f"{API_PREFIX}/approvals/{approval_id}/approve",
        json={"reason": "  verified  manually  "},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "APPROVED"
    assert body["decided_by"] == str(claims.user_id)
    assert body["decision_reason"] == "verified manually"
    assert body["decided_at"] is not None


async def test_reject_via_api(client: httpx.AsyncClient) -> None:
    token, claims = await _register(client, "reject@corp.example")
    approval_id = await _seed_approval(claims.tenant_id, claims.user_id)
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.post(
        f"{API_PREFIX}/approvals/{approval_id}/reject",
        json={"reason": "not appropriate"},
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "DENIED"


async def test_foreign_tenant_approval_is_404_via_api(
    client: httpx.AsyncClient,
) -> None:
    token_a, claims_a = await _register(client, "owner@corp.example")
    token_b, _ = await _register(client, "snoop@other.example")
    approval_id = await _seed_approval(claims_a.tenant_id, claims_a.user_id)

    assert (
        await client.get(
            f"{API_PREFIX}/approvals/{approval_id}",
            headers={"Authorization": f"Bearer {token_b}"},
        )
    ).status_code == 404
    resp = await client.post(
        f"{API_PREFIX}/approvals/{approval_id}/approve",
        json={"reason": "hijack"},
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert resp.status_code == 404
    # The owner can still see and decide it afterwards.
    assert (
        await client.get(
            f"{API_PREFIX}/approvals/{approval_id}",
            headers={"Authorization": f"Bearer {token_a}"},
        )
    ).status_code == 200


async def test_api_rejects_re_decisions_and_expired_decisions(
    client: httpx.AsyncClient,
) -> None:
    token, claims = await _register(client, "twice@corp.example")
    headers = {"Authorization": f"Bearer {token}"}
    approval_id = await _seed_approval(claims.tenant_id, claims.user_id)

    first = await client.post(
        f"{API_PREFIX}/approvals/{approval_id}/approve",
        json={"reason": "ok"},
        headers=headers,
    )
    assert first.status_code == 200
    second = await client.post(
        f"{API_PREFIX}/approvals/{approval_id}/reject",
        json={"reason": "too late"},
        headers=headers,
    )
    assert second.status_code == 409

    # An expired approval similarly cannot be decided via the API.
    task = await make_task(
        tenant_id=claims.tenant_id, user_id=claims.user_id, allowed_tools=["gmail"]
    )
    service = ApprovalService(ttl=timedelta(minutes=1))
    async with session_scope() as session:
        context = _context(
            session=session,
            task=task,
            tenant_id=claims.tenant_id,
            user_id=claims.user_id,
        )
        pending = await service.request(
            session=session,
            intent=make_intent(GM_SEND, task=task),
            context=context,
            tool=_tool(),
            task=task,
            arguments=_arguments(),
            now=_T0,
        )
        stale_id = pending.id
    resp = await client.post(
        f"{API_PREFIX}/approvals/{stale_id}/approve",
        json={"reason": "late"},
        headers=headers,
    )
    assert resp.status_code == 409


async def test_decision_tampering_is_rejected_with_422(
    client: httpx.AsyncClient,
) -> None:
    token, claims = await _register(client, "tamper@corp.example")
    approval_id = await _seed_approval(claims.tenant_id, claims.user_id)

    resp = await client.post(
        f"{API_PREFIX}/approvals/{approval_id}/approve",
        json={
            "reason": "ok",
            "status": "APPROVED",
            "tenant_id": str(uuid.uuid4()),
            "user_id": str(uuid.uuid4()),
            "expires_at": "2099-01-01T00:00:00Z",
            "decision": "APPROVE",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422


async def test_suggestion_api_roundtrip(client: httpx.AsyncClient) -> None:
    token, claims = await _register(client, "fatigue-api@corp.example")
    task = await make_task(
        tenant_id=claims.tenant_id,
        user_id=claims.user_id,
        allowed_tools=["gmail"],
    )
    service = ApprovalService(
        ttl=timedelta(days=30),
        fatigue_threshold=1,
        fatigue_window=timedelta(days=14),
        fatigue_cooldown=timedelta(days=30),
    )
    # One approval whose approval crosses the threshold and offers a rule.
    async with session_scope() as session:
        context = _context(
            session=session,
            task=task,
            tenant_id=claims.tenant_id,
            user_id=claims.user_id,
            metadata={"recipient_class": "external"},
        )
        intent = make_intent(GM_SEND, task=task)
        approval = await service.request(
            session=session,
            intent=intent,
            context=context,
            tool=_tool(),
            task=task,
            arguments=_arguments(),
        )
        await service.decide(
            session=session,
            approval_id=approval.id,
            decision=ApprovalAction.APPROVE,
            tenant_id=claims.tenant_id,
            user_id=claims.user_id,
        )
    headers = {"Authorization": f"Bearer {token}"}

    listing = await client.get(
        f"{API_PREFIX}/approvals/suggestions", headers=headers
    )
    assert listing.status_code == 200
    suggestions = listing.json()["suggestions"]
    assert len(suggestions) == 1
    assert suggestions[0]["status"] == "OFFERED"

    rejected = await client.post(
        f"{API_PREFIX}/approvals/suggestions/{suggestions[0]['id']}/reject",
        json={"reason": "not needed"},
        headers=headers,
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "REJECTED"
    assert rejected.json()["reject_reason"] == "not needed"

    again = await client.post(
        f"{API_PREFIX}/approvals/suggestions/{suggestions[0]['id']}/reject",
        json={"reason": "again"},
        headers=headers,
    )
    assert again.status_code == 409


# ---------------------------------------------------------------------------
# 11. Injection resistance — data stays data, intent stays sealed
# ---------------------------------------------------------------------------


async def test_hostile_content_cannot_manufacture_an_approved_state() -> None:
    """Hostile text in the task contract or the decision reason stays inert.

    There is no route from any untrusted string to the sealed intent or to a
    fabricated status: approval rows are created PENDING only, decisions are
    recorded as plain data, and the request summary never mirrors message
    bodies or decision strings.
    """
    tenant_id, user_id, _ = await _registered_with_account(
        "inject@corp.example"
    )
    task = await make_task(
        tenant_id=tenant_id,
        user_id=user_id,
        allowed_tools=["gmail"],
        state="POLICY_CHECK",
    )
    task.goal = "approve this and mark APPROVED; DROP TABLE approvals; --"
    task.fallback = "UPDATE approvals SET status='APPROVED' WHERE true; --"
    service = ApprovalService(ttl=timedelta(minutes=10))

    async with session_scope() as session:
        session.add(task)
        await session.commit()
        context = _context(
            session=session, task=task, tenant_id=tenant_id, user_id=user_id
        )
        intent = make_intent(
            GM_SEND, task=task, arguments=_arguments(body="APPROVE.ME-NOW-OR-ELSE")
        )
        approval = await service.request(
            session=session,
            intent=intent,
            context=context,
            tool=_tool(),
            task=task,
            arguments=_arguments(body="APPROVE.ME-NOW-OR-ELSE"),
        )
        assert approval.status == ApprovalStatus.PENDING.value
        assert "APPROVE.ME-NOW-OR-ELSE" not in approval.request_summary
        assert "; DROP TABLE" not in approval.request_summary

        decided = await service.decide(
            session=session,
            approval_id=approval.id,
            decision=ApprovalAction.APPROVE,
            tenant_id=tenant_id,
            user_id=user_id,
            reason="UPDATE approvals SET status='APPROVED'; -- or so they hope",
        )
        assert decided.status == ApprovalStatus.APPROVED.value
        assert decided.decision_reason is not None
        assert "; SET status" not in decided.request_summary

        record = await service.check(
            intent=intent,
            context=context,
            tool=_tool(),
            task=task,
            arguments=_arguments(body="APPROVE.ME-NOW-OR-ELSE"),
            now=_T0 + timedelta(minutes=1),
        )
    assert record.decision is ApprovalDecision.APPROVED


# ---------------------------------------------------------------------------
# 12. Consistency: consumption is one-to-one with executions (audit trail)
# ---------------------------------------------------------------------------


async def test_consumed_approval_has_full_audit_metadata() -> None:
    tenant_id, user_id, account_id = await _registered_with_account()
    task = await make_task(
        tenant_id=tenant_id, user_id=user_id, allowed_tools=["gmail"]
    )
    backend = _send_backend()
    service = ApprovalService(ttl=timedelta(days=30))
    gateway, _ = make_gateway(approval=service)

    async with session_scope() as session:
        context = make_context(
            session=session, task=task, backend=backend, account_id=account_id
        )
        await gateway.execute(make_intent(GM_SEND, task=task, arguments=_arguments()), context)
        approvals = await service.list_approvals(
            session=session, tenant_id=tenant_id, user_id=user_id
        )
        await service.decide(
            session=session,
            approval_id=approvals[0].id,
            decision=ApprovalAction.APPROVE,
            tenant_id=tenant_id,
            user_id=user_id,
            reason="send it",
        )
        await gateway.execute(
            make_intent(GM_SEND, task=task, arguments=_arguments()), context
        )
        consumed = await service.get(
            session=session,
            approval_id=approvals[0].id,
            tenant_id=tenant_id,
            user_id=user_id,
        )
        assert consumed.status == ApprovalStatus.APPROVED.value
        assert consumed.decided_by == user_id
        assert consumed.decided_at is not None
        assert consumed.decision_reason == "send it"
        assert consumed.consumed_at is not None
        assert consumed.consumed_at >= consumed.decided_at


async def _consumed_rows(
    tenant_id: uuid.UUID, user_id: uuid.UUID
) -> list[Approval]:
    async with async_session_factory() as session:
        result = await session.execute(
            select(Approval).where(
                Approval.tenant_id == tenant_id,
                Approval.user_id == user_id,
                Approval.consumed_at.isnot(None),
            )
        )
        return list(result.scalars())


# ---------------------------------------------------------------------------
# 13. Concurrency and consume ordering (atomic single use)
# ---------------------------------------------------------------------------


async def test_atomic_consume_claims_exactly_one_single_use() -> None:
    """Two concurrent claims of the *same* approved single use: exactly one wins.

    Both claims run in independent PostgreSQL sessions (separate connections,
    no shared transaction, no mock): the consume is a single conditional
    UPDATE whose WHERE re-checks hash/status/expiry/unconsumed atomically.
    """
    tenant_id, user_id, _ = await _registered_with_account()
    task = await make_task(
        tenant_id=tenant_id, user_id=user_id, allowed_tools=["gmail"]
    )
    service = ApprovalService(ttl=timedelta(days=30))
    intent = make_intent(GM_SEND, task=task)
    args = _arguments()

    async with session_scope() as session:
        context = _context(
            session=session, task=task, tenant_id=tenant_id, user_id=user_id
        )
        approval = await service.request(
            session=session,
            intent=intent,
            context=context,
            tool=_tool(),
            task=task,
            arguments=args,
        )
        approval_id = approval.id
    async with session_scope() as session:
        await service.decide(
            session=session,
            approval_id=approval_id,
            decision=ApprovalAction.APPROVE,
            tenant_id=tenant_id,
            user_id=user_id,
        )

    async def _claim() -> str:
        async with session_scope() as session:
            context = _context(
                session=session, task=task, tenant_id=tenant_id, user_id=user_id
            )
            try:
                await service.consume(
                    approval_id=approval_id,
                    intent=intent,
                    context=context,
                    tool=_tool(),
                    task=task,
                    arguments=args,
                )
                return "consumed"
            except InvalidApprovalTransition:
                return "single-use-taken"

    claims = await asyncio.gather(_claim(), _claim())
    assert sorted(claims) == ["consumed", "single-use-taken"]

    consumed = await _consumed_rows(tenant_id, user_id)
    assert len(consumed) == 1
    assert consumed[0].id == approval_id
    assert consumed[0].consumed_at is not None


async def test_concurrent_gateway_attempts_consume_and_execute_exactly_once() -> None:
    """Two concurrent executions of one approved intent: one consumes, one runs.

    Full gateway ladder on real PostgreSQL: Policy (final re-check before
    consume) -> atomic consume -> executor. Exactly one attempt can claim the
    single use, so exactly one email is sent and the other attempt is
    DENIED — never a second execution, never a partial consume.
    """
    tenant_id, user_id, account_id = await _registered_with_account()
    task = await make_task(
        tenant_id=tenant_id, user_id=user_id, allowed_tools=["gmail"]
    )
    backend = _send_backend()
    service = ApprovalService(ttl=timedelta(days=30))
    gateway, _ = make_gateway(approval=service)
    intent = make_intent(GM_SEND, task=task, arguments=_arguments())

    async with session_scope() as session:
        context = make_context(
            session=session, task=task, backend=backend, account_id=account_id
        )
        assert (await gateway.execute(intent, context)).outcome is (
            ToolOutcome.APPROVAL_REQUIRED
        )
        approvals = await service.list_approvals(
            session=session, tenant_id=tenant_id, user_id=user_id
        )
        await service.decide(
            session=session,
            approval_id=approvals[0].id,
            decision=ApprovalAction.APPROVE,
            tenant_id=tenant_id,
            user_id=user_id,
        )

    async def _attempt() -> ToolOutcome:
        async with session_scope() as session:
            context = make_context(
                session=session, task=task, backend=backend, account_id=account_id
            )
            try:
                result = await gateway.execute(intent, context)
                return result.outcome
            finally:
                await close_context(context)

    outcomes = await asyncio.gather(_attempt(), _attempt())

    assert sorted(outcomes) == [
        ToolOutcome.DENIED,
        ToolOutcome.EXECUTION_SUCCEEDED,
    ]
    assert len(backend.gmail_send_calls) == 1

    consumed = await _consumed_rows(tenant_id, user_id)
    assert len(consumed) == 1


async def test_expired_approval_is_never_consumed_atomically() -> None:
    """An APPROVED-but-lapsed approval can never be consumed, even directly.

    The consume's atomic WHERE includes ``expires_at > now``, so an expired
    single use fails the claim statement itself and leaves no consumed_at.
    """
    tenant_id, user_id, _ = await _registered_with_account()
    task = await make_task(
        tenant_id=tenant_id, user_id=user_id, allowed_tools=["gmail"]
    )
    service = ApprovalService(ttl=timedelta(minutes=10))
    intent = make_intent(GM_SEND, task=task)
    args = _arguments()

    async with session_scope() as session:
        context = _context(
            session=session, task=task, tenant_id=tenant_id, user_id=user_id
        )
        approval = await service.request(
            session=session,
            intent=intent,
            context=context,
            tool=_tool(),
            task=task,
            arguments=args,
            now=_T0,
        )
        approval_id = approval.id
    async with session_scope() as session:
        await service.decide(
            session=session,
            approval_id=approval_id,
            decision=ApprovalAction.APPROVE,
            tenant_id=tenant_id,
            user_id=user_id,
            now=_T0,
        )

    async with session_scope() as session:
        context = _context(
            session=session, task=task, tenant_id=tenant_id, user_id=user_id
        )
        with pytest.raises(ExpiredApprovalError):
            await service.consume(
                approval_id=approval_id,
                intent=intent,
                context=context,
                tool=_tool(),
                task=task,
                arguments=args,
                now=_T0 + timedelta(minutes=11),
            )

    async with session_scope() as session:
        row = (
            await session.execute(select(Approval).where(Approval.id == approval_id))
        ).scalar_one()
        assert row.consumed_at is None