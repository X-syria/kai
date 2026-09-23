"""Provider adapters: the only network-reaching layer, reusing the existing
integration services.

Each adapter test drives the *gateway* (so all boundaries run) with a real
Google account row in PostgreSQL and a deterministic MockTransport. The
assertions prove the adapter used the existing service/client path (right
endpoints, right bearer token, right payloads) and left JSON-safe data.
"""

from __future__ import annotations

from kai.tools import (
    ApprovalDecision,
    ApprovalManagerStub,
    ToolRegistry,
    default_definitions,
)
from kai.tools.adapters.calendar import CalendarAdapter
from kai.tools.adapters.contacts import ContactsAdapter
from kai.tools.adapters.gmail import GmailAdapter
from kai.tools.result import ToolOutcome
from tool_harness import (
    GoogleBackend,
    close_context,
    contact_payload,
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


def _registry_with(tracked: dict[str, object]) -> ToolRegistry:
    """Registry whose adapters expose ``.calls`` for call-accounting asserts."""
    return ToolRegistry.build(
        default_definitions(
            contacts=tracked["contacts"],  # type: ignore[arg-type]
            calendar=tracked["calendar"],  # type: ignore[arg-type]
            gmail=tracked["gmail"],  # type: ignore[arg-type]
        )
    )


async def test_calendar_read_slots_uses_the_service_path() -> None:
    backend = GoogleBackend()
    backend.calendar_items = [
        event_payload(
            event_id="ev-1",
            summary="Standup",
            start="2026-06-24T10:00:00+02:00",
            end="2026-06-24T10:30:00+02:00",
            timezone="Europe/Berlin",
        )
    ]
    tracked = {
        "contacts": ContactsAdapter(),
        "calendar": CalendarAdapter(),
        "gmail": GmailAdapter(),
    }
    registry = _registry_with(tracked)

    tenant, user = await make_tenant_user("acme@corp.example")
    account = await insert_account(
        tenant_id=tenant.id, user_id=user.id, email="alice@example.com", sub="sub-1"
    )
    task = await make_task(tenant_id=tenant.id, user_id=user.id, allowed_tools=["calendar"])

    gateway, _ = make_gateway(registry=registry)
    async with session_scope() as session:
        context = make_context(
            session=session, task=task, backend=backend, account_id=account.id
        )
        result = await gateway.execute(
            make_intent(
                "calendar.read_slots",
                task=task,
                arguments={"date": "2026-06-24", "timezone": "Europe/Berlin"},
            ),
            context,
        )
        await close_context(context)

    assert result.outcome is ToolOutcome.EXECUTION_SUCCEEDED
    data = result.data
    assert data["date"] == "2026-06-24"
    assert data["day_start"] == "09:00:00"
    assert data["day_end"] == "17:00:00"
    assert data["busy"][0]["start"] == "2026-06-24T10:00:00+02:00"
    assert data["free"] and data["free"][0]["start"] == "2026-06-24T09:00:00+02:00"
    assert "calendar.read_slots" in tracked["calendar"].calls  # type: ignore[attr-defined]
    # The adapter hit the real service's URL with the real stored token.
    assert len(backend.calendar_calls) == 1
    assert backend.calendar_calls[0].headers["Authorization"] == "Bearer test-access-token"
    assert backend.calendar_calls[0].url.host == "www.googleapis.com"


async def test_calendar_get_event_reads_one_event() -> None:
    backend = GoogleBackend()
    backend.read_event_payload = event_payload(
        event_id="ev-9",
        summary="Review",
        start="2026-06-25T15:00:00+02:00",
        end="2026-06-25T16:00:00+02:00",
        timezone="Europe/Berlin",
        attendees=("bob@example.com",),
    )
    tracked = {
        "contacts": ContactsAdapter(),
        "calendar": CalendarAdapter(),
        "gmail": GmailAdapter(),
    }

    tenant, user = await make_tenant_user("acme@corp.example")
    account = await insert_account(
        tenant_id=tenant.id, user_id=user.id, email="alice@example.com", sub="sub-1"
    )
    task = await make_task(tenant_id=tenant.id, user_id=user.id, allowed_tools=["calendar"])

    gateway, _ = make_gateway(registry=_registry_with(tracked))
    async with session_scope() as session:
        context = make_context(
            session=session, task=task, backend=backend, account_id=account.id
        )
        result = await gateway.execute(
            make_intent(
                "calendar.get_event",
                task=task,
                arguments={"event_id": "ev-9"},
            ),
            context,
        )
        await close_context(context)

    assert result.outcome is ToolOutcome.EXECUTION_SUCCEEDED
    assert result.data["event_id"] == "ev-9"
    assert result.data["title"] == "Review"
    assert result.evidence["event_id"] == "ev-9"


async def test_calendar_create_event_is_verified() -> None:
    tracked = {
        "contacts": ContactsAdapter(),
        "calendar": CalendarAdapter(),
        "gmail": GmailAdapter(),
    }
    backend = GoogleBackend()

    tenant, user = await make_tenant_user("acme@corp.example")
    account = await insert_account(
        tenant_id=tenant.id, user_id=user.id, email="alice@example.com", sub="sub-1"
    )
    task = await make_task(tenant_id=tenant.id, user_id=user.id, allowed_tools=["calendar"])

    gateway, _ = make_gateway(
        registry=_registry_with(tracked),
        approval=ApprovalManagerStub(decision=ApprovalDecision.APPROVED),
    )
    async with session_scope() as session:
        context = make_context(
            session=session, task=task, backend=backend, account_id=account.id
        )
        result = await gateway.execute(
            make_intent(
                "calendar.create_event",
                task=task,
                arguments={
                    "title": "Sync with Bob",
                    "date": "2026-06-26",
                    "start_time": "11:00",
                    "end_time": "12:00",
                    "timezone": "Europe/Berlin",
                    "attendees": ["bob@example.com"],
                },
            ),
            context,
        )
        await close_context(context)

    assert result.outcome is ToolOutcome.EXECUTION_SUCCEEDED
    assert result.verified is True
    assert result.evidence["contract"] == "read_after_create"
    assert result.data["title"] == "Sync with Bob"
    assert result.data["attendees"] == ["bob@example.com"]
    assert result.mismatches == ()
    # POST (create) + GET (read-after-create) happened.
    methods = [call.method for call in backend.calendar_calls]
    assert methods == ["POST", "GET"]
    assert "calendar.create_event" in tracked["calendar"].calls  # type: ignore[attr-defined]


async def test_gmail_search_and_read_return_body_as_data() -> None:
    backend = GoogleBackend()
    backend.search_return = [{"id": "msg-1", "threadId": "thread-1"}]
    backend.read_return = message_payload(
        message_id="msg-1",
        subject="Re: project",
        body_text="Let me send the update on Thursday.",
    )
    tracked = {
        "contacts": ContactsAdapter(),
        "calendar": CalendarAdapter(),
        "gmail": GmailAdapter(),
    }

    tenant, user = await make_tenant_user("acme@corp.example")
    account = await insert_account(
        tenant_id=tenant.id, user_id=user.id, email="alice@example.com", sub="sub-1"
    )
    task = await make_task(tenant_id=tenant.id, user_id=user.id, allowed_tools=["gmail"])

    gateway, _ = make_gateway(registry=_registry_with(tracked))
    async with session_scope() as session:
        context = make_context(
            session=session, task=task, backend=backend, account_id=account.id
        )
        search = await gateway.execute(
            make_intent(
                "gmail.search",
                task=task,
                arguments={"query": "from:alice", "max_results": 10},
            ),
            context,
        )
        read = await gateway.execute(
            make_intent(
                "gmail.read",
                task=task,
                arguments={"message_id": "msg-1"},
            ),
            context,
        )
        await close_context(context)

    assert search.outcome is ToolOutcome.EXECUTION_SUCCEEDED
    assert search.data == [{"message_id": "msg-1", "thread_id": "thread-1"}]
    assert search.evidence["total"] == 1
    assert read.outcome is ToolOutcome.EXECUTION_SUCCEEDED
    assert "update on Thursday" in read.data["body"]
    assert read.data["message_id"] == "msg-1"
    assert "gmail.search" in tracked["gmail"].calls  # type: ignore[attr-defined]
    assert "gmail.read" in tracked["gmail"].calls  # type: ignore[attr-defined]


async def test_contacts_match_filters_and_counts() -> None:
    backend = GoogleBackend()
    backend.contacts_connections = [
        contact_payload(
            resource_name="people/1",
            display_name="Alice Example",
            emails=("alice@example.com",),
            phones=("+1 555 0100",),
        ),
        contact_payload(
            resource_name="people/2",
            display_name="Bob Builder",
            emails=("bob@example.com",),
        ),
    ]
    tracked = {
        "contacts": ContactsAdapter(),
        "calendar": CalendarAdapter(),
        "gmail": GmailAdapter(),
    }

    tenant, user = await make_tenant_user("acme@corp.example")
    account = await insert_account(
        tenant_id=tenant.id, user_id=user.id, email="alice@example.com", sub="sub-1"
    )
    task = await make_task(tenant_id=tenant.id, user_id=user.id, allowed_tools=["contacts"])

    gateway, _ = make_gateway(registry=_registry_with(tracked))
    async with session_scope() as session:
        context = make_context(
            session=session, task=task, backend=backend, account_id=account.id
        )
        result = await gateway.execute(
            make_intent("contacts.match", task=task, arguments={"query": "Alice"}),
            context,
        )
        await close_context(context)

    assert result.outcome is ToolOutcome.EXECUTION_SUCCEEDED
    assert result.evidence["total"] == 2
    assert result.evidence["matched"] == 1
    assert result.data[0]["display_name"] == "Alice Example"
    assert "contacts.match" in tracked["contacts"].calls  # type: ignore[attr-defined]