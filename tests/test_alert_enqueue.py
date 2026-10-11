"""B's own tests: the atomic enqueue of the durable alert outbox.

This file is deliberately free of the dispatcher and the HTTP delivery path. It
imports neither ``kai.maintenance.alert_dispatch`` nor
``kai.observability.alerting``: a transition that writes its outbox row has to be
provable on its own, and a test that had to import the module that opens the
socket in order to check that no socket is opened would be proving nothing.

The database is the real Dockerized PostgreSQL the rest of the suite uses, through
the same ``kai.db.engine`` factory. Atomicity - "the failure is durable" and "an
alert is owed" being one commit - is only observable at the transaction seam, so
it is asserted against the database rather than against a helper.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select

from kai.alerts import classification
from kai.alerts import service as alert_service
from kai.alerts.models import DELIVERY_PENDING, Alert
from kai.auth.jwt import decode_access_token
from kai.db.engine import async_session_factory
from kai.tasks.models import Task
from kai.tasks.service import transition_task
from kai.tasks.state import (
    COMPLETED,
    CREATED,
    ESCALATE,
    EXECUTING,
    FAILED,
    REQUIRES_ATTENTION,
    RETRY,
    UNDERSTANDING,
)

API = "/api/v1"

#: Pinned identity vectors. ``Alert.id`` is a uuid5 primary key, so a change to the
#: pre-image, the separator, or the namespace would silently re-key every recorded
#: incident; the vector turns that into a failing test.
TENANT = UUID("11111111-1111-1111-1111-111111111111")
TASK = UUID("22222222-2222-2222-2222-222222222222")

#: A failure reason carrying everything a real provider error carries. It proves
#: the outbox stores none of it: ``fail_reason`` is persisted on the task row and
#: is never copied into the alert.
POISON = "provider rejected token sk_live_51H8xQ for alice@example.com"


async def _register(client: httpx.AsyncClient, email: str) -> tuple[str, Any]:
    resp = await client.post(
        f"{API}/auth/register", json={"email": email, "password": "password123"}
    )
    assert resp.status_code == 201, resp.text
    token = resp.json()["access_token"]
    return token, decode_access_token(token)


async def _create_task(
    client: httpx.AsyncClient,
    token: str,
    goal: str = "Book a flight to Berlin",
) -> dict:
    resp = await client.post(
        f"{API}/tasks",
        json={
            "goal": goal,
            "success_criteria": ["booking reference exists"],
            "fulfillment_profile": "calendar_meeting",
            "allowed_tools": ["calendar"],
            "risk_policy": "LOW",
            "deadline": "2026-06-24T10:00:00Z",
            "max_cost": 500.0,
            "max_steps": 20,
            "verification_method": "automatic",
            "fallback": "ask the user",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _alerts_for(task_id: str) -> list[Alert]:
    async with async_session_factory() as session:
        rows = await session.execute(select(Alert).where(Alert.task_id == UUID(task_id)))
        return list(rows.scalars().all())


async def _task_for(task_id: str) -> Task:
    async with async_session_factory() as session:
        row = await session.execute(select(Task).where(Task.id == UUID(task_id)))
        return row.scalar_one()


# ---------------------------------------------------------------------------
# Atomicity: the row and the state change are one commit
# ---------------------------------------------------------------------------


async def test_the_outbox_row_commits_with_the_transition(
    client: httpx.AsyncClient,
) -> None:
    token, claims = await _register(client, "enqueue-atomic@example.com")
    task = await _create_task(client, token)

    async with async_session_factory() as session:
        row = await transition_task(
            session,
            task_id=UUID(task["id"]),
            tenant_id=claims.tenant_id,
            user_id=claims.user_id,
            target=FAILED,
            fail_reason="planning refused",
        )
    assert row.state == FAILED

    alerts = await _alerts_for(task["id"])
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.kind == classification.ALERT_KIND_FAILURE
    assert alert.from_state == CREATED
    assert alert.to_state == FAILED
    assert alert.delivery_state == DELIVERY_PENDING
    assert alert.schema_version == classification.ALERT_SCHEMA
    assert alert.fulfillment_profile == "calendar_meeting"
    assert alert.occurred_at is not None
    assert alert.tenant_id == claims.tenant_id
    assert alert.id == alert_service.alert_identity(
        tenant_id=claims.tenant_id,
        task_id=UUID(task["id"]),
        kind=classification.ALERT_KIND_FAILURE,
        from_state=CREATED,
        to_state=FAILED,
        task_attempts=0,
    )


async def test_a_failed_enqueue_rolls_back_the_transition(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token, claims = await _register(client, "enqueue-rollback@example.com")
    task = await _create_task(client, token)

    def _explode(*args: object, **kwargs: object) -> object:
        raise RuntimeError("forced enqueue failure")

    monkeypatch.setattr(alert_service, "enqueue_transition_alert", _explode)

    async with async_session_factory() as session:
        with pytest.raises(RuntimeError):
            await transition_task(
                session,
                task_id=UUID(task["id"]),
                tenant_id=claims.tenant_id,
                user_id=claims.user_id,
                target=FAILED,
                fail_reason="planning refused",
            )
        await session.rollback()

    # The enqueue and the state change share one transaction; with the enqueue
    # refused, neither is persisted.
    assert (await _task_for(task["id"])).state == CREATED
    assert await _alerts_for(task["id"]) == []


async def test_the_transition_path_opens_no_socket(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token, claims = await _register(client, "enqueue-nosocket@example.com")
    task = await _create_task(client, token)

    def _forbid(*args: object, **kwargs: object) -> object:
        raise AssertionError("the transition path must not construct an HTTP client")

    monkeypatch.setattr(httpx, "AsyncClient", _forbid)
    monkeypatch.setattr(httpx, "Client", _forbid)

    async with async_session_factory() as session:
        row = await transition_task(
            session,
            task_id=UUID(task["id"]),
            tenant_id=claims.tenant_id,
            user_id=claims.user_id,
            target=FAILED,
            fail_reason="no calendar account",
        )
    assert row.state == FAILED
    assert len(await _alerts_for(task["id"])) == 1


async def test_a_retry_resolution_is_recorded_as_a_second_distinct_alert(
    client: httpx.AsyncClient,
) -> None:
    token, claims = await _register(client, "enqueue-resolution@example.com")
    task = await _create_task(client, token)

    async with async_session_factory() as session:
        for target in (FAILED, RETRY, EXECUTING):
            await transition_task(
                session,
                task_id=UUID(task["id"]),
                tenant_id=claims.tenant_id,
                user_id=claims.user_id,
                target=target,
                fail_reason="planning refused",
            )

    alerts = await _alerts_for(task["id"])
    recorded = {(a.from_state, a.to_state, a.kind) for a in alerts}
    assert (CREATED, FAILED, classification.ALERT_KIND_FAILURE) in recorded
    assert (RETRY, EXECUTING, classification.ALERT_KIND_RESOLUTION) in recorded
    # FAILED -> RETRY stays inside the failure set: the same incident, no alert.
    assert (FAILED, RETRY, classification.ALERT_KIND_FAILURE) not in recorded
    assert len(alerts) == 2


# ---------------------------------------------------------------------------
# Bounded storage: no task content, no provider text
# ---------------------------------------------------------------------------


async def test_the_outbox_stores_no_task_content(client: httpx.AsyncClient) -> None:
    token, claims = await _register(client, "enqueue-bounded@example.com")
    task = await _create_task(client, token, goal=f"email {POISON}")

    async with async_session_factory() as session:
        await transition_task(
            session,
            task_id=UUID(task["id"]),
            tenant_id=claims.tenant_id,
            user_id=claims.user_id,
            target=FAILED,
            fail_reason=POISON,
        )

    alerts = await _alerts_for(task["id"])
    assert len(alerts) == 1
    alert = alerts[0]
    blob = " ".join(str(getattr(alert, column.key)) for column in alert.__table__.columns)
    assert "sk_live_51H8xQ" not in blob
    assert "alice@example.com" not in blob
    # No delivery has been attempted, so the delivery column holds nothing.
    assert alert.last_error is None


async def test_a_repeated_enqueue_of_one_incident_is_a_no_op(
    client: httpx.AsyncClient,
) -> None:
    token, claims = await _register(client, "enqueue-dedup@example.com")
    task = await _create_task(client, token)

    async with async_session_factory() as session:
        row = await transition_task(
            session,
            task_id=UUID(task["id"]),
            tenant_id=claims.tenant_id,
            user_id=claims.user_id,
            target=FAILED,
            fail_reason="planning refused",
        )
        first = await alert_service.enqueue_transition_alert(
            session,
            task=row,
            kind=classification.ALERT_KIND_FAILURE,
            from_state=CREATED,
            to_state=FAILED,
            occurred_at=row.failed_at,
            schema_version=classification.ALERT_SCHEMA,
        )
        second = await alert_service.enqueue_transition_alert(
            session,
            task=row,
            kind=classification.ALERT_KIND_FAILURE,
            from_state=CREATED,
            to_state=FAILED,
            occurred_at=row.failed_at,
            schema_version=classification.ALERT_SCHEMA,
        )
        await session.commit()

    assert first == second
    alerts = await _alerts_for(task["id"])
    assert len(alerts) == 1
    assert alerts[0].id == first


# ---------------------------------------------------------------------------
# The pure contract: classification and identity
# ---------------------------------------------------------------------------


def test_a_transition_into_a_failure_is_a_failure_alert() -> None:
    assert classification.classify_transition(CREATED, FAILED) == classification.ALERT_KIND_FAILURE
    assert (
        classification.classify_transition(EXECUTING, FAILED)
        == classification.ALERT_KIND_FAILURE
    )


def test_only_leaving_the_failure_set_resolves() -> None:
    assert (
        classification.classify_transition(RETRY, EXECUTING)
        == classification.ALERT_KIND_RESOLUTION
    )
    # A relabelled failure is the same incident, and an escalation is a hand-off:
    # neither is a resolution.
    assert classification.classify_transition(FAILED, REQUIRES_ATTENTION) is None
    assert classification.classify_transition(FAILED, ESCALATE) is None


def test_a_non_alerting_edge_is_silent() -> None:
    assert classification.classify_transition(CREATED, UNDERSTANDING) is None


def test_an_unknown_state_is_never_read_as_healthy() -> None:
    assert classification.classify_transition(FAILED, "NOT_A_STATE") is None
    assert classification.classify_transition(ESCALATE, "NOT_A_STATE") is None
    assert classification.classify_transition("NOT_A_STATE", COMPLETED) is None


def test_the_open_failure_states_are_exactly_the_declared_set() -> None:
    assert frozenset(
        {FAILED, REQUIRES_ATTENTION, RETRY, ESCALATE}
    ) == classification.OPEN_FAILURE_STATES


def test_alert_identity_is_a_pure_fixed_function() -> None:
    kwargs = {
        "tenant_id": TENANT,
        "task_id": TASK,
        "kind": classification.ALERT_KIND_FAILURE,
        "from_state": EXECUTING,
        "to_state": FAILED,
        "task_attempts": 0,
    }
    assert alert_service.alert_identity(**kwargs) == UUID(
        "731f8257-46db-5b3c-9455-7d9efb820be4"
    )
    assert alert_service.alert_identity(**kwargs) == alert_service.alert_identity(**kwargs)


def test_a_later_attempt_is_a_different_incident() -> None:
    assert alert_service.alert_identity(
        tenant_id=TENANT,
        task_id=TASK,
        kind=classification.ALERT_KIND_FAILURE,
        from_state=EXECUTING,
        to_state=FAILED,
        task_attempts=1,
    ) == UUID("ade43da9-5f65-5930-a879-d514146836a7")
