"""Security properties of the governed external-write path.

These properties used to be asserted against ``POST /calendar/events`` and
``POST /gmail/send``. Both endpoints reached the provider write services
directly, so they were a second, ungoverned route to a side effect the runtime
already performs through ``ToolGateway`` — no policy, no approval, no identity
resolution, no durable evidence, no audit. They now answer ``410 Gone``.

Deleting them without re-homing their assertions would have quietly removed real
coverage, so every property that was only proven at those endpoints is proven
here, on the one path that can still perform the write.

The provider-facing behaviour under test:

* a provider error is never reported as success;
* a read-after-write mismatch is never reported as success;
* an unreadable readback is not verification;
* malformed arguments are refused before any provider request;
* a credential never appears in the result.
"""

from __future__ import annotations

import inspect

import httpx
import pytest

from kai.tools import ApprovalManagerStub
from kai.tools.adapters.base import provider_failure
from kai.tools.boundaries import ApprovalDecision, ApprovalDecisionRecord
from kai.tools.errors import (
    APPROVAL_STALE,
    PROVIDER_ERROR,
    PROVIDER_NOT_CONTACTED,
    PROVIDER_SERVER_ERROR,
    PROVIDER_UNREACHABLE,
    UNRESOLVED_EXTERNAL_CODES,
    is_unresolved_external,
)
from kai.tools.result import ToolOutcome
from tool_harness import (
    GoogleBackend,
    close_context,
    event_payload,
    insert_account,
    make_context,
    make_gateway,
    make_intent,
    make_task,
    make_tenant_user,
    message_payload,
    session_scope,
)

# --- what can be established about a request, not which exception was raised ---


@pytest.mark.parametrize(
    "exception",
    [
        httpx.ReadTimeout("timed out"),
        httpx.ReadError("connection reset by peer"),
        httpx.RemoteProtocolError("server disconnected"),
        httpx.WriteError("broken pipe"),
        httpx.TransportError("something went wrong on the wire"),
    ],
)
def test_a_dispatched_request_with_no_answer_is_unresolved(
    exception: Exception,
) -> None:
    """The request went out and no complete response came back.

    Every one of these raised the same code as a genuine provider rejection
    before this was separated. On a write that is the dangerous direction: the
    run records "the provider refused", which reads as proof the write did not
    happen, and a timeout that actually succeeded would be reported as a failure
    that never occurred.
    """
    failure = provider_failure(exception, codes={})
    assert failure.error_code == PROVIDER_UNREACHABLE
    assert failure.error_code != PROVIDER_ERROR
    assert is_unresolved_external(failure.error_code) is True


@pytest.mark.parametrize(
    "exception",
    [
        httpx.ConnectTimeout("connect timed out"),
        httpx.ConnectError("connection refused"),
        httpx.PoolTimeout("pool timed out"),
        httpx.ProxyError("proxy refused"),
        httpx.UnsupportedProtocol("no protocol"),
    ],
)
def test_a_request_that_never_left_the_process_is_known_not_to_have_happened(
    exception: Exception,
) -> None:
    """No connection was established, so nothing is unknown.

    This is the boundary the single "transport is unresolved" rule got wrong in
    the other direction. A refused connection is *positive* knowledge that the
    provider never received the request, and calling it unresolved would assert
    doubt about an operation that provably did not happen — which then blocks a
    human for no reason and hides a real misconfiguration behind a scary
    ``REQUIRES_ATTENTION``.
    """
    failure = provider_failure(exception, codes={})
    assert failure.error_code == PROVIDER_NOT_CONTACTED
    assert is_unresolved_external(failure.error_code) is False


def test_the_never_contacted_codes_are_not_confused_with_a_refusal() -> None:
    """A known non-attempt is neither unresolved nor a provider rejection.

    All three codes mean something different, and collapsing any pair of them
    would either invent doubt or deny a refusal that happened.
    """
    codes = {
        provider_failure(exc, codes={}).error_code
        for exc in (httpx.ConnectError("refused"), httpx.ReadTimeout("timed out"))
    }
    assert codes == {PROVIDER_NOT_CONTACTED, PROVIDER_UNREACHABLE}
    assert len(codes) == 2


def _google_error(status: int) -> Exception:
    """A ``GoogleApiError`` carrying its real status, as the clients now attach."""
    from kai.calendar.client import GoogleApiError

    return GoogleApiError(f"google calendar api request failed ({status})", status_code=status)


def test_a_4xx_from_the_provider_is_a_known_refusal() -> None:
    """The provider answered and rejected it, which is a conclusive failure.

    For a write this is the strongest negative evidence available: the provider
    read the request and declined it. Treating it as unresolved would be the same
    error as a lost answer, in the direction that invents doubt.
    """
    from kai.calendar.client import GoogleNotFoundError

    failure = provider_failure(
        GoogleNotFoundError("google calendar event was not found", status_code=404),
        codes={GoogleNotFoundError: "CALENDAR_EVENT_NOT_FOUND"},
    )
    assert failure.error_code == "CALENDAR_EVENT_NOT_FOUND"
    assert is_unresolved_external(failure.error_code) is False


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_a_5xx_does_not_say_whether_the_write_was_applied(status: int) -> None:
    """A server error is an answer, but not an answer about *this* write.

    The request was accepted and then something failed server-side, which is
    exactly the window in which an upstream may already have committed the
    operation. Reporting it as a clean failure asserts that nothing happened;
    reporting it as success asserts the opposite. Neither is knowable here.
    """
    failure = provider_failure(_google_error(status), codes={})
    assert failure.error_code == PROVIDER_SERVER_ERROR
    assert failure.error_code != PROVIDER_ERROR
    assert is_unresolved_external(failure.error_code) is True


def test_the_server_error_is_read_from_the_status_not_the_message() -> None:
    """The status is a field; parsing it back out of prose would be a guess.

    The message text is provider-controlled formatting, and every code that
    looked for a number in it would break the day Google reworded a string.
    """
    without_status = provider_failure(
        _BareGoogleError("google calendar api request failed (503)"), codes={}
    )
    assert without_status.error_code == PROVIDER_ERROR, (
        "a message mentioning 503 is not a 503; with no status attached there "
        "is nothing to read but the answer's own class"
    )


class _BareGoogleError(Exception):
    """Stands in for a typed provider error that carries no status at all."""


def test_a_provider_api_error_is_still_an_answer_not_a_lost_one() -> None:
    """``GoogleApiError`` means the provider replied; that reply is a result."""

    class _GoogleApiError(Exception):
        pass

    failure = provider_failure(
        _GoogleApiError("403 forbidden"),
        codes={_GoogleApiError: "PERMISSION_DENIED"},
    )
    assert failure.error_code == "PERMISSION_DENIED"
    assert is_unresolved_external(failure.error_code) is False


def test_only_two_codes_name_an_unestablished_outcome() -> None:
    """The set is closed and small, so each member is a deliberate decision.

    ``APPROVAL_STALE`` is deliberately *not* a member. It is an authorization
    refusal: the gateway consumes an approval immediately before dispatch, so the
    refusal stopped this invocation before the provider was reached. Its presence
    in this set is what previously let a policy refusal be reported as an
    undecidable side effect.
    """
    assert frozenset({PROVIDER_UNREACHABLE, PROVIDER_SERVER_ERROR}) == (
        UNRESOLVED_EXTERNAL_CODES
    )
    assert is_unresolved_external(None) is False
    assert is_unresolved_external(PROVIDER_ERROR) is False
    assert is_unresolved_external(PROVIDER_NOT_CONTACTED) is False
    assert is_unresolved_external(APPROVAL_STALE) is False
    assert is_unresolved_external("") is False


CALENDAR_ARGS = {
    "title": "Team Sync",
    "date": "2026-06-24",
    "start_time": "10:00",
    "end_time": "11:00",
    "timezone": "Europe/Berlin",
}
GMAIL_ARGS = {
    "to": "bob@example.com",
    "subject": "Meeting tomorrow",
    "body": "Hi Bob, let's sync tomorrow at 10.",
}


async def _run(
    backend: GoogleBackend,
    tool_name: str,
    arguments: dict,
    *,
    approval=None,
    tenant_id=None,
    user_id=None,
    owner=None,
):
    """Execute one tool through the gateway and return its result."""
    tenant, user = owner or await make_tenant_user("acme@corp.example")
    account = await insert_account(
        tenant_id=tenant.id, user_id=user.id, email="alice@example.com", sub="sub-1"
    )
    task = await make_task(tenant_id=tenant.id, user_id=user.id)
    gateway, _ = make_gateway() if approval is None else make_gateway(approval=approval)
    async with session_scope() as session:
        context = make_context(
            session=session,
            task=task,
            backend=backend,
            account_id=account.id,
            tenant_id=tenant_id,
            user_id=user_id,
        )
        result = await gateway.execute(
            make_intent(tool_name, task=task, arguments=arguments), context
        )
        await close_context(context)
    return result


# --- provider errors are never success --------------------------------------

@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
async def test_calendar_create_provider_error_is_never_success(status: int) -> None:
    backend = GoogleBackend()
    backend.create_event_status = status

    result = await _run(backend, "calendar.create_event", CALENDAR_ARGS)

    assert result.ok is False
    assert result.verified is False
    assert result.outcome is ToolOutcome.EXECUTION_FAILED
    assert backend.calendar_calls, "the provider was never contacted"


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
async def test_gmail_send_provider_error_is_never_success(status: int) -> None:
    backend = GoogleBackend()
    backend.send_status = status

    result = await _run(backend, "gmail.send", GMAIL_ARGS)

    assert result.ok is False
    assert result.verified is False
    assert result.outcome is ToolOutcome.EXECUTION_FAILED
    assert backend.gmail_send_calls, "the provider was never contacted"


# --- read-after-write verification -------------------------------------------

async def test_calendar_create_readback_mismatch_is_not_success() -> None:
    """The provider echoes a different summary; that is not a created meeting."""
    backend = GoogleBackend()
    backend.read_event_payload = event_payload(
        event_id="created-event-1",
        summary="Something Else Entirely",
        start="2026-06-24T10:00:00+02:00",
        end="2026-06-24T11:00:00+02:00",
        timezone="Europe/Berlin",
    )

    result = await _run(backend, "calendar.create_event", CALENDAR_ARGS)

    assert result.ok is False
    assert result.verified is False
    assert result.outcome is ToolOutcome.REQUIRES_ATTENTION
    assert result.mismatches


async def test_calendar_create_with_unreadable_readback_is_not_verified() -> None:
    """Write accepted, readback 404: existence is unproven, so not verified."""
    backend = GoogleBackend()
    backend.calendar_read_status = 404

    result = await _run(backend, "calendar.create_event", CALENDAR_ARGS)

    assert result.ok is False
    assert result.verified is False
    assert result.outcome is ToolOutcome.REQUIRES_ATTENTION


async def test_gmail_send_with_unreadable_readback_is_not_verified() -> None:
    """A send whose readback cannot be read is never reported as sent-and-seen.

    This is the case where the message may genuinely have been delivered. It is
    reported as unverified rather than as a failure to avoid a blind retry that
    would deliver twice; see the unknown-outcome work.
    """
    backend = GoogleBackend()
    backend.readback_status = 404

    result = await _run(backend, "gmail.send", GMAIL_ARGS)

    assert result.ok is False
    assert result.verified is False
    assert result.outcome is ToolOutcome.REQUIRES_ATTENTION


async def test_gmail_send_without_sent_label_is_not_verified() -> None:
    backend = GoogleBackend()
    backend.send_return = message_payload(
        message_id="sent-message-1",
        thread_id="sent-thread-1",
        labels=("DRAFT",),
        subject="Meeting tomorrow",
        body_text="Hi Bob, let's sync tomorrow at 10.",
    )

    result = await _run(backend, "gmail.send", GMAIL_ARGS)

    assert result.ok is False
    assert result.verified is False


# --- malformed arguments never reach the provider ---------------------------

@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param({**CALENDAR_ARGS, "date": "not-a-date"}, id="bad-date"),
        pytest.param({**CALENDAR_ARGS, "start_time": "99:99"}, id="bad-start-time"),
        pytest.param({**CALENDAR_ARGS, "end_time": "09:00"}, id="end-before-start"),
        pytest.param({**CALENDAR_ARGS, "timezone": "Mars/Olympus"}, id="bad-timezone"),
        pytest.param(
            {**CALENDAR_ARGS, "attendees": ["not-an-address"]}, id="bad-attendee"
        ),
        pytest.param({**CALENDAR_ARGS, "unexpected": "field"}, id="extra-field"),
    ],
)
async def test_malformed_calendar_arguments_never_reach_the_provider(
    arguments: dict,
) -> None:
    backend = GoogleBackend()

    result = await _run(backend, "calendar.create_event", arguments)

    assert result.outcome is ToolOutcome.REJECTED
    assert result.error_code == "VALIDATION_ERROR"
    assert backend.calendar_calls == []


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param({**GMAIL_ARGS, "to": "not-an-email"}, id="bad-recipient"),
        pytest.param({**GMAIL_ARGS, "subject": ""}, id="empty-subject"),
        pytest.param({**GMAIL_ARGS, "body": ""}, id="empty-body"),
        pytest.param({**GMAIL_ARGS, "sender": "attacker@evil.example"}, id="sender-field"),
    ],
)
async def test_malformed_gmail_arguments_never_reach_the_provider(
    arguments: dict,
) -> None:
    """``sender`` is structurally impossible: the account owns the from-address."""
    backend = GoogleBackend()

    result = await _run(backend, "gmail.send", arguments)

    assert result.outcome is ToolOutcome.REJECTED
    assert backend.gmail_send_calls == []


# --- credential containment --------------------------------------------------

async def test_no_credential_reaches_the_result_of_a_write() -> None:
    backend = GoogleBackend()
    backend.send_return = message_payload(
        message_id="sent-message-1",
        thread_id="sent-thread-1",
        labels=("SENT",),
        subject="Meeting tomorrow",
        body_text="Hi Bob, let's sync tomorrow at 10.",
    )

    result = await _run(backend, "gmail.send", GMAIL_ARGS)

    rendered = f"{result.data!r} {result.evidence!r} {result.message!r}"
    assert "test-access-token" not in rendered
    assert "test-refresh-token" not in rendered
    assert "credentials_encrypted" not in rendered


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("calendar.create_event", CALENDAR_ARGS),
        ("gmail.send", GMAIL_ARGS),
    ],
)
@pytest.mark.parametrize("dimension", ["tenant", "user"])
async def test_a_context_that_does_not_own_the_task_never_reaches_the_provider(
    tool_name: str, arguments: dict, dimension: str
) -> None:
    """The task is loaded by ``(task_id, tenant_id, user_id)``, so a caller who is
    not the owner cannot even see it.

    This is the property the retired endpoints enforced by hand when they accepted
    an ``account_id`` query parameter and resolved it against
    ``(current.tenant_id, current.user_id)``. On the governed path there is no
    account id to supply and no caller-supplied identifier at all: the task lookup
    is itself the scope check, and a mismatch fails before the provider is
    contacted. Policy's ``rule_binding`` and the gateway's intent/context identity
    check sit behind this as further layers.
    """
    backend = GoogleBackend()
    tenant, user = await make_tenant_user("acme@corp.example")
    other, _ = await make_tenant_user("intruder@evil.example")

    if dimension == "tenant":
        context_tenant, context_user = other.id, user.id
    else:
        context_tenant, context_user = tenant.id, other.id

    result = await _run(
        backend,
        tool_name,
        arguments,
        tenant_id=context_tenant,
        user_id=context_user,
        owner=(tenant, user),
    )

    assert result.ok is False
    assert result.verified is False
    assert result.error_code == "TASK_UNRESOLVED"
    assert backend.calendar_calls == []
    assert backend.gmail_send_calls == []


def test_the_runtime_resolves_the_google_account_from_the_task_alone() -> None:
    """No caller-supplied ``account_id`` can select whose Google account is used.

    ``AgentRuntime._resolve_account`` filters on both ``task.tenant_id`` and
    ``task.user_id``, and accepts no account id. A caller therefore cannot aim a
    write at another tenant's connected account, which is the property the
    retired query-string ``account_id`` had to enforce by hand.
    """
    from kai.runtime.service import AgentRuntime

    source = inspect.getsource(AgentRuntime._resolve_account)
    assert "GoogleAccount.tenant_id == task.tenant_id" in source
    assert "GoogleAccount.user_id == task.user_id" in source
    assert "account_id" not in source


async def test_gmail_send_reports_the_provider_message_id_as_evidence() -> None:
    """A verified send must leave the provider's own identifier behind.

    Without the message id the outcome cannot be reconstructed or reconciled
    after the fact, so its presence is part of what "verified" means.
    """
    backend = GoogleBackend()
    backend.send_return = message_payload(
        message_id="sent-message-1",
        thread_id="sent-thread-1",
        labels=("SENT",),
        to=("bob@example.com",),
        subject="Meeting tomorrow",
        body_text="Hi Bob, let's sync tomorrow at 10.",
    )

    result = await _run(backend, "gmail.send", GMAIL_ARGS)

    assert result.ok is True
    assert result.verified is True
    assert result.outcome is ToolOutcome.EXECUTION_SUCCEEDED
    assert "sent-message-1" in f"{result.data!r} {result.evidence!r}"


@pytest.mark.parametrize(
    "subject",
    [
        pytest.param("Subject\r\nBcc: attacker@evil.example", id="crlf"),
        pytest.param("Subject\nBcc: attacker@evil.example", id="bare-newline"),
    ],
)
async def test_gmail_send_subject_with_a_line_break_is_refused(subject: str) -> None:
    """Header injection: a newline in a header value must never reach the API."""
    backend = GoogleBackend()

    result = await _run(backend, "gmail.send", {**GMAIL_ARGS, "subject": subject})

    assert result.ok is False
    assert result.outcome is ToolOutcome.REJECTED
    assert backend.gmail_send_calls == []


async def test_gmail_send_readback_recipient_mismatch_is_not_success() -> None:
    """The provider's own readback named a different recipient than we sent to."""
    backend = GoogleBackend()
    backend.read_return = message_payload(
        message_id="sent-message-1",
        thread_id="sent-thread-1",
        labels=("SENT",),
        to=("someone.else@example.com",),
        subject="Meeting tomorrow",
        body_text="Hi Bob, let's sync tomorrow at 10.",
    )

    result = await _run(backend, "gmail.send", GMAIL_ARGS)

    assert result.ok is False
    assert result.verified is False
    assert result.outcome is ToolOutcome.REQUIRES_ATTENTION


async def test_an_approved_calendar_write_still_reports_verified_success() -> None:
    """The positive control: the governed path can still perform the write.

    Without this, every test above would also pass if the governed path had been
    broken into permanent refusal.
    """
    backend = GoogleBackend()

    result = await _run(
        backend,
        "calendar.create_event",
        CALENDAR_ARGS,
        approval=ApprovalManagerStub(),
    )

    assert result.ok is True
    assert result.verified is True
    assert result.outcome is ToolOutcome.EXECUTION_SUCCEEDED
    assert backend.calendar_calls


# --- a spent approval is machine-readable, and never reaches a provider ---------


class _SpentApprovalManager:
    """An approval manager whose single use is already gone.

    This is the state a worker leaves behind when it dies between consuming the
    approval and recording the receipt: the write may or may not have reached
    the provider, and nothing durable can say which.
    """

    async def check(self, **kwargs) -> ApprovalDecisionRecord:
        return ApprovalDecisionRecord(
            decision=ApprovalDecision.STALE,
            reason="the single use for this exact intent is already consumed",
            decided_by="test",
        )

    async def consume(self, **kwargs) -> None:
        raise AssertionError("a STALE approval must never be consumed")


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("calendar.create_event", CALENDAR_ARGS),
        ("gmail.send", GMAIL_ARGS),
    ],
)
async def test_a_spent_approval_is_refused_with_a_stable_code(
    tool_name: str, arguments: dict
) -> None:
    """The runtime keys off ``APPROVAL_STALE``, never off message text.

    The outcome stays ``DENIED`` so policy semantics and every existing caller
    are unchanged; only a machine-readable reason is added, and the provider is
    never contacted.
    """
    backend = GoogleBackend()

    result = await _run(backend, tool_name, arguments, approval=_SpentApprovalManager())

    assert result.outcome is ToolOutcome.DENIED
    assert result.ok is False
    assert result.verified is False
    assert result.error_code == APPROVAL_STALE
    assert result.error_code == "APPROVAL_STALE"
    assert not backend.created_events
    assert not backend.sent_messages


@pytest.mark.parametrize(
    "decision", [ApprovalDecision.DENIED, ApprovalDecision.EXPIRED]
)
async def test_other_refusals_are_not_labelled_as_a_spent_approval(
    decision: ApprovalDecision,
) -> None:
    """Only STALE means "the use is gone"; DENIED and EXPIRED mean "not now".

    Labelling them the same would park ordinary policy refusals for a human and
    destroy the distinction between a decision and an undecidable outcome.
    """

    class _Refuses:
        async def check(self, **kwargs) -> ApprovalDecisionRecord:
            return ApprovalDecisionRecord(
                decision=decision, reason="refused", decided_by="test"
            )

        async def consume(self, **kwargs) -> None:
            raise AssertionError("a refused approval must never be consumed")

    backend = GoogleBackend()

    result = await _run(
        backend, "calendar.create_event", CALENDAR_ARGS, approval=_Refuses()
    )

    assert result.outcome is ToolOutcome.DENIED
    assert result.error_code is None, decision
    assert not backend.created_events