"""Task Manager integration tests — real Dockerized PostgreSQL, no mocks.

Service-level calls and API calls both land in the same shared Postgres that
the app uses at runtime. Cross-tenant and cross-user isolation are asserted at
the row level (list/get/mutation silently resolve to nothing) and at the API
level (404 on foreign ids, 401 without a token).
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
from datetime import UTC, datetime

import httpx
import pytest
from pytest import LogCaptureFixture
from sqlalchemy import inspect as sa_inspect

from kai.auth.jwt import TokenClaims, decode_access_token
from kai.db.engine import async_session_factory
from kai.db.models import User
from kai.main import app
from kai.tasks.models import Task
from kai.tasks.schemas import TaskContractInput
from kai.tasks.service import (
    InvalidTransition,
    TaskNotFoundError,
    archive_task,
    assign_task,
    count_tasks,
    create_task,
    get_task,
    list_tasks,
    transition_task,
)
from kai.tasks.state import (
    COMPLETED,
    CREATED,
    ESCALATE,
    EXECUTING,
    FAILED,
    REQUIRES_ATTENTION,
    RETRY,
    TRANSITIONS,
    UNDERSTANDING,
)

API_PREFIX = "/api/v1"


def _contract(**overrides: object) -> TaskContractInput:
    data: dict[str, object] = {
        "goal": "Book a flight to Berlin",
        "success_criteria": ["booking reference exists", "cost confirmed"],
        "allowed_tools": ["calendar", "gmail"],
        "risk_policy": "LOW",
        "deadline": datetime(2026, 6, 24, 10, 0, tzinfo=UTC),
        "max_cost": 500.0,
        "max_steps": 20,
        "verification_method": "automatic",
        "fallback": "ask the user",
    }
    data.update(overrides)
    return TaskContractInput.model_validate(data)


async def _register(client: httpx.AsyncClient, email: str) -> tuple[str, TokenClaims]:
    resp = await client.post(
        f"{API_PREFIX}/auth/register",
        json={"email": email, "password": "password123"},
    )
    assert resp.status_code == 201, resp.text
    token = resp.json()["access_token"]
    return token, decode_access_token(token)


def _task_json(goal: str = "Book a flight to Berlin") -> dict:
    return {
        "goal": goal,
        "success_criteria": ["booking reference exists"],
        "allowed_tools": ["calendar"],
        "risk_policy": "LOW",
        "deadline": "2026-06-24T10:00:00Z",
        "max_cost": 500.0,
        "max_steps": 20,
        "verification_method": "automatic",
        "fallback": "ask the user",
    }


async def _create_via_api(client: httpx.AsyncClient, token: str) -> dict:
    resp = await client.post(
        f"{API_PREFIX}/tasks",
        json=_task_json(),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# --- create / get / list ---------------------------------------------------

async def test_create_get_list_roundtrip(client: httpx.AsyncClient) -> None:
    token, claims = await _register(client, "alice@example.com")
    created = await _create_via_api(client, token)

    assert created["state"] == CREATED
    assert created["tenant_id"] == str(claims.tenant_id)
    assert created["user_id"] == str(claims.user_id)
    assert created["contract"]["goal"] == "Book a flight to Berlin"

    fetched = await client.get(
        f"{API_PREFIX}/tasks/{created['id']}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert fetched.status_code == 200
    assert fetched.json()["id"] == created["id"]
    assert fetched.json()["contract"]["deadline"] == "2026-06-24T10:00:00Z"

    listing = await client.get(
        f"{API_PREFIX}/tasks", headers={"Authorization": f"Bearer {token}"}
    )
    assert listing.status_code == 200
    assert [t["id"] for t in listing.json()["tasks"]] == [created["id"]]


async def test_contract_roundtrip_preserves_details(
    client: httpx.AsyncClient,
) -> None:
    token, _ = await _register(client, "roundtrip@example.com")
    resp = await client.post(
        f"{API_PREFIX}/tasks",
        json=_task_json(),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201
    contract = resp.json()["contract"]
    assert contract["success_criteria"] == ["booking reference exists"]
    assert contract["allowed_tools"] == ["calendar"]
    assert contract["risk_policy"] == "LOW"
    assert contract["max_cost"] == 500.0
    assert contract["max_steps"] == 20
    assert contract["verification_method"] == "automatic"


async def test_create_ignores_caller_supplied_scope_fields(
    client: httpx.AsyncClient,
) -> None:
    token, claims = await _register(client, "scope@example.com")
    resp = await client.post(
        f"{API_PREFIX}/tasks",
        json={**_task_json(), "tenant_id": "other", "user_id": "other"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["tenant_id"] == str(claims.tenant_id)
    assert body["user_id"] == str(claims.user_id)


# --- tenant isolation ------------------------------------------------------

async def test_cross_tenant_read_is_404(client: httpx.AsyncClient) -> None:
    token_a, _ = await _register(client, "acme@corp.example")
    token_b, _ = await _register(client, "globex@corp.example")
    created = await _create_via_api(client, token_a)

    resp = await client.get(
        f"{API_PREFIX}/tasks/{created['id']}",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert resp.status_code == 404


async def test_cross_tenant_transition_is_404_and_does_not_mutate(
    client: httpx.AsyncClient,
) -> None:
    token_a, _ = await _register(client, "acme@corp.example")
    token_b, _ = await _register(client, "globex@corp.example")
    created = await _create_via_api(client, token_a)

    resp = await client.post(
        f"{API_PREFIX}/tasks/{created['id']}/transition",
        json={"target_state": UNDERSTANDING},
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert resp.status_code == 404

    mine = await client.get(
        f"{API_PREFIX}/tasks/{created['id']}",
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert mine.status_code == 200
    assert mine.json()["state"] == CREATED


async def test_cross_tenant_archive_is_404(client: httpx.AsyncClient) -> None:
    token_a, _ = await _register(client, "acme@corp.example")
    token_b, _ = await _register(client, "globex@corp.example")
    created = await _create_via_api(client, token_a)
    resp = await client.post(
        f"{API_PREFIX}/tasks/{created['id']}/archive",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert resp.status_code == 404


async def test_cross_tenant_listing_is_empty(client: httpx.AsyncClient) -> None:
    token_a, _ = await _register(client, "acme@corp.example")
    token_b, _ = await _register(client, "globex@corp.example")
    await _create_via_api(client, token_a)
    await _create_via_api(client, token_a)
    acme_created = await _create_via_api(client, token_a)

    g1 = await _create_via_api(client, token_b)
    g2 = await _create_via_api(client, token_b)

    resp = await client.get(
        f"{API_PREFIX}/tasks", headers={"Authorization": f"Bearer {token_b}"}
    )
    assert resp.status_code == 200
    tasks = resp.json()["tasks"]
    assert {t["id"] for t in tasks} == {g1["id"], g2["id"]}
    assert acme_created["id"] not in {t["id"] for t in tasks}


async def test_service_scopes_every_lookup_by_tenant() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        _, claims_a = await _register(client, "acme@corp.example")
        _, claims_b = await _register(client, "globex@corp.example")

    async with async_session_factory() as session:
        task = await create_task(
            session,
            tenant_id=claims_a.tenant_id,
            user_id=claims_a.user_id,
            contract=_contract(),
        )
        task_id = task.id

        assert (
            await get_task(
                session, task_id=task_id, tenant_id=claims_a.tenant_id
            )
        ).id == task_id

        with pytest.raises(TaskNotFoundError):
            await get_task(
                session, task_id=task_id, tenant_id=claims_b.tenant_id
            )
        assert await count_tasks(session, tenant_id=claims_b.tenant_id) == 0
        assert await list_tasks(session, tenant_id=claims_b.tenant_id) == []


# --- user isolation within a tenant ---------------------------------------

async def test_same_tenant_other_user_cannot_touch_task() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        _, claims_a = await _register(client, "owner@corp.example")

    other = User(tenant_id=claims_a.tenant_id, email="second@corp.example", password_hash="unused")
    async with async_session_factory() as session:
        session.add(other)
        await session.commit()
        await session.refresh(other)

        task = await create_task(
            session,
            tenant_id=claims_a.tenant_id,
            user_id=claims_a.user_id,
            contract=_contract(),
        )
        task_id = task.id

        with pytest.raises(TaskNotFoundError):
            await get_task(
                session,
                task_id=task_id,
                tenant_id=claims_a.tenant_id,
                user_id=other.id,
            )
        assert await list_tasks(
            session,
            tenant_id=claims_a.tenant_id,
            user_id=other.id,
        ) == []
        with pytest.raises(TaskNotFoundError):
            await transition_task(
                session,
                task_id=task_id,
                tenant_id=claims_a.tenant_id,
                user_id=other.id,
                target=UNDERSTANDING,
            )

        # Assign to the second user *within the same tenant*: the owner loses
        # visibility, the second user gains it.
        await assign_task(
            session,
            task_id=task_id,
            tenant_id=claims_a.tenant_id,
            user_id=other.id,
        )
        moved = await get_task(
            session,
            task_id=task_id,
            tenant_id=claims_a.tenant_id,
            user_id=other.id,
        )
        assert moved.user_id == other.id
        with pytest.raises(TaskNotFoundError):
            await get_task(
                session,
                task_id=task_id,
                tenant_id=claims_a.tenant_id,
                user_id=claims_a.user_id,
            )


# --- state machine through the service ------------------------------------

async def test_every_declared_transition_applies() -> None:
    for current, targets in TRANSITIONS.items():
        for target in targets:
            row = await _persist(state=current)
            async with async_session_factory() as session:
                await transition_task(
                    session,
                    task_id=row.id,
                    tenant_id=row.tenant_id,
                    user_id=row.user_id,
                    target=target,
                )
            async with async_session_factory() as session:
                reloaded = await get_task(
                    session,
                    task_id=row.id,
                    tenant_id=row.tenant_id,
                    user_id=row.user_id,
                )
                assert reloaded.state == target, (current, target)


async def test_illegal_transition_is_rejected_and_persists_nothing() -> None:
    row = await _persist(state=CREATED)
    async with async_session_factory() as session:
        with pytest.raises(InvalidTransition) as exc_info:
            await transition_task(
                session,
                task_id=row.id,
                tenant_id=row.tenant_id,
                user_id=row.user_id,
                target=EXECUTING,
            )
        assert "illegal transition" in str(exc_info.value)
        await session.rollback()
    async with async_session_factory() as session:
        reloaded = await get_task(
            session,
            task_id=row.id,
            tenant_id=row.tenant_id,
            user_id=row.user_id,
        )
        assert reloaded.state == CREATED
        assert reloaded.attempts == 0


async def test_completed_is_frozen() -> None:
    row = await _persist(state=COMPLETED)
    async with async_session_factory() as session:
        with pytest.raises(InvalidTransition):
            await transition_task(
                session,
                task_id=row.id,
                tenant_id=row.tenant_id,
                user_id=row.user_id,
                target=EXECUTING,
            )


async def test_retry_path_increments_attempts_and_clears_error() -> None:
    row = await _persist(state=CREATED)
    async with async_session_factory() as session:
        await transition_task(
            session,
            task_id=row.id,
            tenant_id=row.tenant_id,
            user_id=row.user_id,
            target=FAILED,
            fail_reason="external provider timed out",
        )
        failed = await get_task(
            session,
            task_id=row.id,
            tenant_id=row.tenant_id,
            user_id=row.user_id,
        )
        assert failed.state == FAILED
        assert failed.attempts == 0
        assert failed.last_error == "external provider timed out"
        assert failed.failed_at is not None

        await transition_task(
            session,
            task_id=row.id,
            tenant_id=row.tenant_id,
            user_id=row.user_id,
            target=RETRY,
        )
        retried = await get_task(
            session,
            task_id=row.id,
            tenant_id=row.tenant_id,
            user_id=row.user_id,
        )
        assert retried.state == RETRY
        assert retried.attempts == 1
        assert retried.last_error is None
        assert retried.failed_at is None

        await transition_task(
            session,
            task_id=row.id,
            tenant_id=row.tenant_id,
            user_id=row.user_id,
            target=EXECUTING,
        )
        resumed = await get_task(
            session,
            task_id=row.id,
            tenant_id=row.tenant_id,
            user_id=row.user_id,
        )
        assert resumed.state == EXECUTING


async def test_retry_budget_exhaustion_forces_escalation() -> None:
    row = await _persist(state=FAILED, attempts=1, max_retries=1)
    async with async_session_factory() as session:
        with pytest.raises(InvalidTransition) as exc_info:
            await transition_task(
                session,
                task_id=row.id,
                tenant_id=row.tenant_id,
                user_id=row.user_id,
                target=RETRY,
            )
        assert "budget exhausted" in str(exc_info.value)

        await transition_task(
            session,
            task_id=row.id,
            tenant_id=row.tenant_id,
            user_id=row.user_id,
            target=ESCALATE,
        )
        escalated = await get_task(
            session,
            task_id=row.id,
            tenant_id=row.tenant_id,
            user_id=row.user_id,
        )
        assert escalated.state == ESCALATE


async def test_requires_attention_path() -> None:
    row = await _persist(state=CREATED)
    async with async_session_factory() as session:
        await transition_task(
            session,
            task_id=row.id,
            tenant_id=row.tenant_id,
            user_id=row.user_id,
            target=REQUIRES_ATTENTION,
            fail_reason="needs human input",
        )
        flagged = await get_task(
            session,
            task_id=row.id,
            tenant_id=row.tenant_id,
            user_id=row.user_id,
        )
        assert flagged.state == REQUIRES_ATTENTION
        assert flagged.last_error == "needs human input"

        await transition_task(
            session,
            task_id=row.id,
            tenant_id=row.tenant_id,
            user_id=row.user_id,
            target=RETRY,
        )
        retried = await get_task(
            session,
            task_id=row.id,
            tenant_id=row.tenant_id,
            user_id=row.user_id,
        )
        assert retried.state == RETRY
        assert retried.attempts == 1


async def test_failed_can_escalate_directly() -> None:
    row = await _persist(state=FAILED, attempts=0)
    async with async_session_factory() as session:
        await transition_task(
            session,
            task_id=row.id,
            tenant_id=row.tenant_id,
            user_id=row.user_id,
            target=ESCALATE,
        )
        reloaded = await get_task(
            session,
            task_id=row.id,
            tenant_id=row.tenant_id,
            user_id=row.user_id,
        )
        assert reloaded.state == ESCALATE


async def test_archive_requires_terminal_state() -> None:
    row = await _persist(state=CREATED)
    async with async_session_factory() as session:
        with pytest.raises(InvalidTransition):
            await archive_task(
                session,
                task_id=row.id,
                tenant_id=row.tenant_id,
                user_id=row.user_id,
            )
    terminal = await _persist(state=COMPLETED)
    async with async_session_factory() as session:
        await archive_task(
            session,
            task_id=terminal.id,
            tenant_id=terminal.tenant_id,
            user_id=terminal.user_id,
        )
        assert await list_tasks(session, tenant_id=terminal.tenant_id) == []
        assert len(
            await list_tasks(
                session, tenant_id=terminal.tenant_id, include_archived=True
            )
        ) == 1


async def test_archive_via_api_hides_from_list(client: httpx.AsyncClient) -> None:
    token, _ = await _register(client, "archive@example.com")
    created = await _create_via_api(client, token)
    await client.post(
        f"{API_PREFIX}/tasks/{created['id']}/transition",
        json={"target_state": UNDERSTANDING},
        headers={"Authorization": f"Bearer {token}"},
    )
    # archive requires terminal: move it there through the machine.
    for target in ("PLANNING", "POLICY_CHECK", "EXECUTING", "VERIFYING", COMPLETED):
        resp = await client.post(
            f"{API_PREFIX}/tasks/{created['id']}/transition",
            json={"target_state": target},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, (target, resp.text)
    resp = await client.post(
        f"{API_PREFIX}/tasks/{created['id']}/archive",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200

    listing = await client.get(
        f"{API_PREFIX}/tasks", headers={"Authorization": f"Bearer {token}"}
    )
    assert listing.json()["tasks"] == []


# --- API transitions -------------------------------------------------------

async def test_transition_via_api(client: httpx.AsyncClient) -> None:
    token, _ = await _register(client, "api@example.com")
    created = await _create_via_api(client, token)

    resp = await client.post(
        f"{API_PREFIX}/tasks/{created['id']}/transition",
        json={"target_state": UNDERSTANDING},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["state"] == UNDERSTANDING


async def test_illegal_transition_via_api_is_409(client: httpx.AsyncClient) -> None:
    token, _ = await _register(client, "api@example.com")
    created = await _create_via_api(client, token)

    resp = await client.post(
        f"{API_PREFIX}/tasks/{created['id']}/transition",
        json={"target_state": EXECUTING},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 409
    assert "illegal transition" in resp.json()["detail"]["error"]

    verify = await client.get(
        f"{API_PREFIX}/tasks/{created['id']}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert verify.json()["state"] == CREATED


async def test_unknown_target_state_is_422(client: httpx.AsyncClient) -> None:
    token, _ = await _register(client, "api@example.com")
    created = await _create_via_api(client, token)
    resp = await client.post(
        f"{API_PREFIX}/tasks/{created['id']}/transition",
        json={"target_state": "NOT_A_STATE"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422


async def test_transition_missing_task_is_404(client: httpx.AsyncClient) -> None:
    token, _ = await _register(client, "api@example.com")
    resp = await client.post(
        f"{API_PREFIX}/tasks/00000000-0000-0000-0000-000000000000/transition",
        json={"target_state": UNDERSTANDING},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404


# --- authentication --------------------------------------------------------

async def test_endpoints_require_authentication(client: httpx.AsyncClient) -> None:
    zero = "00000000-0000-0000-0000-000000000000"
    assert (await client.post(f"{API_PREFIX}/tasks", json=_task_json())).status_code == 401
    assert (await client.get(f"{API_PREFIX}/tasks")).status_code == 401
    assert (await client.get(f"{API_PREFIX}/tasks/{zero}")).status_code == 401
    assert (
        await client.post(
            f"{API_PREFIX}/tasks/{zero}/transition",
            json={"target_state": UNDERSTANDING},
        )
    ).status_code == 401
    assert (await client.post(f"{API_PREFIX}/tasks/{zero}/archive")).status_code == 401


async def test_invalid_token_is_401(client: httpx.AsyncClient) -> None:
    headers = {"Authorization": "Bearer not-a-jwt"}
    assert (await client.get(f"{API_PREFIX}/tasks", headers=headers)).status_code == 401


# --- malformed input -------------------------------------------------------

async def test_create_empty_body_is_422(client: httpx.AsyncClient) -> None:
    token, _ = await _register(client, "mal@example.com")
    resp = await client.post(
        f"{API_PREFIX}/tasks", json={}, headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 422


async def test_create_blank_goal_is_422(client: httpx.AsyncClient) -> None:
    token, _ = await _register(client, "mal@example.com")
    resp = await client.post(
        f"{API_PREFIX}/tasks",
        json={**_task_json(), "goal": "   "},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422


async def test_create_naive_deadline_is_422(client: httpx.AsyncClient) -> None:
    token, _ = await _register(client, "mal@example.com")
    resp = await client.post(
        f"{API_PREFIX}/tasks",
        json={**_task_json(), "deadline": "2026-06-24T10:00:00"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422


async def test_create_bad_max_steps_is_422(client: httpx.AsyncClient) -> None:
    token, _ = await _register(client, "mal@example.com")
    resp = await client.post(
        f"{API_PREFIX}/tasks",
        json={**_task_json(), "max_steps": 1001},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422


async def test_create_negative_max_cost_is_422(client: httpx.AsyncClient) -> None:
    token, _ = await _register(client, "mal@example.com")
    resp = await client.post(
        f"{API_PREFIX}/tasks",
        json={**_task_json(), "max_cost": -5},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422


async def test_create_high_risk_over_step_cap_is_422(client: httpx.AsyncClient) -> None:
    token, _ = await _register(client, "mal@example.com")
    resp = await client.post(
        f"{API_PREFIX}/tasks",
        json={**_task_json(), "risk_policy": "CRITICAL", "max_steps": 101},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422


# --- no sensitive data leakage ---------------------------------------------

async def test_responses_never_expose_internal_metadata(
    client: httpx.AsyncClient, caplog: LogCaptureFixture
) -> None:
    token, _ = await _register(client, "leak@example.com")
    created = await _create_via_api(client, token)
    resp = await client.post(
        f"{API_PREFIX}/tasks/{created['id']}/transition",
        json={"target_state": UNDERSTANDING},
        headers={"Authorization": f"Bearer {token}"},
    )
    listing = await client.get(
        f"{API_PREFIX}/tasks", headers={"Authorization": f"Bearer {token}"}
    )
    for body in (created, resp.json(), listing.json()):
        text = repr(body)
        assert "last_error" not in text
        assert "failed_at" not in text
        assert "attempts" not in text
        assert "is_archived" not in text
        assert "Book a flight to Berlin" in text  # goal is a legit contract field

    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        await client.get(
            f"{API_PREFIX}/tasks/{created['id']}",
            headers={"Authorization": f"Bearer {token}"},
        )
    combined = caplog.text
    assert "access_token" not in combined
    assert created["tenant_id"] not in combined
    assert "last_error" not in combined


# --- migration -------------------------------------------------------------

REVISION_PREV = "5f3e1d2c4b6a"
REVISION_HEAD = "f1e2d3c4b5a6"


def _alembic(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _table_names_fresh() -> list[str]:
    """Inspect on its own disposable engine + event loop.

    The migration test runs synchronously and must not touch the shared
    ``engine``'s pool, whose connections live on pytest's loop.
    """

    from sqlalchemy.ext.asyncio import create_async_engine

    from kai.config import get_settings

    async def _names() -> list[str]:
        eng = create_async_engine(get_settings().database_url)
        try:
            async with eng.connect() as conn:
                tables = await conn.run_sync(
                    lambda sync: sa_inspect(sync).get_table_names()
                )
                return list(tables)
        finally:
            await eng.dispose()

    return asyncio.run(_names())


def test_migration_is_rollbackable_and_forward() -> None:
    try:
        down = _alembic("downgrade", REVISION_PREV)
        assert down.returncode == 0, down.stderr
        current = _alembic("current").stdout
        assert REVISION_PREV in current

        assert "tasks" not in _table_names_fresh()
        assert "task_plans" not in _table_names_fresh()

        up = _alembic("upgrade", "head")
        assert up.returncode == 0, up.stderr
        assert "tasks" in _table_names_fresh()
        assert "task_plans" in _table_names_fresh()
        assert REVISION_HEAD in _alembic("current").stdout
    finally:
        # Never leave the schema behind head, even on assertion failure.
        _alembic("upgrade", "head")


async def _persist(**overrides: object) -> Task:
    from uuid import uuid4

    from kai.db.models import Tenant

    async with async_session_factory() as session:
        tenant = Tenant(name=f"tenant-{uuid4()}")
        session.add(tenant)
        await session.flush()
        user = User(
            tenant_id=tenant.id,
            email=f"user-{uuid4()}@example.com",
            password_hash="unused",
        )
        session.add(user)
        await session.flush()

        params: dict[str, object] = {
            "tenant_id": tenant.id,
            "user_id": user.id,
            "state": CREATED,
            "goal": "persisted goal",
            "success_criteria": ["criterion"],
            "allowed_tools": [],
            "risk_policy": "LOW",
            "max_cost": 0.0,
            "max_steps": 20,
            "verification_method": "automatic",
            "fallback": "ask user",
            "attempts": 0,
            "max_retries": 2,
        }
        params.update(overrides)
        task = Task(**params)  # type: ignore[arg-type]
        session.add(task)
        await session.commit()
        await session.refresh(task)
        return task


def test_contract_validation_errors_use_ids_not_row_content() -> None:
    # Pydantic's boundary errors mention field names, never stored values.
    from pydantic import ValidationError

    import kai.tasks.schemas as schemas

    with pytest.raises(ValidationError) as exc_info:
        schemas.TaskContractInput.model_validate({})
    text = str(exc_info.value)
    assert "goal" in text
    assert "FAILED" not in text