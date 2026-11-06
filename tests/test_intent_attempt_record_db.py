"""O1-B and P2-A against the real PostgreSQL, the real ApprovalService, the real DB.

The no-database module (``tests/test_intent_attempt_record.py``) pins the
gateway's *seam*: one ``add``, on the caller's session, before ``consume``, and
no commit that originates in the gateway. It cannot pin durability, because
durability is a property of ``ApprovalService.consume``'s commits and of the
database.

This module pins exactly those claims, and only those:

* the attempt row is durable after a **successful** single-use claim;
* the attempt row is durable after a **lost** claim (CAS failure), while the
  approval was **not** consumed by this attempt -- because ``consume`` commits
  the caller's session before it raises. No "both or nothing" is asserted;
* after a lost claim the approval must not be assumed intact: the test
  deliberately makes a concurrent attempt consume it first and then re-reads the
  row instead of assuming;
* the reader is read-only over real rows: it returns one tenant's rows only,
  newest first, bounded, and it performs no lazy expiry and no write;
* the reader lists only attempts with **no AUTHORIZED reference recorded**: a row
  that already carries one is stored but excluded, so the predicate is pinned in
  both directions;
* attempt rows are deleted with their owning task (``ON DELETE CASCADE``), so
  the table is a bounded retention surface, not a permanent audit record.

These are the repository's normal DB tests: a real PostgreSQL, no SQLite, no
mocks of the service layer. The gateway is wrapped in a tiny proxy only to
create a *deterministic interleaving* (another attempt consuming the approval
between ``check`` and ``consume``); the approval semantics stay the real ones.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select

from kai.approvals.models import Approval, ApprovalStatus
from kai.approvals.service import ApprovalAction, ApprovalService
from kai.audit.reader import list_intent_attempts_without_authorization
from kai.db.models import IntentAttempt
from kai.tasks.models import Task
from kai.tools.boundaries import ApprovalDecision, ApprovalDecisionRecord
from kai.tools.context import ToolContext
from kai.tools.result import ToolOutcome
from tool_harness import (
    GoogleBackend,
    insert_account,
    make_context,
    make_gateway,
    make_intent,
    make_task,
    make_tenant_user,
    message_payload,
    session_scope,
)

GM_SEND = "gmail.send"
ALICE = "alice@example.com"
BODY = "Internal: the deal closes Friday at 40k."


def _args() -> dict[str, Any]:
    return {"to": ALICE, "subject": "Quarterly plan", "body": BODY}


def _backend() -> GoogleBackend:
    backend = GoogleBackend()
    backend.send_return = message_payload(
        message_id="sent-message-1",
        sender="Owner <owner@corp.example>",
        to=(ALICE,),
        subject="Quarterly plan",
        body_text=BODY,
        labels=("SENT",),
    )
    return backend


class _RacingApproval:
    """Real ApprovalService, with one deterministic race injected after ``check``.

    ``check`` delegates to the real service and, on the first APPROVED verdict,
    lets a *second* attempt consume the same approval in its own session before
    returning. The gateway then calls ``consume`` on its own session, which is the
    real CAS failure: zero rows matched. Nothing about the approval's behaviour is
    faked -- only the interleaving is forced, which a single-threaded test cannot
    otherwise reproduce.
    """

    def __init__(self, service: ApprovalService) -> None:
        self._service = service
        self.raced = False
        self.race_consumed = False

    async def check(
        self,
        *,
        intent: Any,
        context: ToolContext,
        tool: Any,
        task: Any,
        arguments: Any,
    ) -> ApprovalDecisionRecord:
        record = await self._service.check(
            intent=intent,
            context=context,
            tool=tool,
            task=task,
            arguments=arguments,
        )
        if record.decision is ApprovalDecision.APPROVED and not self.raced:
            self.raced = True
            assert record.approval_id is not None
            async with session_scope() as other:
                await self._service.consume(
                    approval_id=record.approval_id,
                    intent=intent,
                    context=ToolContext(
                        session=other,
                        task_id=context.task_id,
                        tenant_id=context.tenant_id,
                        user_id=context.user_id,
                        account_id=context.account_id,
                    ),
                    tool=tool,
                    task=task,
                    arguments=arguments,
                )
            self.race_consumed = True
        return record

    async def consume(self, **kwargs: Any) -> None:
        await self._service.consume(**kwargs)


async def _attempt_rows(tenant_id: uuid.UUID) -> list[IntentAttempt]:
    """Read the attempts back in a *fresh* session, so only durability is seen."""
    async with session_scope() as session:
        result = await session.execute(
            select(IntentAttempt)
            .where(IntentAttempt.tenant_id == tenant_id)
            .order_by(IntentAttempt.created_at.asc())
        )
        return list(result.scalars().all())


async def _approve(
    service: ApprovalService,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    session: Any,
) -> Approval:
    approvals = await service.list_approvals(session=session, tenant_id=tenant_id, user_id=user_id)
    assert len(approvals) == 1
    return await service.decide(
        session=session,
        approval_id=approvals[0].id,
        decision=ApprovalAction.APPROVE,
        tenant_id=tenant_id,
        user_id=user_id,
    )


async def test_the_attempt_is_durable_after_a_successful_claim() -> None:
    """Success path: one durable attempt row, bound to the approved intent."""
    tenant, user = await make_tenant_user("owner@corp.example")
    account = await insert_account(
        tenant_id=tenant.id, user_id=user.id, email="owner@corp.example", sub="sub-1"
    )
    task = await make_task(tenant_id=tenant.id, user_id=user.id, allowed_tools=["gmail"])
    backend = _backend()
    service = ApprovalService(ttl=timedelta(days=30))
    gateway, _ = make_gateway(approval=service)

    async with session_scope() as session:
        context = make_context(session=session, task=task, backend=backend, account_id=account.id)
        intent = make_intent(GM_SEND, task=task, arguments=_args())
        held = await gateway.execute(intent, context)
        assert held.outcome is ToolOutcome.APPROVAL_REQUIRED
        assert await _attempt_rows(tenant.id) == [], (
            "an unapproved intent never reaches the claim, so it writes no attempt"
        )

        approval = await _approve(service, tenant_id=tenant.id, user_id=user.id, session=session)
        done = await gateway.execute(intent, context)
        assert done.outcome is ToolOutcome.EXECUTION_SUCCEEDED

    rows = await _attempt_rows(tenant.id)
    assert len(rows) == 1, "one attempt for the one execution that was allowed"
    row = rows[0]
    assert row.approval_id == approval.id
    assert row.intent_hash == approval.intent_hash
    assert row.tool_name == GM_SEND
    assert row.account_id == account.id
    assert row.task_id == task.id
    assert row.user_id == user.id
    assert row.created_at is not None, "created_at is database time"
    # No L2 writer exists, so an AUTHORIZED reference cannot be present.
    assert row.authorization_id is None
    assert row.authorized_at_utc is None

    # The reader's own query -- not a raw SELECT -- returns that same row: the
    # writer's NULL ``authorization_id`` is exactly what the endpoint's predicate
    # selects, so an O1-B record is retrievable through
    # ``GET /api/v1/audit/intent-attempts`` for its own tenant/user.
    async with session_scope() as session:
        listed = await list_intent_attempts_without_authorization(
            session, tenant_id=tenant.id, user_id=user.id
        )
    assert [item.id for item in listed] == [row.id]


async def test_a_lost_claim_still_leaves_the_attempt_durable() -> None:
    """CAS failure: the row commits, the approval was not consumed *here*.

    This is the accepted limit, measured instead of assumed. ``consume`` commits
    the caller's session before raising, so the attempt row is durable even
    though this attempt lost the claim -- and the approval is not intact either,
    because a concurrent attempt consumed it. Both facts are read back from the
    database; neither is inferred from the attempt row.
    """
    tenant, user = await make_tenant_user("owner@corp.example")
    account = await insert_account(
        tenant_id=tenant.id, user_id=user.id, email="owner@corp.example", sub="sub-1"
    )
    task = await make_task(tenant_id=tenant.id, user_id=user.id, allowed_tools=["gmail"])
    backend = _backend()
    service = ApprovalService(ttl=timedelta(days=30))
    racing = _RacingApproval(service)
    gateway, _ = make_gateway(approval=racing)

    async with session_scope() as session:
        context = make_context(session=session, task=task, backend=backend, account_id=account.id)
        intent = make_intent(GM_SEND, task=task, arguments=_args())
        await gateway.execute(intent, context)
        approval = await _approve(service, tenant_id=tenant.id, user_id=user.id, session=session)

        blocked = await gateway.execute(intent, context)

        assert blocked.outcome is ToolOutcome.DENIED
        assert racing.race_consumed is True
        # The attempt never reached the executor: a lost claim dispatches nothing.
        assert not backend.gmail_send_calls

        # Re-read the approval rather than assuming its state: the racing attempt
        # consumed it, so it is NOT intact after this failure.
        after = await service.get(
            session=session,
            approval_id=approval.id,
            tenant_id=tenant.id,
            user_id=user.id,
        )
        assert after.consumed_at is not None, (
            "after a CAS failure the approval must be re-read; here a concurrent "
            "attempt consumed it"
        )

    rows = await _attempt_rows(tenant.id)
    assert len(rows) == 1, (
        "the attempt row is durable even though the approval was not consumed by "
        "this attempt -- there is no 'both or nothing' atomicity"
    )
    assert rows[0].approval_id == approval.id
    assert rows[0].authorization_id is None


async def test_the_reader_is_read_only_and_does_not_lazily_expire_approvals() -> None:
    """The reader touches nothing: no write, no expiry, no commit."""
    tenant, user = await make_tenant_user("owner@corp.example")
    task = await make_task(tenant_id=tenant.id, user_id=user.id)
    past = datetime.now(UTC) - timedelta(minutes=5)

    async with session_scope() as session:
        # A PENDING approval that is already past its expiry: any read path that
        # lazily expires rows would flip it to EXPIRED.
        stale = Approval(
            tenant_id=tenant.id,
            user_id=user.id,
            task_id=task.id,
            tool_name=GM_SEND,
            risk_level="HIGH",
            status=ApprovalStatus.PENDING.value,
            recipient_class="",
            intent_hash="b" * 64,
            request_summary="pending and expired",
            expires_at=past,
        )
        session.add(stale)
        session.add(
            IntentAttempt(
                tenant_id=tenant.id,
                user_id=user.id,
                task_id=task.id,
                approval_id=uuid.uuid4(),
                tool_name=GM_SEND,
                intent_hash="b" * 64,
                account_id=None,
            )
        )
        await session.commit()
        stale_id = stale.id

    async with session_scope() as session:
        before = await session.execute(select(func.count(IntentAttempt.id)))
        rows = await list_intent_attempts_without_authorization(
            session, tenant_id=tenant.id, user_id=user.id
        )
        assert len(rows) == 1
        # No commit is issued by the reader; the session is still usable and the
        # counts are unchanged.
        after = await session.execute(select(func.count(IntentAttempt.id)))
        assert before.scalar_one() == after.scalar_one() == 1
        still = await session.execute(select(Approval.status).where(Approval.id == stale_id))
        assert still.scalar_one() == ApprovalStatus.PENDING.value, (
            "the reader must not lazily expire approvals: that would be a write"
        )


async def test_the_reader_isolates_tenants_and_orders_newest_first() -> None:
    """One tenant's rows only, newest first, and the limit is honoured."""
    tenant_a, user_a = await make_tenant_user("a@corp.example")
    tenant_b, user_b = await make_tenant_user("b@corp.example")
    task_a = await make_task(tenant_id=tenant_a.id, user_id=user_a.id)
    task_b = await make_task(tenant_id=tenant_b.id, user_id=user_b.id)
    base = datetime.now(UTC) - timedelta(hours=1)

    async with session_scope() as session:
        for offset in range(3):
            session.add(
                IntentAttempt(
                    tenant_id=tenant_a.id,
                    user_id=user_a.id,
                    task_id=task_a.id,
                    approval_id=uuid.uuid4(),
                    tool_name=GM_SEND,
                    intent_hash=f"{offset}" * 64,
                    created_at=base + timedelta(minutes=offset),
                )
            )
        session.add(
            IntentAttempt(
                tenant_id=tenant_b.id,
                user_id=user_b.id,
                task_id=task_b.id,
                approval_id=uuid.uuid4(),
                tool_name=GM_SEND,
                intent_hash="c" * 64,
                created_at=base + timedelta(minutes=10),
            )
        )
        await session.commit()

    async with session_scope() as session:
        rows_a = await list_intent_attempts_without_authorization(
            session, tenant_id=tenant_a.id, user_id=user_a.id
        )
        assert {row.tenant_id for row in rows_a} == {tenant_a.id}
        assert [row.intent_hash for row in rows_a] == ["2" * 64, "1" * 64, "0" * 64], "newest first"

        limited = await list_intent_attempts_without_authorization(
            session, tenant_id=tenant_a.id, user_id=user_a.id, limit=1
        )
        assert [row.intent_hash for row in limited] == ["2" * 64]

        rows_b = await list_intent_attempts_without_authorization(
            session, tenant_id=tenant_b.id, user_id=user_b.id
        )
        assert {row.tenant_id for row in rows_b} == {tenant_b.id}
        assert len(rows_b) == 1

        # Tenant A cannot name tenant B: the tenant is a required argument that
        # the route fills from the principal, and there is no default.
        assert all(row.tenant_id != tenant_b.id for row in rows_a)


async def test_the_reader_excludes_attempts_with_an_authorized_reference() -> None:
    """Intended rows appear; a row that already has an AUTHORIZED link does not.

    No writer sets ``authorization_id`` today, so this pins the predicate forward
    rather than describing current data. The endpoint's question is "attempts
    with no AUTHORIZED reference recorded", so a row that carries one is out of
    scope. The row is still stored -- the exclusion must come from the predicate,
    not from a missing insert.
    """
    tenant, user = await make_tenant_user("authz@corp.example")
    task = await make_task(tenant_id=tenant.id, user_id=user.id)

    async with session_scope() as session:
        session.add(
            IntentAttempt(
                tenant_id=tenant.id,
                user_id=user.id,
                task_id=task.id,
                approval_id=uuid.uuid4(),
                tool_name=GM_SEND,
                intent_hash="d" * 64,
            )
        )
        session.add(
            IntentAttempt(
                tenant_id=tenant.id,
                user_id=user.id,
                task_id=task.id,
                approval_id=uuid.uuid4(),
                tool_name=GM_SEND,
                intent_hash="e" * 64,
                authorization_id=uuid.uuid4(),
                authorized_at_utc=datetime.now(UTC),
            )
        )
        await session.commit()

    async with session_scope() as session:
        total = (await session.execute(select(func.count(IntentAttempt.id)))).scalar_one()
        rows = await list_intent_attempts_without_authorization(
            session, tenant_id=tenant.id, user_id=user.id
        )

    assert total == 2, "both rows are stored; only one is eligible"
    assert [row.intent_hash for row in rows] == ["d" * 64]
    assert rows[0].authorization_id is None


async def test_attempt_rows_are_deleted_with_their_owning_task() -> None:
    """Retention: an attempt is not a permanent audit record.

    ``task_id`` is an ``ON DELETE CASCADE`` foreign key, so the row dies with the
    task. Asserted against the real database rather than only documented,
    because calling this an "audit record" without qualification would suggest
    permanence that the schema does not provide.
    """
    tenant, user = await make_tenant_user("cascade@corp.example")
    task = await make_task(tenant_id=tenant.id, user_id=user.id)

    async with session_scope() as session:
        session.add(
            IntentAttempt(
                tenant_id=tenant.id,
                user_id=user.id,
                task_id=task.id,
                approval_id=uuid.uuid4(),
                tool_name=GM_SEND,
                intent_hash="f" * 64,
            )
        )
        await session.commit()

    assert len(await _attempt_rows(tenant.id)) == 1

    async with session_scope() as session:
        await session.execute(delete(Task).where(Task.id == task.id))
        await session.commit()

    assert await _attempt_rows(tenant.id) == [], (
        "attempt rows are deleted with the owning task; this table is not a permanent audit log"
    )
