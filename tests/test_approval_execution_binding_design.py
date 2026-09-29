"""The leg of the approval chain that no test protected.

Four earlier rounds pinned the digest: the body is in it, the account is in it, an
invisible edit breaks it, and ``test_the_body_is_bound_to_the_approval_and_never_
shown_to_the_approver`` stands as the regression guard. Every one of those
assertions is made by calling
``canonical_payload``/``intent_hash`` **directly**, which proves the hash is a
function of its arguments — a property of SHA-256, not of the gateway.

That leaves the last link unmeasured: *which argument map actually reaches the
executor, and which bytes actually reach the provider.* The gateway hands one
local variable to ``check``, to ``consume`` and to ``execute``, and the adapter
reads three keys out of what it is given. Both steps are real code and neither
was pinned, so two mutations pass the entire suite untouched:

* **M1** — the adapter sends ``body="INJECTED: never hashed, never approved"``
  instead of ``arguments["body"]``. 154 tests pass. The mail really leaves the
  building with text that was never approved and never hashed.
* **M8** — the gateway rebuilds ``arguments`` after the approval is consumed and
  executes the rebuild. 154 tests pass. The executed map is no longer the map
  that was hashed.

Both are invisible because the suite asserts *what the hash covers* and never
*what the hash is compared against*. This file closes that: it runs the real
gateway, a real ``ApprovalService``, and the real Postgres row, and it asserts
the chain end to end —

    approver decision -> stored intent_hash -> executor arguments -> provider bytes

The two properties below are the ones M1 and M8 break, and the two after them
are the ones they would be mistaken for, kept here so the distinction stays
visible: an approval binding a *changed* field is a different claim from an
approval binding the *right* field while something downstream quietly discards it.
"""

from __future__ import annotations

import base64
import dataclasses
import json
from collections.abc import Mapping
from datetime import timedelta
from typing import Any, Final
from uuid import UUID

import pytest

from kai.approvals.service import ApprovalAction, ApprovalService, intent_hash
from kai.tools.context import ToolContext
from kai.tools.execution import ToolExecutionOutcome
from kai.tools.registry import ToolRegistry
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

GM_SEND: Final = "gmail.send"
ALICE: Final = "alice@example.com"
BODY: Final = "Internal: the deal closes Friday at 40k."


def _args(**overrides: Any) -> dict[str, Any]:
    return {"to": ALICE, "subject": "Quarterly plan", "body": BODY, **overrides}


def _backend() -> GoogleBackend:
    """A Gmail backend whose read-after-send agrees with :func:`_args`.

    The readback has to match, or the verification boundary reports
    ``VERIFICATION_FAILED`` and the test measures the wrong thing. It is worth
    being explicit that the readback is checked against ``label_ids``,
    ``sender``, ``recipient`` and ``subject`` — and, as the M1 result below shows,
    not against ``body``.
    """
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


class _RecordingExecutor:
    """Wraps the real gmail executor and keeps what the gateway actually handed it.

    The point is to observe the ``arguments`` object *at the executor boundary*,
    which is the only place where "the map that was hashed" and "the map that
    runs" can still be told apart.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls: list[tuple[dict[str, Any], UUID | None]] = []

    async def execute(
        self, *, tool: Any, arguments: Mapping[str, Any], context: ToolContext
    ) -> ToolExecutionOutcome:
        self.calls.append((dict(arguments), context.account_id))
        return await self._inner.execute(tool=tool, arguments=arguments, context=context)


def _recording_registry(executor: _RecordingExecutor) -> ToolRegistry:
    """The production catalog, with ``gmail.send``'s executor swapped for a spy."""
    from kai.tools.catalog import default_registry

    original = default_registry().get(GM_SEND)
    spy = dataclasses.replace(original, executor=executor)
    others = [
        default_registry().get(name)
        for name in (
            "calendar.create_event",
            "calendar.read_slots",
            "calendar.get_event",
            "contacts.match",
        )
    ]
    return ToolRegistry.build([spy, *others])


async def test_the_executor_receives_the_exact_arguments_that_were_hashed() -> None:
    """The M8 regression guard: hashed map == executed map.

    ``ApprovalService.consume`` already re-derives the digest from the arguments it
    is given and compares it to the stored one in the same UPDATE. So by the time
    an execution is allowed, the map the gateway holds *is* the approved map. What
    that proves is about the map at the ``consume`` call — not about the map at the
    ``execute`` call one statement later. M8 moves the second without moving the
    first, and nothing in the suite noticed.

    So the assertion is made at the seam: whatever reaches the executor, hashed
    with whatever account it runs under, must reproduce the stored ``intent_hash``.
    That is the whole definition of "the approved canonical payload was executed",
    and it is the one M8 breaks.
    """
    from kai.tools.catalog import default_registry

    tenant, user = await make_tenant_user("owner@corp.example")
    account = await insert_account(
        tenant_id=tenant.id, user_id=user.id, email="owner@corp.example", sub="sub-1"
    )
    task = await make_task(tenant_id=tenant.id, user_id=user.id, allowed_tools=["gmail"])
    backend = _backend()
    service = ApprovalService(ttl=timedelta(days=30))

    real = default_registry().get(GM_SEND)
    spy = _RecordingExecutor(real.executor)
    gateway, _ = make_gateway(approval=service, registry=_recording_registry(spy))

    async with session_scope() as session:
        context = make_context(
            session=session, task=task, backend=backend, account_id=account.id
        )
        intent = make_intent(GM_SEND, task=task, arguments=_args())

        held = await gateway.execute(intent, context)
        assert held.outcome is ToolOutcome.APPROVAL_REQUIRED
        assert not spy.calls, "nothing may reach the executor before the decision"

        approvals = await service.list_approvals(
            session=session, tenant_id=tenant.id, user_id=user.id
        )
        await service.decide(
            session=session,
            approval_id=approvals[0].id,
            decision=ApprovalAction.APPROVE,
            tenant_id=tenant.id,
            user_id=user.id,
        )
        approved_hash = approvals[0].intent_hash

        done = await gateway.execute(intent, context)
        assert done.outcome is ToolOutcome.EXECUTION_SUCCEEDED

        assert len(spy.calls) == 1, "exactly one execution, one observed argument map"
        executed_arguments, executed_account = spy.calls[0]

        # The property M8 breaks: the executed map, re-hashed under the account it
        # ran with, is the very digest the human approved.
        assert (
            intent_hash(
                tool=real,
                arguments=executed_arguments,
                account_id=executed_account,
            )
            == approved_hash
        ), (
            "the gateway executed an argument map that is not the one it hashed: "
            f"executed={executed_arguments!r} approved_hash={approved_hash}"
        )
        # And the account is part of that same statement, not a separate promise.
        assert executed_account == account.id


async def test_the_provider_receives_the_approved_body_not_a_replacement() -> None:
    """The M1 regression guard: hashed body == transmitted body.

    M1 rewrote one line in the adapter so the send carried a constant instead of
    ``arguments["body"]``, and the suite stayed green. The adapter's fidelity is
    the last unmeasured link: everything upstream of it is pinned, and the bytes
    that leave the process are the only thing the recipient ever sees.

    The check is deliberately made on the wire, not on ``ToolResult.data``, because
    the result is assembled *from* the same execution and would happily agree with
    a substitution. ``gmail_send_calls`` holds the real ``httpx.Request``, so the
    assertion is about the bytes Gmail was actually handed.
    """
    tenant, user = await make_tenant_user("owner@corp.example")
    account = await insert_account(
        tenant_id=tenant.id, user_id=user.id, email="owner@corp.example", sub="sub-1"
    )
    task = await make_task(tenant_id=tenant.id, user_id=user.id, allowed_tools=["gmail"])
    backend = _backend()
    service = ApprovalService(ttl=timedelta(days=30))
    gateway, _ = make_gateway(approval=service)

    async with session_scope() as session:
        context = make_context(
            session=session, task=task, backend=backend, account_id=account.id
        )
        intent = make_intent(GM_SEND, task=task, arguments=_args())
        await gateway.execute(intent, context)
        approvals = await service.list_approvals(
            session=session, tenant_id=tenant.id, user_id=user.id
        )
        await service.decide(
            session=session,
            approval_id=approvals[0].id,
            decision=ApprovalAction.APPROVE,
            tenant_id=tenant.id,
            user_id=user.id,
        )
        done = await gateway.execute(intent, context)
        assert done.outcome is ToolOutcome.EXECUTION_SUCCEEDED

        assert len(backend.gmail_send_calls) == 1
        envelope = json.loads(backend.gmail_send_calls[0].read().decode("utf-8"))
        raw = base64.urlsafe_b64decode(envelope["raw"] + "==").decode("utf-8")
        wire_body = raw.split("\r\n\r\n", 1)[1]

        assert wire_body == BODY, (
            "the provider received text that was never hashed or approved: "
            f"{wire_body!r}"
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("to", "carol@example.com"),
        ("subject", "a completely different subject"),
        ("body", "Transfer $500k to mallory@evil.example"),
    ],
)
async def test_an_approved_field_cannot_be_swapped_before_execution(
    field: str, value: str
) -> None:
    """The binding the suite *does* protect, restated at the gateway.

    Worth keeping beside the two above, because the difference is the whole point:
    here the approval is asked to cover a field it never covered, and the digest
    says no. In M1 and M8 the approval covers exactly the right field and
    something downstream discards it anyway. Only one of those is a hash problem.
    """
    tenant, user = await make_tenant_user("owner@corp.example")
    account = await insert_account(
        tenant_id=tenant.id, user_id=user.id, email="owner@corp.example", sub="sub-1"
    )
    task = await make_task(tenant_id=tenant.id, user_id=user.id, allowed_tools=["gmail"])
    backend = _backend()
    service = ApprovalService(ttl=timedelta(days=30))
    gateway, _ = make_gateway(approval=service)

    async with session_scope() as session:
        context = make_context(
            session=session, task=task, backend=backend, account_id=account.id
        )
        await gateway.execute(make_intent(GM_SEND, task=task, arguments=_args()), context)
        approvals = await service.list_approvals(
            session=session, tenant_id=tenant.id, user_id=user.id
        )
        await service.decide(
            session=session,
            approval_id=approvals[0].id,
            decision=ApprovalAction.APPROVE,
            tenant_id=tenant.id,
            user_id=user.id,
        )

        tampered = make_intent(GM_SEND, task=task, arguments=_args(**{field: value}))
        blocked = await gateway.execute(tampered, context)

        assert blocked.outcome is ToolOutcome.APPROVAL_REQUIRED
        assert not backend.gmail_send_calls, "a swapped field must reach nobody"


async def test_an_approved_account_cannot_be_swapped_before_execution() -> None:
    """``account`` is hash-bound and unauthorised by argument, so it is the one field
    whose divergence can only be tested by moving the context rather than the intent.

    A second *valid* account for the same tenant and user is used, so the probe
    cannot pass or fail on the lookup failing: both mailboxes exist, both are
    usable, and the only thing separating them is the digest. This is also the
    answer to whether ``account`` is execution-bound — it is, through the same
    re-derivation as every other field, and it is worth a test because no argument
    of the intent can ever carry it.
    """
    tenant, user = await make_tenant_user("owner@corp.example")
    approved_account = await insert_account(
        tenant_id=tenant.id, user_id=user.id, email="owner@corp.example", sub="sub-1"
    )
    other_account = await insert_account(
        tenant_id=tenant.id, user_id=user.id, email="second@corp.example", sub="sub-2"
    )
    task = await make_task(tenant_id=tenant.id, user_id=user.id, allowed_tools=["gmail"])
    backend = _backend()
    service = ApprovalService(ttl=timedelta(days=30))
    gateway, _ = make_gateway(approval=service)
    intent = make_intent(GM_SEND, task=task, arguments=_args())

    async with session_scope() as session:
        first = make_context(
            session=session, task=task, backend=backend, account_id=approved_account.id
        )
        await gateway.execute(intent, first)
        approvals = await service.list_approvals(
            session=session, tenant_id=tenant.id, user_id=user.id
        )
        await service.decide(
            session=session,
            approval_id=approvals[0].id,
            decision=ApprovalAction.APPROVE,
            tenant_id=tenant.id,
            user_id=user.id,
        )

        swapped = make_context(
            session=session, task=task, backend=backend, account_id=other_account.id
        )
        blocked = await gateway.execute(intent, swapped)

        assert blocked.outcome is ToolOutcome.APPROVAL_REQUIRED
        assert not backend.gmail_send_calls

        # And the original approval is left unconsumed rather than burnt by the probe.
        after = await service.get(
            session=session,
            approval_id=approvals[0].id,
            tenant_id=tenant.id,
            user_id=user.id,
        )
        assert after.consumed_at is None
