"""Unit tests for the durable alert outbox's pure, dependency-free pieces.

Everything here runs with no database and no socket: the identity function, the
retry schedule, the deterministic-id integrity check, the payload rebuild, and
the delivery classifier (driven through an ``httpx`` mock transport). The tests
that need PostgreSQL, the ASGI app, and a real receiver live in
``tests/test_alerting.py``.

The identity vectors below are pinned deliberately. ``Alert.id`` is a uuid5 and
is the primary key, so if the pre-image, the separator, or the namespace ever
changed, every already-recorded incident would get a new id and the deduplication
guarantee would silently dissolve. A pinned vector turns that into a failing
test instead.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest

from kai.alerts import service
from kai.alerts.service import (
    ALERT_IDENTITY_NAMESPACE,
    MAX_ATTEMPTS,
    MAX_BACKOFF_SECONDS,
    AlertIntegrityError,
    QueuedAlert,
    alert_identity,
    backoff_ceiling,
    next_backoff,
)
from kai.maintenance.alert_dispatch import _payload_from_claim
from kai.observability import alerting

TENANT = UUID("11111111-1111-1111-1111-111111111111")
TASK = UUID("22222222-2222-2222-2222-222222222222")


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def test_the_identity_namespace_is_the_committed_constant() -> None:
    assert UUID("a4540a2c-9058-464d-82d5-1be2d327ac83") == ALERT_IDENTITY_NAMESPACE


def test_a_failure_identity_is_a_fixed_vector() -> None:
    assert (
        alert_identity(
            tenant_id=TENANT,
            task_id=TASK,
            kind="task_failure",
            from_state="EXECUTING",
            to_state="FAILED",
            task_attempts=0,
        )
        == UUID("731f8257-46db-5b3c-9455-7d9efb820be4")
    )


def test_a_later_attempt_is_a_different_incident() -> None:
    # The attempt counter is in the identity, so a second failure of the same task
    # after a retry is a genuinely new incident rather than a suppressed duplicate.
    assert (
        alert_identity(
            tenant_id=TENANT,
            task_id=TASK,
            kind="task_failure",
            from_state="EXECUTING",
            to_state="FAILED",
            task_attempts=1,
        )
        == UUID("ade43da9-5f65-5930-a879-d514146836a7")
    )


def test_a_resolution_is_a_fixed_vector() -> None:
    assert (
        alert_identity(
            tenant_id=TENANT,
            task_id=TASK,
            kind="task_failure_resolved",
            from_state="RETRY",
            to_state="EXECUTING",
            task_attempts=1,
        )
        == UUID("4689d6cf-9dd7-564d-a84c-af519201b225")
    )


def test_the_identity_is_a_pure_function() -> None:
    kwargs = {
        "tenant_id": TENANT,
        "task_id": TASK,
        "kind": "task_failure",
        "from_state": "EXECUTING",
        "to_state": "FAILED",
        "task_attempts": 3,
    }
    assert alert_identity(**kwargs) == alert_identity(**kwargs)


# ---------------------------------------------------------------------------
# Retry schedule
# ---------------------------------------------------------------------------


def test_the_backoff_ceiling_doubles_and_then_caps() -> None:
    assert backoff_ceiling(1) == 30.0
    assert backoff_ceiling(2) == 60.0
    assert backoff_ceiling(3) == 120.0
    assert backoff_ceiling(4) == 240.0
    assert backoff_ceiling(5) == 480.0
    # 30 * 2**5 = 960 would exceed the cap.
    assert backoff_ceiling(6) == MAX_BACKOFF_SECONDS
    assert backoff_ceiling(50) == MAX_BACKOFF_SECONDS


def test_next_backoff_stays_inside_the_ceiling() -> None:
    for attempt in range(1, MAX_ATTEMPTS + 1):
        ceiling = backoff_ceiling(attempt)
        for _ in range(50):
            delay = next_backoff(attempt)
            assert 0.0 <= delay <= ceiling


# ---------------------------------------------------------------------------
# The deterministic-id integrity check
# ---------------------------------------------------------------------------


def _existing(**overrides: object) -> SimpleNamespace:
    fields: dict[str, object] = {
        "tenant_id": TENANT,
        "task_id": TASK,
        "kind": "task_failure",
        "from_state": "EXECUTING",
        "to_state": "FAILED",
        "task_attempts": 0,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _values(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "tenant_id": TENANT,
        "task_id": TASK,
        "kind": "task_failure",
        "from_state": "EXECUTING",
        "to_state": "FAILED",
        "task_attempts": 0,
    }
    values.update(overrides)
    return values


def test_an_identical_conflict_is_accepted() -> None:
    service._assert_consistent(_existing(), _values())  # type: ignore[arg-type]


def test_a_collision_with_a_different_incident_raises() -> None:
    with pytest.raises(AlertIntegrityError):
        service._assert_consistent(
            _existing(to_state="REQUIRES_ATTENTION"), _values()  # type: ignore[arg-type]
        )


def test_a_vanished_conflict_row_raises() -> None:
    with pytest.raises(AlertIntegrityError):
        service._assert_consistent(None, _values())


# ---------------------------------------------------------------------------
# Rebuilding the payload from a durable row
# ---------------------------------------------------------------------------


def _claim(**overrides: object) -> QueuedAlert:
    fields: dict[str, object] = {
        "id": alert_identity(
            tenant_id=TENANT,
            task_id=TASK,
            kind="task_failure",
            from_state="EXECUTING",
            to_state="FAILED",
            task_attempts=0,
        ),
        "kind": "task_failure",
        "task_id": TASK,
        "tenant_id": TENANT,
        "from_state": "EXECUTING",
        "to_state": "FAILED",
        "fulfillment_profile": "calendar_meeting",
        "task_attempts": 0,
        "task_max_retries": 2,
        "schema_version": alerting.ALERT_SCHEMA,
        "occurred_at": datetime(2026, 10, 2, 20, 0, tzinfo=UTC),
        "attempts": 1,
        "claim_token": UUID("33333333-3333-3333-3333-333333333333"),
    }
    fields.update(overrides)
    return QueuedAlert(**fields)  # type: ignore[arg-type]


def test_a_claim_rebuilds_the_recorded_payload() -> None:
    alert = _payload_from_claim(_claim())
    assert alert.kind == "task_failure"
    assert alert.task_id == str(TASK)
    assert alert.tenant_id == str(TENANT)
    assert alert.occurred_at == "2026-10-02T20:00:00Z"
    assert alert.payload()["schema"] == alerting.ALERT_SCHEMA


def test_a_claim_without_frozen_facts_is_refused() -> None:
    # A row written before the outbox revision has no frozen payload facts; the
    # builder refuses it rather than inventing values.
    with pytest.raises(AlertIntegrityError):
        _payload_from_claim(_claim(fulfillment_profile=None))


@pytest.mark.parametrize(
    "overrides",
    [
        {"task_attempts": None},
        {"task_max_retries": None},
        {"schema_version": None},
    ],
)
def test_any_missing_frozen_fact_is_refused(overrides: dict[str, object]) -> None:
    with pytest.raises(AlertIntegrityError):
        _payload_from_claim(_claim(**overrides))


# ---------------------------------------------------------------------------
# The delivery classifier
# ---------------------------------------------------------------------------


def _alert() -> alerting.TaskAlert:
    return alerting.TaskAlert(
        kind="task_failure",
        task_id=str(TASK),
        tenant_id=str(TENANT),
        from_state="EXECUTING",
        to_state="FAILED",
        fulfillment_profile="calendar_meeting",
        attempts=0,
        max_retries=2,
        occurred_at="2026-10-02T20:00:00Z",
    )


async def _attempt(handler: object) -> alerting.DeliveryAttempt:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=transport) as client:
        return await alerting.attempt_delivery(
            _alert(), url="https://hooks.example.com/x", timeout=5.0, client=client
        )


async def test_a_2xx_is_a_delivery() -> None:
    result = await _attempt(lambda request: httpx.Response(204))
    assert result.delivered is True
    assert result.retryable is False
    assert result.error is None


@pytest.mark.parametrize("status", [408, 429, 500, 503])
async def test_transient_statuses_are_retryable(status: int) -> None:
    result = await _attempt(lambda request: httpx.Response(status))
    assert result.delivered is False
    assert result.retryable is True
    assert result.error == "HTTPStatusError"


@pytest.mark.parametrize("status", [400, 401, 404, 422])
async def test_other_4xx_are_permanent(status: int) -> None:
    result = await _attempt(lambda request: httpx.Response(status))
    assert result.delivered is False
    assert result.retryable is False
    assert result.error == "HTTPStatusError"


async def test_a_transport_failure_never_raises() -> None:
    def _explode(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    result = await _attempt(_explode)
    assert result.delivered is False
    assert result.retryable is True
    # A bounded class name, never the driver message (which can embed the URL).
    assert result.error == "ConnectError"


def test_the_outbound_payload_never_carries_task_content() -> None:
    body = _alert().render()
    for forbidden in (b"goal", b"last_error", b"token", b"secret"):
        assert forbidden not in body
