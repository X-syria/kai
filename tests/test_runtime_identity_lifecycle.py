"""Runtime lifecycle guard: identities are resolved once, before planning.

``TASK-RUNTIME-IDENTITY-LIFECYCLE-05`` closes the gap REPLAY-03 documented: the
direct planning endpoint refused a task with empty persisted identities, but the
runtime's own recovery path re-resolved them in *any* state. That made an
advanced task able to silently acquire an identity its already-persisted plan
and evidence were never bound to, and a missing scenario surfaced as an
``AssertionError`` (``_drive`` stage 2) rather than a governed refusal.

The behaviours pinned here:

* W1/W2 (``UNDERSTANDING``, no scenario / scenario on record, no identities) are
  still the legitimate identity window: the runtime resolves and continues.
* W3 (``UNDERSTANDING`` with the durable set) replays without re-resolving and
  without rewriting the set.
* ``PLANNING`` and later with an empty set is refused before any model call,
  plan, transition, or re-resolution. Work already on record parks the task in
  ``REQUIRES_ATTENTION``; a task that never ran fails.
* A missing scenario in an advanced state is the same governed refusal, never an
  ``AssertionError``.
* A refused task is terminal, so a repeated trigger writes nothing.

The guard is presence-only by design. No authoritative contract defines an
"invalid" persisted identity set beyond emptiness: the positive-status rule in
``_resolve_guest_identities`` governs *resolution*, and
``calendar_meeting_criteria`` deliberately accepts an unsatisfiable sealed set
rather than rejecting it. This suite therefore does not invent one.
"""

from __future__ import annotations

from datetime import date, timedelta
from uuid import UUID

import httpx
import pytest
from sqlalchemy import func, select

from kai.db.engine import async_session_factory
from kai.db.models import User
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
from kai.tasks.state import EXECUTING, PLANNING, POLICY_CHECK, UNDERSTANDING
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

#: The durable fields the guard must leave byte-identical unless it has to
#: transition the task itself.
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


class LifecycleStubModel:
    """Deterministic understanding + planner double that records every call."""

    model_id = "stub-runtime-identity-lifecycle-v1"

    def __init__(self) -> None:
        self.calls: list[list[dict]] = []
        self.understanding_calls = 0

    async def generate_plan(self, *, messages: list[dict]) -> dict:
        self.calls.append(messages)
        if messages[0]["content"] == UNDERSTANDING_SYSTEM_PROMPT:
            self.understanding_calls += 1
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
def stub() -> LifecycleStubModel:
    return LifecycleStubModel()


@pytest.fixture(autouse=True)
def overrides(backend: GoogleBackend, stub: LifecycleStubModel):
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


async def _inspect(client: httpx.AsyncClient, token: str, task_id: str) -> dict:
    resp = await client.get(
        f"{API_PREFIX}/runtime/tasks/{task_id}",
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


async def _create_plan_directly(
    stub: LifecycleStubModel, task_id: str, tenant_id: UUID, user_id: UUID
) -> None:
    async with async_session_factory() as session:
        await PlannerService(stub).create_plan(
            session,
            task_id=UUID(task_id),
            tenant_id=tenant_id,
            user_id=user_id,
        )


async def _seed_attendee_memory(tenant_id: UUID, user_id: UUID) -> None:
    async with async_session_factory() as session:
        await create_memory(
            session,
            tenant_id=tenant_id,
            user_id=user_id,
            memory_type=MemoryType.PERSON,
            content=f"{ATTENDEE_NAME} is a colleague; email {ATTENDEE_EMAIL}",
        )


async def _prepare_runtime(
    client: httpx.AsyncClient, email: str
) -> tuple[str, str, UUID, UUID]:
    """Register, connect Google, seed the attendee hint, and submit a task."""
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


# --- advanced states are not an identity-resolution window -----------------


async def test_planning_with_empty_identities_fails_without_resolving(
    client: httpx.AsyncClient,
    stub: LifecycleStubModel,
) -> None:
    """PLANNING + empty set -> FAILED, before the model, the plan, or a resolve."""
    token, task_id, tenant_id, user_id = await _prepare_runtime(
        client, "life-adv-empty@example.com"
    )
    await _walk(task_id, tenant_id, user_id, UNDERSTANDING)
    await _persist_scenario(task_id)
    await _create_plan_directly(stub, task_id, tenant_id, user_id)

    assert await _task_state(task_id) == PLANNING
    before = await _row_snapshot(task_id)
    assert before["identities"] == []
    calls_before = len(stub.calls)
    plans_before = await _plan_count()

    outcome = await _trigger(client, token, task_id)

    assert outcome["task_state"] == "FAILED", outcome
    assert outcome["outcome"] == "FAILED", outcome
    assert "no runtime identities" in outcome["message"].lower(), outcome
    # No identity was resolved, no model was consulted, no plan was added.
    assert len(stub.calls) == calls_before
    assert await _plan_count() == plans_before
    assert await _row_identities(task_id) == []
    after = await _row_snapshot(task_id)
    for field in (
        "scenario",
        "evidence",
        "cursor",
        "steps_used",
        "plan_version",
        "plan_hash",
    ):
        assert after[field] == before[field], field

    inspected = await _inspect(client, token, task_id)
    assert inspected["task_state"] == "FAILED"
    assert "no runtime identities" in (inspected["last_error"] or "").lower()


async def test_planning_without_scenario_is_a_governed_refusal_not_an_assertion(
    client: httpx.AsyncClient,
    stub: LifecycleStubModel,
) -> None:
    """A missing scenario in PLANNING fails the task; it never raises."""
    token, task_id, tenant_id, user_id = await _prepare_runtime(
        client, "life-adv-noscenario@example.com"
    )
    await _walk(task_id, tenant_id, user_id, UNDERSTANDING, PLANNING)

    before = await _row_snapshot(task_id)
    assert before["scenario"] is None
    assert before["identities"] == []
    calls_before = len(stub.calls)

    # The pre-LIFECYCLE-05 code reached ``assert scenario is not None`` here and
    # surfaced an AssertionError through the API.
    outcome = await _trigger(client, token, task_id)

    assert outcome["task_state"] == "FAILED", outcome
    assert outcome["outcome"] == "FAILED", outcome
    assert "no understanding" in outcome["message"].lower(), outcome
    assert len(stub.calls) == calls_before
    after = await _row_snapshot(task_id)
    assert after["scenario"] is None
    assert after["identities"] == []


async def test_advanced_state_with_work_on_record_parks_for_a_human(
    client: httpx.AsyncClient,
    stub: LifecycleStubModel,
) -> None:
    """EXECUTING + empty set + work already done -> REQUIRES_ATTENTION, not FAILED."""
    token, task_id, tenant_id, user_id = await _prepare_runtime(
        client, "life-adv-work@example.com"
    )
    await _walk(
        task_id,
        tenant_id,
        user_id,
        UNDERSTANDING,
        PLANNING,
        POLICY_CHECK,
        EXECUTING,
    )
    await _persist_scenario(task_id)

    async with async_session_factory() as session:
        row = (
            await session.execute(
                select(RuntimeExecution).where(
                    RuntimeExecution.task_id == UUID(task_id)
                )
            )
        ).scalar_one()
        row.cursor = 1
        row.steps_used = 1
        row.evidence = [{"kind": "opaque", "slot": "s1"}]
        await session.commit()

    before = await _row_snapshot(task_id)
    assert before["identities"] == []
    calls_before = len(stub.calls)

    outcome = await _trigger(client, token, task_id)

    assert outcome["task_state"] == "REQUIRES_ATTENTION", outcome
    assert outcome["outcome"] == "REQUIRES_ATTENTION", outcome
    assert "no runtime identities" in outcome["message"].lower(), outcome
    assert len(stub.calls) == calls_before
    after = await _row_snapshot(task_id)
    # Nothing was rewound and no identity was resolved.
    assert after["identities"] == []
    assert after["cursor"] == 1
    assert after["steps_used"] == 1
    assert after["evidence"] == before["evidence"]
    assert after["scenario"] == before["scenario"]


async def test_verifying_with_empty_identities_is_not_settled(
    client: httpx.AsyncClient,
    stub: LifecycleStubModel,
) -> None:
    """VERIFYING is advanced too: no identity set means no settlement."""
    token, task_id, tenant_id, user_id = await _prepare_runtime(
        client, "life-verifying@example.com"
    )
    await _walk(
        task_id,
        tenant_id,
        user_id,
        UNDERSTANDING,
        PLANNING,
        POLICY_CHECK,
        EXECUTING,
        "VERIFYING",
    )
    await _persist_scenario(task_id)

    before = await _row_snapshot(task_id)
    assert before["identities"] == []
    calls_before = len(stub.calls)

    outcome = await _trigger(client, token, task_id)

    assert outcome["task_state"] == "FAILED", outcome
    assert "no runtime identities" in outcome["message"].lower(), outcome
    assert len(stub.calls) == calls_before
    after = await _row_snapshot(task_id)
    assert after["identities"] == []
    assert after["scenario"] == before["scenario"]
    assert after["evidence"] == before["evidence"]
    assert after["cursor"] == before["cursor"]


# --- W1/W2/W3 must keep working -------------------------------------------


async def test_w1_understanding_without_scenario_still_resolves(
    client: httpx.AsyncClient,
    stub: LifecycleStubModel,
) -> None:
    """W1: UNDERSTANDING + no scenario + no identities is the identity window."""
    token, task_id, tenant_id, user_id = await _prepare_runtime(
        client, "life-w1@example.com"
    )
    await _walk(task_id, tenant_id, user_id, UNDERSTANDING)

    before = await _row_snapshot(task_id)
    assert before["scenario"] is None
    assert before["identities"] == []

    outcome = await _trigger(client, token, task_id)

    assert outcome["task_state"] == "WAITING_APPROVAL", outcome
    assert stub.understanding_calls == 1
    identities = await _row_identities(task_id)
    assert identities and identities[0]["status"] == "CONTACT_MATCH"


async def test_w2_understanding_with_persisted_scenario_still_resolves(
    client: httpx.AsyncClient,
    stub: LifecycleStubModel,
) -> None:
    """W2: the scenario is already durable; identity still resolves here."""
    token, task_id, tenant_id, user_id = await _prepare_runtime(
        client, "life-w2@example.com"
    )
    await _walk(task_id, tenant_id, user_id, UNDERSTANDING)
    await _persist_scenario(task_id)

    outcome = await _trigger(client, token, task_id)

    assert outcome["task_state"] == "WAITING_APPROVAL", outcome
    # The persisted understanding was read back, not regenerated.
    assert stub.understanding_calls == 0
    identities = await _row_identities(task_id)
    assert identities and identities[0]["status"] == "CONTACT_MATCH"


async def test_w3_replay_with_identities_does_not_rewrite_the_set(
    client: httpx.AsyncClient,
    stub: LifecycleStubModel,
) -> None:
    """W3: a valid non-empty set replays unchanged and is not re-resolved."""
    token, task_id, tenant_id, user_id = await _prepare_runtime(
        client, "life-w3@example.com"
    )
    await _walk(task_id, tenant_id, user_id, UNDERSTANDING)
    await _persist_scenario(task_id)
    await _persist_identities(task_id)

    before = await _row_identities(task_id)
    assert before == [VALID_IDENTITY]

    outcome = await _trigger(client, token, task_id)

    assert outcome["task_state"] == "WAITING_APPROVAL", outcome
    assert stub.understanding_calls == 0
    assert await _row_identities(task_id) == before


# --- refusal is terminal and repeated triggers write nothing --------------


async def test_repeated_trigger_after_lifecycle_refusal_is_idempotent(
    client: httpx.AsyncClient,
    stub: LifecycleStubModel,
) -> None:
    """After the governed refusal, a second trigger refuses and writes nothing.

    ``FAILED`` is a retryable failure, not a terminal state, so the runtime
    answers 409 instead of ``ALREADY_TERMINAL``. What matters is that the
    refusal is deterministic and leaves the settled row untouched.
    """
    token, task_id, tenant_id, user_id = await _prepare_runtime(
        client, "life-repeat@example.com"
    )
    await _walk(task_id, tenant_id, user_id, UNDERSTANDING)
    await _persist_scenario(task_id)
    await _create_plan_directly(stub, task_id, tenant_id, user_id)

    first = await _trigger(client, token, task_id)
    assert first["task_state"] == "FAILED", first
    settled = await _row_snapshot(task_id)
    calls_before = len(stub.calls)

    second = await client.post(
        f"{API_PREFIX}/runtime/tasks/{task_id}/trigger",
        headers=_auth(token),
    )

    assert second.status_code == 409, second.text
    assert "not resumable" in second.json()["detail"]
    assert len(stub.calls) == calls_before
    assert await _row_snapshot(task_id) == settled
