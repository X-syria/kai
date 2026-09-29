"""Shared harness for Tool Gateway tests.

The harness builds real rows in the real PostgreSQL (tenant, user, Google
account, task), a deterministic Google provider behind ``httpx.MockTransport``
(only Google's network is faked), and a fully wired ``ToolGateway``. Service
layers, credential decryption, schema validation, and all boundaries stay real.
"""

from __future__ import annotations

import base64
import uuid
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from kai.db.engine import async_session_factory
from kai.db.models import GoogleAccount, Tenant, User
from kai.google.crypto import encrypt_credentials
from kai.identity.schemas import IdentityQuery
from kai.identity.service import IdentityResolver
from kai.policy import PolicyEngine, PolicyInput
from kai.tasks.models import Task
from kai.tasks.schemas import TaskContractInput
from kai.tasks.service import create_task, transition_task
from kai.tools import (
    ApprovalManager,
    ApprovalManagerStub,
    ContentAuthorizer,
    ContentAuthorizerStub,
    ContractVerificationEngine,
    RiskLevel,
    ToolContext,
    ToolDefinition,
    ToolGateway,
    ToolIntent,
    ToolRegistry,
    VerificationEngine,
    default_registry,
)
from kai.tools import (
    IdentityResolver as IdentityResolverProtocol,
)

ACCESS_TOKEN = "test-access-token"
CONTACTS_URL = "https://people.googleapis.com/v1/people/me/connections"
CALENDAR_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
GMAIL_MESSAGES_URL = "https://www.googleapis.com/gmail/v1/users/me/messages"
GMAIL_SEND_URL = "https://www.googleapis.com/gmail/v1/users/me/messages/send"


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def message_payload(
    *,
    message_id: str,
    thread_id: str = "thread-1",
    sender: str = "Alice Smith <alice@example.com>",
    to: tuple[str, ...] = ("bob@example.com",),
    subject: str = "Hello Bob",
    body_text: str = "This is the plain text body.",
    labels: tuple[str, ...] = ("INBOX", "UNREAD"),
) -> dict:
    headers = [
        {"name": "From", "value": sender},
        {"name": "To", "value": ", ".join(to)},
        {"name": "Subject", "value": subject},
        {"name": "Date", "value": "Tue, 23 Jun 2026 10:00:00 +0200"},
    ]
    parts = [
        {"mimeType": "text/plain", "body": {"data": b64(body_text)}},
        {"mimeType": "text/html", "body": {"data": b64("<html><body>HI</body></html>")}},
    ]
    return {
        "id": message_id,
        "threadId": thread_id,
        "snippet": subject,
        "labelIds": list(labels),
        "payload": {"mimeType": "multipart/mixed", "headers": headers, "parts": parts},
    }


def event_payload(
    *,
    event_id: str,
    summary: str,
    start: str,
    end: str,
    timezone: str,
    attendees: tuple[str, ...] = (),
) -> dict:
    return {
        "id": event_id,
        "summary": summary,
        "start": {"dateTime": start, "timeZone": timezone},
        "end": {"dateTime": end, "timeZone": timezone},
        "attendees": [{"email": email} for email in attendees],
    }


def contact_payload(
    *,
    resource_name: str,
    display_name: str,
    emails: tuple[str, ...] = (),
    phones: tuple[str, ...] = (),
) -> dict:
    return {
        "resourceName": resource_name,
        "names": [{"displayName": display_name}],
        "emailAddresses": [{"value": email} for email in emails],
        "phoneNumbers": [{"value": phone} for phone in phones],
    }


class GoogleBackend:
    """Deterministic stand-in serving Contacts/Calendar/Gmail over one MockTransport."""

    def __init__(self) -> None:
        self.contacts_status = 200
        self.contacts_connections: list[dict] = []
        self.calendar_list_status = 200
        self.calendar_items: list[dict] = []
        self.calendar_read_status = 200
        self.read_event_payload: dict | None = None
        self.create_event_status = 200
        self.created_events: dict[str, dict] = {}
        self.next_event_id = 1

        self.search_status = 200
        self.search_return: list[dict] = []
        self.read_status = 200
        self.read_return: dict | None = None
        self.send_status = 200
        self.send_return: dict | None = None
        self.send_raw: str | None = None
        self.readback_status = 200
        self.sent_messages: dict[str, dict] = {}

        self.contacts_calls: list[httpx.Request] = []
        self.calendar_calls: list[httpx.Request] = []
        self.gmail_search_calls: list[httpx.Request] = []
        self.gmail_read_calls: list[httpx.Request] = []
        self.gmail_send_calls: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = urlparse(str(request.url))

        if url.path == "/v1/people/me/connections":
            self.contacts_calls.append(request)
            if self.contacts_status >= 400:
                return httpx.Response(self.contacts_status, json={"error": "denied"})
            return httpx.Response(
                200,
                json={
                    "connections": self.contacts_connections,
                    "totalPeople": len(self.contacts_connections),
                },
            )

        if url.path == "/calendar/v3/calendars/primary/events":
            if request.method == "POST":
                self.calendar_calls.append(request)
                if self.create_event_status >= 400:
                    return httpx.Response(self.create_event_status, json={"error": "denied"})
                body = request.read().decode("utf-8")
                import json as _json

                payload = _json.loads(body)
                event_id = f"created-event-{self.next_event_id}"
                self.next_event_id += 1
                created = event_payload(
                    event_id=event_id,
                    summary=payload.get("summary", ""),
                    start=payload["start"]["dateTime"],
                    end=payload["end"]["dateTime"],
                    timezone=payload["start"]["timeZone"],
                    attendees=tuple(
                        attendee["email"] for attendee in payload.get("attendees", [])
                    ),
                )
                self.created_events[event_id] = created
                return httpx.Response(200, json=created)
            self.calendar_calls.append(request)
            if self.calendar_list_status >= 400:
                return httpx.Response(self.calendar_list_status, json={"error": "denied"})
            return httpx.Response(200, json={"items": self.calendar_items})

        if url.path.startswith("/calendar/v3/calendars/primary/events/"):
            event_id = url.path.rsplit("/", 1)[-1]
            self.calendar_calls.append(request)
            if self.calendar_read_status >= 400:
                return httpx.Response(self.calendar_read_status, json={"error": "denied"})
            if self.read_event_payload is not None:
                return httpx.Response(200, json=self.read_event_payload)
            if event_id in self.created_events:
                return httpx.Response(200, json=self.created_events[event_id])
            return httpx.Response(404, json={"error": "not found"})

        if url.path == "/gmail/v1/users/me/messages":
            self.gmail_search_calls.append(request)
            if self.search_status >= 400:
                return httpx.Response(self.search_status, json={"error": "denied"})
            return httpx.Response(200, json={"messages": self.search_return})

        if url.path.startswith("/gmail/v1/users/me/messages/send"):
            self.gmail_send_calls.append(request)
            if self.send_status >= 400:
                return httpx.Response(self.send_status, json={"error": "denied"})
            body = request.read().decode("utf-8")
            import json as _json

            raw = _json.loads(body).get("raw", "")
            self.send_raw = _decode_raw(raw)
            sent = self.send_return or message_payload(
                message_id="sent-message-1",
                thread_id="sent-thread-1",
                labels=("SENT",),
                subject="Hello Bob",
                body_text="This is the plain text body.",
            )
            self.sent_messages[sent["id"]] = sent
            return httpx.Response(200, json=sent)

        if url.path.startswith("/gmail/v1/users/me/messages/"):
            message_id = url.path.rsplit("/", 1)[-1]
            self.gmail_read_calls.append(request)
            if self.read_status >= 400:
                return httpx.Response(self.read_status, json={"error": "denied"})
            if self.read_return is not None:
                return httpx.Response(200, json=self.read_return)
            if self.readback_status >= 400:
                return httpx.Response(self.readback_status, json={"error": "not found"})
            if message_id in self.sent_messages:
                return httpx.Response(200, json=self.sent_messages[message_id])
            if message_id in {item["id"] for item in self.search_return}:
                fallback = message_payload(message_id=message_id)
                return httpx.Response(200, json=self.read_return or fallback)
            return httpx.Response(404, json={"error": "not found"})

        return httpx.Response(404, json={"error": str(request.url)})


def _decode_raw(raw: str) -> str:
    padded = raw + "=" * (-len(raw) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")


async def make_tenant_user(email: str, *, tenant_name: str | None = None) -> tuple[Tenant, User]:
    async with async_session_factory() as session:
        tenant = Tenant(name=tenant_name or f"tenant-{uuid.uuid4()}")
        user = User(email=email, password_hash="test", tenant=tenant)
        session.add(tenant)
        session.add(user)
        await session.commit()
        await session.refresh(tenant)
        await session.refresh(user)
        return tenant, user


async def insert_account(
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    email: str,
    sub: str = "google-sub-1",
    scopes: str = "openid email https://www.googleapis.com/auth/calendar.events",
) -> GoogleAccount:
    async with async_session_factory() as session:
        account = GoogleAccount(
            tenant_id=tenant_id,
            user_id=user_id,
            google_sub=sub,
            email=email,
            scopes=scopes,
            credentials_encrypted=encrypt_credentials({"access_token": ACCESS_TOKEN}),
            token_expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        session.add(account)
        await session.commit()
        await session.refresh(account)
        return account


async def make_task(
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    allowed_tools: list[str] | None = None,
    risk_policy: str = "LOW",
    deadline: Any = None,
    max_cost: float = 0.0,
    max_steps: int = 20,
    state: str = "EXECUTING",
) -> Task:
    """Create a task and walk the legal state machine path to ``state``.

    The default ``EXECUTING`` is the only state in which the Policy Engine
    allows tool execution; callers that need an earlier state pass
    ``state="CREATED"`` etc. The walk goes through real ``transition_task``
    calls so a test fixture is always in a machine-legal state.
    """
    contract = TaskContractInput.model_validate(
        {
            "goal": "Assist the user",
            "success_criteria": ["done"],
            "fulfillment_profile": "calendar_meeting",
            "allowed_tools": allowed_tools or [],
            "risk_policy": risk_policy,
            "deadline": deadline,
            "max_cost": max_cost,
            "max_steps": max_steps,
            "verification_method": "automatic",
            "fallback": "ask the user",
        }
    )
    async with async_session_factory() as session:
        task = await create_task(
            session, tenant_id=tenant_id, user_id=user_id, contract=contract
        )
        path = _transition_path(state)
        for target in path[1:]:
            task = await transition_task(
                session,
                task_id=task.id,
                tenant_id=tenant_id,
                user_id=user_id,
                target=target,
            )
        return task


_HAPPY_STATES = frozenset(
    {
        "UNDERSTANDING",
        "PLANNING",
        "POLICY_CHECK",
        "WAITING_APPROVAL",
        "EXECUTING",
        "WAITING_EXTERNAL",
        "VERIFYING",
        "COMPLETED",
    }
)


def _transition_path(target: str) -> list[str]:
    """Shortest machine-legal path from CREATED to ``target``.

    BFS prefers the happy path and only rewrites into failure/retry states when
    the requested state itself requires it, so default fixtures never tarnish
    a task through ``FAILED -> RETRY``.
    """
    from kai.tasks.state import CREATED, TRANSITIONS

    if target == CREATED:
        return [CREATED]

    def _bfs(edges: dict[str, frozenset[str]]) -> list[str] | None:
        queue: deque[str] = deque([CREATED])
        parents: dict[str, str | None] = {CREATED: None}
        while queue:
            current = queue.popleft()
            if current == target:
                path: list[str] = []
                while current is not None:
                    path.append(current)
                    current = parents[current]
                return list(reversed(path))
            for nxt in sorted(edges[current]):
                if nxt not in parents:
                    parents[nxt] = current
                    queue.append(nxt)
        return None

    happy_edges = {
        state: frozenset(nxt for nxt in nexts if nxt in _HAPPY_STATES)
        for state, nexts in TRANSITIONS.items()
    }
    path = _bfs(happy_edges)
    if path is not None:
        return path
    full = _bfs(dict(TRANSITIONS))
    if full is not None:
        return full
    raise ValueError(f"no legal state-machine path from CREATED to {target!r}")


def make_intent(
    tool_name: str,
    *,
    task: Task,
    task_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    arguments: dict[str, Any] | None = None,
    request_id: str = "req-1",
) -> ToolIntent:
    return ToolIntent(
        tool_name=tool_name,
        arguments=arguments or {},
        task_id=task_id if task_id is not None else task.id,
        tenant_id=tenant_id if tenant_id is not None else task.tenant_id,
        user_id=user_id if user_id is not None else task.user_id,
        request_id=request_id,
    )


def make_context(
    *,
    session: Any,
    task: Task,
    backend: GoogleBackend,
    account_id: uuid.UUID | None,
    task_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    identity: IdentityQuery | None = None,
) -> ToolContext:
    return ToolContext(
        session=session,
        task_id=task_id if task_id is not None else task.id,
        tenant_id=tenant_id if tenant_id is not None else task.tenant_id,
        user_id=user_id if user_id is not None else task.user_id,
        account_id=account_id,
        transport=httpx.AsyncClient(
            transport=httpx.MockTransport(backend.handler),
            base_url="http://google.api.test",
            timeout=15.0,
        ),
        identity=identity,
    )


def make_gateway(
    *,
    registry: ToolRegistry | None = None,
    policy: PolicyEngine | None = None,
    content: ContentAuthorizer | None = None,
    approval: ApprovalManager | None = None,
    verification: VerificationEngine | None = None,
    identity: IdentityResolverProtocol | None = None,
) -> tuple[ToolGateway, dict[str, Any]]:
    """Build a fully wired gateway; return (gateway, {boundary objects})."""
    reg = registry if registry is not None else default_registry()
    pol = policy if policy is not None else PolicyEngine()
    cont = content if content is not None else ContentAuthorizerStub()
    appr = approval if approval is not None else ApprovalManagerStub()
    ver = verification if verification is not None else ContractVerificationEngine()
    ident = identity if identity is not None else IdentityResolver()
    gateway = ToolGateway(
        registry=reg,
        policy_engine=pol,
        content_authorizer=cont,
        approval_manager=appr,
        verification_engine=ver,
        identity_resolver=ident,
    )
    return gateway, {
        "policy": pol,
        "content": cont,
        "approval": appr,
        "verification": ver,
        "identity": ident,
    }


def make_policy_input(
    *,
    task: Task,
    tool: ToolDefinition,
    intent: ToolIntent,
    risk_level: RiskLevel | None = None,
    tenant_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    **kwargs: Any,
) -> PolicyInput:
    """Trusted PolicyInput for engine-level tests, derived from task/tool."""
    return PolicyInput(
        tenant_id=tenant_id if tenant_id is not None else task.tenant_id,
        user_id=user_id if user_id is not None else task.user_id,
        task_id=task.id,
        task=task,
        tool=tool,
        intent=intent,
        risk_level=risk_level if risk_level is not None else tool.risk_level,
        **kwargs,
    )


async def close_context(context: ToolContext) -> None:
    if context.transport is not None:
        await context.transport.aclose()


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """One real DB session for the duration of a test block."""
    async with async_session_factory() as session:
        yield session


__all__ = [
    "ACCESS_TOKEN",
    "GoogleBackend",
    "contact_payload",
    "event_payload",
    "insert_account",
    "make_context",
    "make_gateway",
    "make_intent",
    "make_policy_input",
    "make_task",
    "make_tenant_user",
    "message_payload",
    "session_scope",
]