"""An ambiguous provider success is dispatched-and-unresolved, not a failure.

F4: a write the provider answered with a 2xx whose body cannot be read as the
accepted resource (a malformed body, or no usable id) was classified as a
definitive failure (``PROVIDER_ERROR`` / ``EXECUTION_FAILED``) — the same answer
KAI gives when nothing was written. That is the duplicate-inviting direction: the
provider may already have accepted the write, so a caller reading the failure
would re-issue it and create a second event.

The accepted taxonomy is "request dispatched / outcome unavailable ->
``PROVIDER_UNREACHABLE`` -> unknown external outcome -> no automatic
redispatch". A 2xx whose body cannot be read as the accepted result is exactly
"dispatched / outcome unavailable". These tests pin that classification:

* the unit mapping for both adapters, with a plain ``GoogleApiError`` negative
  control that still maps to ``PROVIDER_ERROR``;
* the calendar governed path (through ``ToolGateway``) for a malformed body and
  for valid JSON lacking an id, plus the retained success and 4xx paths;
* the Gmail service boundary, where the send propagates as
  ``GoogleUnreadableResultError`` and the send still reached the provider.

The Gmail adapter *through the gateway* is not covered here: the governed
runtime slice has no gmail capability (``CAPABILITY_TO_TOOL`` omits it), so a
gmail send is not drivable through the governed path. The adapter's own
classification is pinned by the unit-mapping tests above, and the Gmail write
service boundary is exercised below.

Read paths (``fetch_events_page`` / ``fetch_event`` / ``search_messages`` /
``get_message``) keep raising plain ``GoogleApiError``: a read has no side
effect, so an unreadable read answer is a real ``PROVIDER_ERROR`` and stays
one. That read-side classification is exercised at the end of this module by
driving the real client read methods over ``httpx.MockTransport`` — never by
constructing the exception — so the distinction between the read and write
paths is pinned end to end.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import httpx
import pytest

from kai.calendar import client as calendar_client
from kai.gmail import client as gmail_client
from kai.gmail.service import SendRequest, send_and_verify
from kai.tools.adapters.base import provider_failure
from kai.tools.adapters.calendar import _PROVIDER_CODES as CALENDAR_PROVIDER_CODES
from kai.tools.adapters.gmail import _PROVIDER_CODES as GMAIL_PROVIDER_CODES
from kai.tools.errors import (
    PROVIDER_ERROR,
    PROVIDER_UNREACHABLE,
    is_unresolved_external,
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
    session_scope,
)

CALENDAR_ARGS = {
    "title": "Team Sync",
    "date": "2026-06-24",
    "start_time": "10:00",
    "end_time": "11:00",
    "timezone": "Europe/Berlin",
}
#: A 2xx body that is not JSON at all.
NOT_JSON = b"<html>not json</html>"
#: A well-formed 2xx JSON body that carries no usable resource id.
CALENDAR_JSON_WITHOUT_ID = b'{"summary": "no id here"}'
GMAIL_JSON_WITHOUT_ID = b'{"threadId": "no-id-here"}'


# --- the unit mapping: the subclass wins over the GoogleApiError fallback ------


def test_unreadable_result_maps_to_provider_unreachable_for_both_adapters() -> None:
    """A 2xx whose body cannot be read resolves no outcome, in either adapter."""
    for exc_type, codes in (
        (calendar_client.GoogleUnreadableResultError, CALENDAR_PROVIDER_CODES),
        (gmail_client.GoogleUnreadableResultError, GMAIL_PROVIDER_CODES),
    ):
        failure = provider_failure(exc_type("unreadable", status_code=200), codes=codes)
        assert failure.error_code == PROVIDER_UNREACHABLE
        assert failure.error_code != PROVIDER_ERROR
        assert is_unresolved_external(failure.error_code) is True


def test_a_plain_google_api_error_still_maps_to_provider_error() -> None:
    """Negative control: a read-side answer that failed is still an answer."""
    for exc_type, codes in (
        (calendar_client.GoogleApiError, CALENDAR_PROVIDER_CODES),
        (gmail_client.GoogleApiError, GMAIL_PROVIDER_CODES),
    ):
        failure = provider_failure(exc_type("rejected", status_code=400), codes=codes)
        assert failure.error_code == PROVIDER_ERROR
        assert is_unresolved_external(failure.error_code) is False


# --- the calendar governed path, through the real gateway ---------------------


async def _run_calendar(backend: GoogleBackend):
    """Drive one ``calendar.create_event`` through the real gateway.

    ``allowed_tools`` names the concrete tool the profile permits, so the fixture
    exercises the gateway rather than a profile refusal.
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


@pytest.mark.parametrize("unreadable", [NOT_JSON, CALENDAR_JSON_WITHOUT_ID])
async def test_calendar_create_with_unreadable_success_body_is_unreachable(
    unreadable: bytes,
) -> None:
    """The provider accepted the create; its 2xx cannot be read as the event."""
    backend = GoogleBackend()
    backend.create_event_unreadable = unreadable

    result = await _run_calendar(backend)

    assert result.outcome is ToolOutcome.EXECUTION_FAILED
    assert result.ok is False
    assert result.verified is False
    assert result.error_code == PROVIDER_UNREACHABLE
    assert result.error_code != PROVIDER_ERROR
    # Exactly one create reached the provider: the ambiguity never triggered a
    # retry, and the accepted write is recorded rather than discarded.
    posts = [call for call in backend.calendar_calls if call.method == "POST"]
    assert len(posts) == 1
    assert len(backend.created_events) == 1


async def test_calendar_accepted_and_verified_write_still_succeeds() -> None:
    """Retained: a readable 2xx plus a successful read-back is a real success."""
    backend = GoogleBackend()

    result = await _run_calendar(backend)

    assert result.outcome is ToolOutcome.EXECUTION_SUCCEEDED
    assert result.ok is True
    assert result.verified is True


async def test_calendar_create_refusal_still_maps_to_provider_error() -> None:
    """Retained: a 4xx is the provider saying no, which is a known non-write."""
    backend = GoogleBackend()
    backend.create_event_status = 400

    result = await _run_calendar(backend)

    assert result.outcome is ToolOutcome.EXECUTION_FAILED
    assert result.error_code == PROVIDER_ERROR
    assert result.error_code != PROVIDER_UNREACHABLE
    assert backend.created_events == {}


# --- the Gmail service boundary -----------------------------------------------


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


@pytest.mark.parametrize("unreadable", [NOT_JSON, GMAIL_JSON_WITHOUT_ID])
async def test_gmail_send_with_unreadable_success_body_propagates(
    unreadable: bytes,
) -> None:
    """A send the provider accepted but whose 2xx cannot be read is unresolved."""
    backend = GoogleBackend()
    backend.send_unreadable = unreadable

    with pytest.raises(gmail_client.GoogleUnreadableResultError):
        await _send_and_verify(backend)

    # The send itself reached the provider: this is not a "nothing happened".
    assert backend.send_raw is not None
    assert len(backend.gmail_send_calls) == 1


# --- read paths: an unreadable 2xx is a real PROVIDER_ERROR, never unresolved --
#
# A read has no side effect. If the provider's successful answer cannot be read
# as the requested resource, nothing was changed and there is no ambiguous
# external outcome to protect, so the read clients raise the plain
# ``GoogleApiError`` the write clients do not — and the adapters must map that to
# ``PROVIDER_ERROR``, never to ``PROVIDER_UNREACHABLE``. These tests drive the
# real read functions over ``httpx.MockTransport`` (no database, no network), so
# the classification is pinned through the actual client rather than by
# constructing the exception an assertion expects.

_ReadCall = Callable[[httpx.AsyncClient], Awaitable[object]]

_CALENDAR_READ_START = datetime(2026, 6, 24, 9, 0, tzinfo=UTC)
_CALENDAR_READ_END = datetime(2026, 6, 24, 17, 0, tzinfo=UTC)


async def _calendar_read_event(http: httpx.AsyncClient) -> object:
    return await calendar_client.fetch_event(
        http, access_token="token", event_id="event-1"
    )


async def _calendar_list_events(http: httpx.AsyncClient) -> object:
    return await calendar_client.fetch_events_page(
        http, access_token="token", start=_CALENDAR_READ_START, end=_CALENDAR_READ_END
    )


async def _gmail_read_message(http: httpx.AsyncClient) -> object:
    return await gmail_client.get_message(
        http, access_token="token", message_id="message-1"
    )


async def _gmail_search(http: httpx.AsyncClient) -> object:
    return await gmail_client.search_messages(
        http, access_token="token", query="from:bob@example.com"
    )


async def _read_error(
    call: _ReadCall, base_error: type[Exception], body: bytes
) -> Exception:
    """Run a real client read against a 2xx whose body is ``body``.

    Only Google's network is faked; the client's status handling, JSON parsing,
    and result parsing all run for real, and the raised error is returned so the
    caller can classify it through the production ``provider_failure``.
    """

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(base_error) as excinfo:
            await call(http)
    return excinfo.value


@pytest.mark.parametrize(
    "call,body",
    [
        # Not JSON at all (the parsed-JSON branch).
        (_calendar_read_event, NOT_JSON),
        (_calendar_list_events, NOT_JSON),
        # Valid JSON that carries no usable resource id.
        (_calendar_read_event, CALENDAR_JSON_WITHOUT_ID),
    ],
)
async def test_a_calendar_read_error_is_a_provider_error_not_unresolved(
    call: _ReadCall, body: bytes
) -> None:
    exc = await _read_error(call, calendar_client.GoogleApiError, body)

    assert isinstance(exc, calendar_client.GoogleApiError)
    assert not isinstance(exc, calendar_client.GoogleUnreadableResultError)

    failure = provider_failure(exc, codes=CALENDAR_PROVIDER_CODES)
    assert failure.error_code == PROVIDER_ERROR
    assert failure.error_code != PROVIDER_UNREACHABLE
    assert is_unresolved_external(failure.error_code) is False


@pytest.mark.parametrize(
    "call,body",
    [
        # Not JSON at all (the parsed-JSON branch).
        (_gmail_read_message, NOT_JSON),
        (_gmail_search, NOT_JSON),
        # Valid JSON that carries no usable resource id.
        (_gmail_read_message, GMAIL_JSON_WITHOUT_ID),
    ],
)
async def test_a_gmail_read_error_is_a_provider_error_not_unresolved(
    call: _ReadCall, body: bytes
) -> None:
    exc = await _read_error(call, gmail_client.GoogleApiError, body)

    assert isinstance(exc, gmail_client.GoogleApiError)
    assert not isinstance(exc, gmail_client.GoogleUnreadableResultError)

    failure = provider_failure(exc, codes=GMAIL_PROVIDER_CODES)
    assert failure.error_code == PROVIDER_ERROR
    assert failure.error_code != PROVIDER_UNREACHABLE
    assert is_unresolved_external(failure.error_code) is False
