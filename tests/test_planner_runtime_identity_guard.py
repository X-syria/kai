"""Planner API x Runtime identity guard — real PostgreSQL, deterministic doubles.

The planner endpoint (``POST /tasks/{task_id}/plan``) is a direct/operational
surface, not the governed runtime path. A task submitted through the Agent
Runtime carries a ``RuntimeExecution`` row whose ``identities`` list is the
*durable* output of the runtime's identity-resolution stage. Planning such a
task from the API while that list is still empty would persist a plan, and move
the task to PLANNING, before the identities exist — so the runtime could then
resume past its identity stage.

The guard added in ``src/kai/api/planner.py`` refuses exactly that case with
409, before any ``PlannerService`` call (no model call, no ``TaskPlan`` row).
It runs only *after* ``get_task`` has established that the requesting user may
access the task (tenant **and** user), so an unowned or unknown task keeps the
ordinary opaque 404 and its Runtime identity state is never observable.
The three persisted states it distinguishes:

* W1 — ``scenario IS NULL``, empty identities (nothing resolved yet).
* W2 — a scenario is persisted, empty identities (resume at identity stage).
* W3 — identities already persisted, task not yet PLANNING (must *not* reject).

Defense in depth — what actually exists in THIS checkout (verified, not
assumed): there is **no** separate "persisted-identity validation" in the
runtime service that rejects a PLANNING task with empty identities, and
``tests/test_runtime_identity_validation.py`` has never existed here. What the
runtime does now (``TASK-RUNTIME-IDENTITY-LIFECYCLE-05``) is refuse an advanced
state whose durable prerequisites are missing: identity resolution runs only in
``CREATED``/``UNDERSTANDING`` (W1/W2), and a ``PLANNING``-or-later task with no
identities (or no understanding) is failed or parked instead of re-resolved.
``tests/test_runtime_identity_lifecycle.py`` owns that behaviour;
``test_runtime_probe_*`` below records the change. This file does not invent a
status validator; the API guard remains scoped to the direct planning endpoint.

Everything here uses the real test database and the real lifecycle. Google
traffic is an ``httpx.MockTransport`` (``tool_harness.GoogleBackend``) and the
model is a deterministic stub. No provider is contacted.

Class-B compatibility proof (unchanged, existing test):
``tests/test_planner.py::test_plan_flow_creates_plan_and_moves_task_to_planning``
creates a task through the task API (no ``RuntimeExecution`` row), so the guard
does not apply and planning still succeeds.
"""

from __future__ import annotations

from datetime import date, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select

from kai.auth.security import hash_password
from kai.db.engine import async_session_factory
from kai.db.models import Tenant, User
from kai.google.deps import get_google_http_client
from kai.main import app
from kai.memory.models import MemoryType
from kai.memory.service import create_memory
from kai.planner.deps import get_planner_model
from kai.planner.models import TaskPlan
from kai.planner.service import PlannerService
from kai.runtime.models import RuntimeExecution
from kai.runtime.schemas import UnderstandingOutput
from kai.runtime.understanding import UNDERSTANDING_SYSTEM_PROMPT
from kai.tasks.models import Task
from kai.tasks.service import transition_task
from kai.tasks.state import PLANNING, UNDERSTANDING
from tool_harness import GoogleBackend, contact_payload, insert_account

API_PREFIX = "/api/v1"

GOAL = "Schedule a team meeting with Ahmed"
CRITERIA = [
    "calendar event created",
    "attendee is the requested contact",
    "event fits the requested time window",
]
ATTENDEE_NAME = "Ahmed"
ATTENDEE_EMAIL = "ahmed@example.com"
TIMEZONE = "Asia/Riyadh"
EARLIEST = "16:00"

VALID_IDENTITY = {
    "name": ATTENDEE_NAME,
    "email": ATTENDEE_EMAIL,
    "status": "CONTACT_MATCH",
    "confidence": 0.5,
}

#: The seven durable fields the guard must leave byte-identical.
_SNAPSHOT_FIELDS = (
    "scenario",
    "identities",
    "evidence",
    "cursor",
    "steps_used",
    "plan_version",
    "plan_hash",
)


def _next_monday() -> date:
    """The first Monday strictly after today (always a future weekday)."""
    today = date.today()
    days_until_monday = (0 - today.weekday()) % 7 or 7
    return today + timedelta(days=days_until_monday)


def _valid_understanding() -> dict:
    start = _next_monday()
    return {
        "intent": "schedule_meeting",
        "guests": [ATTENDEE_NAME],
        "window": {
            "start_date": start.isoformat(),
            "end_date": (start + timedelta(days=3)).isoformat(),
            "earliest_time": EARLIEST,
            "title": "Meeting with " + ATTENDEE_NAME,
        },
    }


def _valid_plan() -> dict:
    return {
        "goal": GOAL,
        "success_criteria": list(CRITERIA),
        "steps": [
            {
                "step_id": "s1",
                "order": 1,
                "action": "resolve_contact",
                "purpose": "find the attendee",
                "inputs": [ATTENDEE_NAME],
                "expected_output": "contact identity",
                "verification_hint": "exact email match",
            },
            {
                "step_id": "s2",
                "order": 2,
                "action": "read_calendar",
                "purpose": "check availability",
                "inputs": ["window"],
                "expected_output": "free slots",
                "verification_hint": "slots returned",
            },
            {
                "step_id": "s3",
                "order": 3,
                "action": "create_calendar_event",
                "purpose": "schedule the meeting",
                "inputs": ["slot", "attendee"],
                "expected_output": "created event",
                "verification_hint": "event read back",
            },
            {
                "step_id": "s4",
                "order": 4,
                "action": "verify_calendar_event",
                "purpose": "confirm the event exists",
                "inputs": ["event_id"],
                "expected_output": "confirmed event",
                "verification_hint": "event id on record",
            },
        ],
        "assumptions": ["attendee reserves the event"],
        "unresolved_questions": [],
    }


class GuardStubModel:
    """Answers both stages deterministically and records every call."""

    model_id = "stub-identity-guard-v1"

    def __init__(self) -> None:
        self.calls: list[list[dict]] = []

    async def generate_plan(self, *, messages: list[dict]) -> dict:
        self.calls.append(messages)
        if messages[0]["content"] == UNDERSTANDING_SYSTEM_PROMPT:
            return _valid_understanding()
        return _valid_plan()


@pytest.fixture
def backend() -> GoogleBackend:
    backend = GoogleBackend()
    backend.contacts_connections = [
        contact_payload(
            resource_name="people/attendee-1",
            display_name=ATTENDEE_NAME,
            emails=(ATTENDEE_EMAIL,),
        )
    ]
    return backend


@pytest.fixture
def stub() -> GuardStubModel:
    return GuardStubModel()


@pytest.fixture(autouse=True)
def overrides(backend: GoogleBackend, stub: GuardStubModel):
    async def _client():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(backend.handler),
            base_url="http://google.api.test",
            timeout=15.0,
        ) as client:
            yield client

    app.dependency_overrides[get_planner_model] = lambda: stub
    app.dependency_overrides[get_google_http_client] = _client
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_planner_model, None)
        app.dependency_overrides.pop(get_google_http_client, None)


# --- helpers ---------------------------------------------------------------


async def _register(client: httpx.AsyncClient, email: str) -> str:
    resp = await client.post(
        f"{API_PREFIX}/auth/register",
        json={"email": email, "password": "password123"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["access_token"]


async def _user_scope(email: str) -> tuple[UUID, UUID]:
    async with async_session_factory() as session:
        user = (
            await session.execute(select(User).where(User.email == email))
        ).scalar_one()
        return user.tenant_id, user.id


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _submit(client: httpx.AsyncClient, token: str) -> str:
    resp = await client.post(
        f"{API_PREFIX}/runtime/tasks",
        json={"goal": GOAL, "timezone": TIMEZONE},
        headers=_auth(token),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["task_id"]


async def _trigger(client: httpx.AsyncClient, token: str, task_id: str) -> dict:
    resp = await client.post(
        f"{API_PREFIX}/runtime/tasks/{task_id}/trigger",
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _walk(
    task_id: str, tenant_id: UUID, user_id: UUID, *targets: str
) -> None:
    async with async_session_factory() as session:
        for target in targets:
            await transition_task(
                session,
                task_id=UUID(task_id),
                tenant_id=tenant_id,
                user_id=user_id,
                target=target,
            )


async def _task_state(task_id: str) -> str:
    async with async_session_factory() as session:
        return (
            await session.execute(
                select(Task.state).where(Task.id == UUID(task_id))
            )
        ).scalar_one()


async def _plan_count() -> int:
    async with async_session_factory() as session:
        return int(
            (await session.execute(select(func.count(TaskPlan.id)))).scalar_one()
        )


async def _row_snapshot(task_id: str) -> dict:
    async with async_session_factory() as session:
        row = (
            await session.execute(
                select(RuntimeExecution).where(
                    RuntimeExecution.task_id == UUID(task_id)
                )
            )
        ).scalar_one()
        return {field: getattr(row, field) for field in _SNAPSHOT_FIELDS}


async def _row_identities(task_id: str) -> list:
    async with async_session_factory() as session:
        row = (
            await session.execute(
                select(RuntimeExecution).where(
                    RuntimeExecution.task_id == UUID(task_id)
                )
            )
        ).scalar_one()
        return list(row.identities)


async def _persist_scenario(task_id: str) -> None:
    scenario = UnderstandingOutput.model_validate(_valid_understanding())
    async with async_session_factory() as session:
        row = (
            await session.execute(
                select(RuntimeExecution).where(
                    RuntimeExecution.task_id == UUID(task_id)
                )
            )
        ).scalar_one()
        row.scenario = scenario.model_dump(mode="json")
        await session.commit()


async def _persist_identities(task_id: str) -> None:
    async with async_session_factory() as session:
        row = (
            await session.execute(
                select(RuntimeExecution).where(
                    RuntimeExecution.task_id == UUID(task_id)
                )
            )
        ).scalar_one()
        row.identities = [dict(VALID_IDENTITY)]
        await session.commit()


async def _seed_attendee_memory(tenant_id: UUID, user_id: UUID) -> None:
    async with async_session_factory() as session:
        await create_memory(
            session,
            tenant_id=tenant_id,
            user_id=user_id,
            memory_type=MemoryType.PERSON,
            content=f"{ATTENDEE_NAME} is a colleague; email {ATTENDEE_EMAIL}",
        )


async def _prepare_executable_runtime(
    client: httpx.AsyncClient, email: str
) -> tuple[str, str, UUID, UUID]:
    """Register, connect a Google account, seed memory, and submit a task."""
    token = await _register(client, email)
    tenant_id, user_id = await _user_scope(email)
    await insert_account(
        tenant_id=tenant_id,
        user_id=user_id,
        email=email,
        scopes=(
            "openid email "
            "https://www.googleapis.com/auth/calendar.events "
            "https://www.googleapis.com/auth/contacts.readonly"
        ),
    )
    await _seed_attendee_memory(tenant_id, user_id)
    task_id = await _submit(client, token)
    return token, task_id, tenant_id, user_id


def _assert_identity_refusal(resp: httpx.Response, task_id: str) -> None:
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["current_task_id"] == task_id
    message = detail["error"].lower()
    assert "identity" in message
    assert "runtime" in message
    assert "persisted" in message or "produced" in message


# --- W1/W2: empty identities are refused before planning -------------------


async def test_a_runtime_task_in_understanding_without_identities_is_refused(
    client: httpx.AsyncClient,
    stub: GuardStubModel,
) -> None:
    """Test A: runtime-owned, UNDERSTANDING, empty identities -> 409, no side effects."""
    token = await _register(client, "guard-a@example.com")
    tenant_id, user_id = await _user_scope("guard-a@example.com")
    task_id = await _submit(client, token)
    await _walk(task_id, tenant_id, user_id, UNDERSTANDING)

    before = await _row_snapshot(task_id)
    assert before["scenario"] is None
    assert before["identities"] == []
    plans_before = await _plan_count()

    resp = await client.post(
        f"{API_PREFIX}/tasks/{task_id}/plan", headers=_auth(token)
    )

    _assert_identity_refusal(resp, task_id)
    # Nothing was planned: the model was never consulted.
    assert stub.calls == []
    assert await _plan_count() == plans_before
    # The task and its execution row are untouched (recoverable by the runtime).
    assert await _task_state(task_id) == UNDERSTANDING
    assert await _row_snapshot(task_id) == before


async def test_b_w1_missing_scenario_and_empty_identities_is_refused(
    client: httpx.AsyncClient,
    stub: GuardStubModel,
) -> None:
    """Test B: W1 (``scenario IS NULL``, empty identities) -> 409 and recoverable."""
    token = await _register(client, "guard-b@example.com")
    tenant_id, user_id = await _user_scope("guard-b@example.com")
    task_id = await _submit(client, token)
    await _walk(task_id, tenant_id, user_id, UNDERSTANDING)

    async with async_session_factory() as session:
        row = (
            await session.execute(
                select(RuntimeExecution).where(
                    RuntimeExecution.task_id == UUID(task_id)
                )
            )
        ).scalar_one()
        assert row.scenario is None
        assert row.identities == []
    before = await _row_snapshot(task_id)

    resp = await client.post(
        f"{API_PREFIX}/tasks/{task_id}/plan", headers=_auth(token)
    )

    _assert_identity_refusal(resp, task_id)
    assert stub.calls == []
    assert await _plan_count() == 0
    assert await _task_state(task_id) == UNDERSTANDING
    assert await _row_snapshot(task_id) == before
    # Recoverable: the scenario is still absent and the identity set is still
    # empty, which is exactly the state the runtime's stages consume.
    assert (await _row_snapshot(task_id))["scenario"] is None
    assert await _row_identities(task_id) == []


async def test_c_w2_persisted_scenario_with_empty_identities_is_refused(
    client: httpx.AsyncClient,
    stub: GuardStubModel,
) -> None:
    """Test C: W2 (scenario persisted, identities empty) -> 409, no persistence."""
    token = await _register(client, "guard-c@example.com")
    tenant_id, user_id = await _user_scope("guard-c@example.com")
    task_id = await _submit(client, token)
    await _walk(task_id, tenant_id, user_id, UNDERSTANDING)
    await _persist_scenario(task_id)

    before = await _row_snapshot(task_id)
    assert before["scenario"] is not None
    assert before["identities"] == []
    plans_before = await _plan_count()

    resp = await client.post(
        f"{API_PREFIX}/tasks/{task_id}/plan", headers=_auth(token)
    )

    _assert_identity_refusal(resp, task_id)
    assert stub.calls == []
    assert await _plan_count() == plans_before
    assert await _task_state(task_id) == UNDERSTANDING
    assert await _row_snapshot(task_id) == before


# --- W3: persisted identities are not rejected -----------------------------


async def test_d_w3_persisted_identities_let_planning_and_runtime_proceed(
    client: httpx.AsyncClient,
    stub: GuardStubModel,
) -> None:
    """Test D: W3 -> 201/PLANNING, then the runtime proceeds past identity."""
    token, task_id, tenant_id, user_id = await _prepare_executable_runtime(
        client, "guard-d@example.com"
    )
    await _walk(task_id, tenant_id, user_id, UNDERSTANDING)
    await _persist_scenario(task_id)
    await _persist_identities(task_id)

    resp = await client.post(
        f"{API_PREFIX}/tasks/{task_id}/plan", headers=_auth(token)
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["version"] == 1
    assert await _task_state(task_id) == PLANNING
    # The guard did not filter, repair, or rewrite the persisted identity set.
    assert await _row_identities(task_id) == [VALID_IDENTITY]

    # The runtime proceeds beyond identity resolution with the durable set.
    outcome = await _trigger(client, token, task_id)
    assert outcome["task_state"] == "WAITING_APPROVAL", outcome
    assert await _row_identities(task_id) == [VALID_IDENTITY]


# --- recovery after refusal ------------------------------------------------


async def test_f_runtime_recovers_after_the_planner_refusal(
    client: httpx.AsyncClient,
    stub: GuardStubModel,
) -> None:
    """Test F: refused W1 task, then a normal trigger resolves and persists."""
    token, task_id, tenant_id, user_id = await _prepare_executable_runtime(
        client, "guard-f@example.com"
    )
    await _walk(task_id, tenant_id, user_id, UNDERSTANDING)

    refused = await client.post(
        f"{API_PREFIX}/tasks/{task_id}/plan", headers=_auth(token)
    )
    _assert_identity_refusal(refused, task_id)
    assert await _row_identities(task_id) == []
    assert await _plan_count() == 0

    outcome = await _trigger(client, token, task_id)
    assert outcome["task_state"] == "WAITING_APPROVAL", outcome

    # Identities are durable, not just returned: read them in a new session.
    identities = await _row_identities(task_id)
    assert identities, "the runtime persisted no identities"
    assert identities[0]["name"] == ATTENDEE_NAME
    assert identities[0]["email"] == ATTENDEE_EMAIL
    assert identities[0]["status"] == "CONTACT_MATCH"
    assert identities[0]["confidence"] > 0

    # The runtime advanced past identity resolution and planning, and the plan
    # it created is durable.
    assert await _task_state(task_id) == "WAITING_APPROVAL"
    assert await _plan_count() == 1


# --- defense in depth: the check, and what does not exist ------------------


async def test_g_planning_state_with_empty_identities_is_refused_and_set_untouched(
    client: httpx.AsyncClient,
    stub: GuardStubModel,
) -> None:
    """Test G: PLANNING + empty identities -> 409; a real set is left intact."""
    token = await _register(client, "guard-g@example.com")
    tenant_id, user_id = await _user_scope("guard-g@example.com")
    task_id = await _submit(client, token)
    await _walk(task_id, tenant_id, user_id, UNDERSTANDING, PLANNING)

    before = await _row_snapshot(task_id)
    assert before["identities"] == []

    refused = await client.post(
        f"{API_PREFIX}/tasks/{task_id}/plan", headers=_auth(token)
    )
    _assert_identity_refusal(refused, task_id)
    assert stub.calls == []
    assert await _plan_count() == 0
    assert await _task_state(task_id) == PLANNING
    assert await _row_snapshot(task_id) == before

    # With a durable identity set the same endpoint is allowed to replan, and
    # the guard neither filters nor repairs that set.
    await _persist_identities(task_id)
    allowed = await client.post(
        f"{API_PREFIX}/tasks/{task_id}/plan", headers=_auth(token)
    )
    assert allowed.status_code == 201, allowed.text
    assert await _plan_count() == 1
    assert await _row_identities(task_id) == [VALID_IDENTITY]


async def test_runtime_probe_planning_with_empty_identities_is_now_refused(
    client: httpx.AsyncClient,
    stub: GuardStubModel,
) -> None:
    """Probe: the runtime no longer re-resolves identities in an advanced state.

    REPLAY-03 recorded that a PLANNING task with a scenario and a plan but empty
    identities was driven through ``AgentRuntime._drive`` stage 2, which
    *re-resolved* the guests and persisted them. LIFECYCLE-05 makes that a
    governed refusal instead: the task fails, nothing is re-resolved, and the
    durable row is left exactly as it was.
    """
    token, task_id, tenant_id, user_id = await _prepare_executable_runtime(
        client, "guard-probe@example.com"
    )
    await _walk(task_id, tenant_id, user_id, UNDERSTANDING)
    await _persist_scenario(task_id)

    # Create the plan through the service directly, exactly as the runtime
    # would, but leave identities empty on purpose.
    async with async_session_factory() as session:
        await PlannerService(stub).create_plan(
            session,
            task_id=UUID(task_id),
            tenant_id=tenant_id,
            user_id=user_id,
        )

    assert await _task_state(task_id) == PLANNING
    assert await _row_identities(task_id) == []
    calls_before = len(stub.calls)

    outcome = await _trigger(client, token, task_id)

    assert outcome["task_state"] == "FAILED", outcome
    assert outcome["outcome"] == "FAILED", outcome
    assert "no runtime identities" in outcome["message"].lower(), outcome
    assert len(stub.calls) == calls_before
    assert await _row_identities(task_id) == []


# --- tenant isolation ------------------------------------------------------


def _class_b_task_json() -> dict:
    return {
        "goal": GOAL,
        "success_criteria": list(CRITERIA),
        "fulfillment_profile": "calendar_meeting",
        "allowed_tools": ["calendar"],
        "risk_policy": "LOW",
        "deadline": "2026-06-24T10:00:00Z",
        "max_cost": 100.0,
        "max_steps": 5,
        "verification_method": "automatic",
        "fallback": "ask the user",
    }


async def test_execution_from_another_tenant_cannot_satisfy_or_block_the_guard(
    client: httpx.AsyncClient,
    stub: GuardStubModel,
) -> None:
    """The guard's predicate is tenant-scoped, proven by behaviour.

    A Class-B task (no runtime submission) is given a ``RuntimeExecution`` row
    owned by a *different* tenant, with empty identities. If the guard ignored
    the tenant it would refuse planning; it must not. Planning succeeds, and the
    foreign row is left untouched.
    """
    token = await _register(client, "guard-owner@example.com")
    tenant_id, user_id = await _user_scope("guard-owner@example.com")
    resp = await client.post(
        f"{API_PREFIX}/tasks",
        json=_class_b_task_json(),
        headers=_auth(token),
    )
    assert resp.status_code == 201, resp.text
    task_id = resp.json()["id"]
    await _walk(task_id, tenant_id, user_id, UNDERSTANDING)

    async with async_session_factory() as session:
        foreign_tenant = Tenant(name="foreign-tenant")
        session.add(foreign_tenant)
        await session.flush()
        foreign_user = User(
            tenant_id=foreign_tenant.id,
            email="foreign-user@example.com",
            password_hash="unused",
        )
        session.add(foreign_user)
        await session.flush()
        session.add(
            RuntimeExecution(
                task_id=UUID(task_id),
                tenant_id=foreign_tenant.id,
                user_id=foreign_user.id,
                timezone=TIMEZONE,
                scenario=None,
                identities=[],
                evidence=[],
                cursor=0,
                steps_used=0,
            )
        )
        await session.commit()

    planned = await client.post(
        f"{API_PREFIX}/tasks/{task_id}/plan", headers=_auth(token)
    )
    assert planned.status_code == 201, planned.text
    assert await _task_state(task_id) == PLANNING

    # The foreign row was neither used nor rewritten.
    async with async_session_factory() as session:
        row = (
            await session.execute(
                select(RuntimeExecution).where(
                    RuntimeExecution.task_id == UUID(task_id)
                )
            )
        ).scalar_one()
        assert row.tenant_id != tenant_id
        assert row.identities == []


# --- authorization ordering: identity state is invisible without access ----
#
# ``GET``-style authorization in this application is the canonical
# ``get_task(tenant_id, user_id, task_id)`` lookup: an unknown task and a task
# owned by somebody else resolve to the *same* ``TaskNotFoundError`` and the
# same 404 body. The guard must run only after that decision, or the 409 would
# become an oracle for "this task exists, is Runtime-owned, and has no
# identities yet".

#: The opaque body ``tasks.service._load_task`` raises for every unresolved
#: ``(tenant, user, task)`` triple. Owned by the task service, asserted here so
#: a future change to it cannot silently make authorization observable.
INACCESSIBLE_DETAIL = {"detail": "task not found for tenant"}


async def _add_same_tenant_user(
    tenant_id: UUID, email: str, password: str = "password123"
) -> UUID:
    """Add a second login inside an existing tenant (the MVP is 1:1)."""
    async with async_session_factory() as session:
        user = User(
            tenant_id=tenant_id,
            email=email,
            password_hash=hash_password(password),
        )
        session.add(user)
        await session.commit()
        return user.id


async def _login(
    client: httpx.AsyncClient, email: str, password: str = "password123"
) -> str:
    resp = await client.post(
        f"{API_PREFIX}/auth/login",
        json={"email": email, "password": password},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


async def test_b_same_tenant_other_user_empty_identities_is_indistinguishable(
    client: httpx.AsyncClient,
    stub: GuardStubModel,
) -> None:
    """Test B: same-tenant non-owner, empty identities -> ordinary 404, not 409."""
    owner_token = await _register(client, "owner-b@example.com")
    tenant_id, owner_id = await _user_scope("owner-b@example.com")
    task_id = await _submit(client, owner_token)
    await _walk(task_id, tenant_id, owner_id, UNDERSTANDING)

    await _add_same_tenant_user(tenant_id, "intruder-b@example.com")
    intruder = await _login(client, "intruder-b@example.com")

    before = await _row_snapshot(task_id)
    assert before["identities"] == []
    plans_before = await _plan_count()

    resp = await client.post(
        f"{API_PREFIX}/tasks/{task_id}/plan", headers=_auth(intruder)
    )

    # The owner's identity state must not change the response.
    assert resp.status_code == 404, resp.text
    assert resp.json() == INACCESSIBLE_DETAIL
    assert stub.calls == []
    assert await _plan_count() == plans_before
    assert await _task_state(task_id) == UNDERSTANDING
    assert await _row_snapshot(task_id) == before

    # And the owner is still refused by the identity guard, not by access.
    owner_resp = await client.post(
        f"{API_PREFIX}/tasks/{task_id}/plan", headers=_auth(owner_token)
    )
    _assert_identity_refusal(owner_resp, task_id)


async def test_c_same_tenant_other_user_resolved_identities_is_indistinguishable(
    client: httpx.AsyncClient,
    stub: GuardStubModel,
) -> None:
    """Test C: same-tenant non-owner, resolved identities -> the same 404."""
    owner_token = await _register(client, "owner-c@example.com")
    tenant_id, owner_id = await _user_scope("owner-c@example.com")
    task_id = await _submit(client, owner_token)
    await _walk(task_id, tenant_id, owner_id, UNDERSTANDING)
    await _persist_identities(task_id)

    await _add_same_tenant_user(tenant_id, "intruder-c@example.com")
    intruder = await _login(client, "intruder-c@example.com")

    before = await _row_snapshot(task_id)
    assert before["identities"] == [VALID_IDENTITY]
    plans_before = await _plan_count()

    resp = await client.post(
        f"{API_PREFIX}/tasks/{task_id}/plan", headers=_auth(intruder)
    )

    assert resp.status_code == 404, resp.text
    assert resp.json() == INACCESSIBLE_DETAIL
    assert stub.calls == []
    assert await _plan_count() == plans_before
    assert await _task_state(task_id) == UNDERSTANDING
    assert await _row_snapshot(task_id) == before


async def test_d_unknown_and_inaccessible_share_one_response(
    client: httpx.AsyncClient,
    stub: GuardStubModel,
) -> None:
    """Test D: unknown id, foreign task, and both identity states look identical."""
    owner_token = await _register(client, "owner-d@example.com")
    tenant_id, owner_id = await _user_scope("owner-d@example.com")

    # A Class-B task owned by the owner: no RuntimeExecution envelope.
    created = await client.post(
        f"{API_PREFIX}/tasks",
        json=_class_b_task_json(),
        headers=_auth(owner_token),
    )
    assert created.status_code == 201, created.text
    class_b_id = created.json()["id"]
    await _walk(class_b_id, tenant_id, owner_id, UNDERSTANDING)

    # Two Runtime-owned tasks: one still unresolved, one already resolved.
    empty_id = await _submit(client, owner_token)
    await _walk(empty_id, tenant_id, owner_id, UNDERSTANDING)
    resolved_id = await _submit(client, owner_token)
    await _walk(resolved_id, tenant_id, owner_id, UNDERSTANDING)
    await _persist_identities(resolved_id)

    await _add_same_tenant_user(tenant_id, "intruder-d@example.com")
    intruder = await _login(client, "intruder-d@example.com")

    unknown_id = str(uuid4())
    plans_before = await _plan_count()
    bodies: list[dict] = []
    for target in (unknown_id, class_b_id, empty_id, resolved_id):
        resp = await client.post(
            f"{API_PREFIX}/tasks/{target}/plan", headers=_auth(intruder)
        )
        assert resp.status_code == 404, (target, resp.text)
        bodies.append(resp.json())

    # One normalization for every case: the caller learns nothing about
    # existence, ownership, RuntimeExecution presence, or identity state.
    assert all(body == INACCESSIBLE_DETAIL for body in bodies)
    assert stub.calls == []
    assert await _plan_count() == plans_before
    assert await _task_state(empty_id) == UNDERSTANDING
    assert await _task_state(resolved_id) == UNDERSTANDING


async def test_e_cross_tenant_user_cannot_observe_identity_state(
    client: httpx.AsyncClient,
    stub: GuardStubModel,
) -> None:
    """Test E: a user from another tenant gets the same opaque 404."""
    owner_token = await _register(client, "owner-e@example.com")
    tenant_id, owner_id = await _user_scope("owner-e@example.com")
    task_id = await _submit(client, owner_token)
    await _walk(task_id, tenant_id, owner_id, UNDERSTANDING)

    outsider_token = await _register(client, "outsider-e@example.com")
    before = await _row_snapshot(task_id)
    plans_before = await _plan_count()

    resp = await client.post(
        f"{API_PREFIX}/tasks/{task_id}/plan", headers=_auth(outsider_token)
    )

    assert resp.status_code == 404, resp.text
    assert resp.json() == INACCESSIBLE_DETAIL
    assert stub.calls == []
    assert await _plan_count() == plans_before
    assert await _row_snapshot(task_id) == before

    # The owner is still guarded, so the foreign request neither satisfied nor
    # blocked the presence check.
    owner_resp = await client.post(
        f"{API_PREFIX}/tasks/{task_id}/plan", headers=_auth(owner_token)
    )
    _assert_identity_refusal(owner_resp, task_id)
