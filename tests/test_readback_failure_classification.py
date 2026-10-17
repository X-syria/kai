"""A failed read-back after an accepted external write is not an execution failure.

NEW-STATIC-01: a write the provider accepted, whose read-back then failed with
401/403/429, was classified as ``EXECUTION_FAILED`` and drove the task to the
terminal ``FAILED`` state — the same state KAI uses to say "nothing happened".
That is the duplicate-inviting direction: a create the provider accepted must
never be reported as a clean failure, or a caller reading the failure will
re-issue it and create a second event.

These tests pin the corrected classification:

* the calendar governed path (through ``ToolGateway``) reports an accepted write
  whose read-back failed as ``REQUIRES_ATTENTION`` and keeps the provider id;
* a failure *before* the provider accepted the write stays ``EXECUTION_FAILED``;
* the Gmail service boundary classifies read-back failures the same way, and a
  send that never reached the provider still propagates its error.
"""

from __future__ import annotations

import httpx
import pytest

from kai.gmail.client import (
    GoogleForbiddenError,
    GoogleInvalidTokenError,
    GoogleRateLimitError,
)
from kai.gmail.service import SendRequest, send_and_verify
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
    session_scope,
)

CALENDAR_ARGS = {
    "title": "Team Sync",
    "date": "2026-06-24",
    "start_time": "10:00",
    "end_time": "11:00",
    "timezone": "Europe/Berlin",
}

#: The id the harness assigns to the first create it accepts.
_FIRST_CREATED_EVENT_ID = "created-event-1"


async def _run_calendar(backend: GoogleBackend):
    """Drive one ``calendar.create_event`` through the real gateway.

    ``allowed_tools`` is spelled as the concrete tool name the profile permits,
    so the fixture exercises the gateway rather than a profile refusal.
    """
    tenant, user = await make_tenant_user("acme@corp.example")
    account = await insert_account(
        tenant_id=tenant.id, user_id=user.id, email="alice@example.com", sub="sub-1"
    )
    task = await make_task(
        tenant_id=tenant.id,
        user_id=user.id,
        allowed_tools=["calendar.create_event"],
    )
    gateway, _ = make_gateway()
    async with session_scope() as session:
        context = make_context(
            session=session, task=task, backend=backend, account_id=account.id
        )
        result = await gateway.execute(
            make_intent("calendar.create_event", task=task, arguments=CALENDAR_ARGS),
            context,
        )
        await close_context(context)
    return result


async def _send_and_verify(backend: GoogleBackend):
    """Call the Gmail write service directly, on a real account and session."""
    tenant, user = await make_tenant_user("acme@corp.example")
    account = await insert_account(
        tenant_id=tenant.id, user_id=user.id, email="alice@example.com", sub="sub-1"
    )
    transport = httpx.AsyncClient(
        transport=httpx.MockTransport(backend.handler),
        base_url="http://google.api.test",
        timeout=15.0,
    )
    try:
        async with session_scope() as session:
            return await send_and_verify(
                session,
                transport,
                account_id=account.id,
                tenant_id=tenant.id,
                req=SendRequest(
                    to="bob@example.com",
                    subject="Meeting tomorrow",
                    body="Hi Bob, let's sync tomorrow at 10.",
                ),
            )
    finally:
        await transport.aclose()


# --- the reported defect: accepted write, read-back 401/403/429 ---------------


@pytest.mark.parametrize("status", [401, 403, 429])
async def test_calendar_readback_failure_after_accepted_write_is_not_execution_failure(
    status: int,
) -> None:
    backend = GoogleBackend()
    backend.calendar_read_status = status

    result = await _run_calendar(backend)

    assert result.outcome is ToolOutcome.REQUIRES_ATTENTION
    assert result.ok is False
    assert result.verified is False
    assert result.error_code == "VERIFICATION_FAILED"
    # The provider's accepted write is preserved as evidence, not discarded.
    assert result.evidence.get("event_id") == _FIRST_CREATED_EVENT_ID
    # Exactly one write reached the provider: the failure never triggered a retry.
    posts = [call for call in backend.calendar_calls if call.method == "POST"]
    assert len(posts) == 1
    assert len(backend.created_events) == 1


@pytest.mark.parametrize(
    "status,expected_code",
    [(401, "TOKEN_INVALID"), (403, "PERMISSION_DENIED"), (429, "RATE_LIMITED")],
)
async def test_calendar_create_failure_before_write_stays_execution_failure(
    status: int, expected_code: str
) -> None:
    """Negative control: if the provider never accepted the write, it failed."""
    backend = GoogleBackend()
    backend.create_event_status = status

    result = await _run_calendar(backend)

    assert result.outcome is ToolOutcome.EXECUTION_FAILED
    assert result.ok is False
    assert result.verified is False
    assert result.error_code == expected_code
    # Nothing was written, so there is nothing to preserve or retry around.
    assert backend.created_events == {}


# --- retained behaviour: success and the pre-existing 404 read-back path ------


async def test_calendar_accepted_and_verified_write_still_succeeds() -> None:
    backend = GoogleBackend()

    result = await _run_calendar(backend)

    assert result.outcome is ToolOutcome.EXECUTION_SUCCEEDED
    assert result.ok is True
    assert result.verified is True
    assert result.evidence.get("event_id") == _FIRST_CREATED_EVENT_ID


async def test_calendar_unreadable_readback_404_is_still_requires_attention() -> None:
    backend = GoogleBackend()
    backend.calendar_read_status = 404

    result = await _run_calendar(backend)

    assert result.outcome is ToolOutcome.REQUIRES_ATTENTION
    assert result.verified is False
    assert result.evidence.get("event_id") == _FIRST_CREATED_EVENT_ID


# --- Gmail: the same classification at the service boundary -------------------


@pytest.mark.parametrize("status", [401, 403, 429])
async def test_gmail_readback_failure_after_accepted_send_is_unverified(
    status: int,
) -> None:
    backend = GoogleBackend()
    backend.readback_status = status

    outcome = await _send_and_verify(backend)

    assert outcome.verified is False
    assert outcome.message is None
    assert any(m.field == "read_after_send" for m in outcome.mismatches)
    # The send itself reached the provider, so the failure is not a "nothing happened".
    assert backend.send_raw is not None
    assert len(backend.gmail_send_calls) == 1


async def test_gmail_unreadable_readback_404_is_still_unverified() -> None:
    backend = GoogleBackend()
    backend.readback_status = 404

    outcome = await _send_and_verify(backend)

    assert outcome.verified is False
    assert any(m.field == "read_after_send" for m in outcome.mismatches)


@pytest.mark.parametrize(
    "status,expected_error",
    [
        (401, GoogleInvalidTokenError),
        (403, GoogleForbiddenError),
        (429, GoogleRateLimitError),
    ],
)
async def test_gmail_send_failure_before_delivery_still_propagates(
    status: int, expected_error: type[Exception]
) -> None:
    """Negative control: a send the provider refused must still raise."""
    backend = GoogleBackend()
    backend.send_status = status

    with pytest.raises(expected_error):
        await _send_and_verify(backend)
