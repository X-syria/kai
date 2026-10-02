"""Observability tests: each one fails against the pre-observability code.

The bar here is higher than "the helper returns a number". A counter that
increments but is never reached from a real request would pass a helper-level
assertion while the production signal stayed pinned at zero - the same class of
tautological test the security round was written to reject. So the metric
assertions drive the actual ASGI app over the actual database, and read the
values back out of the rendered exposition text, which is what a Prometheus
scraper receives.

Grouped as: HTTP metrics, auth metrics, task metrics, provider metrics, database
metrics, request-id policy, structured logging, tracing, export opt-in,
cardinality limits, and telemetry-failure isolation.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from kai.config import get_settings
from kai.db.engine import async_session_factory
from kai.logging import configure_logging
from kai.observability import metrics
from kai.observability.context import (
    MAX_REQUEST_ID_LENGTH,
    build_route_table,
    route_label,
)
from kai.observability.tracing import (
    SpanContext,
    current_span,
    format_traceparent,
    new_span_id,
    new_trace_id,
    parse_traceparent,
    span,
)
from kai.security import ratelimit

PASSWORD = "Password123!"


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_metrics() -> Iterator[None]:
    """Give each test its own registry.

    The collectors are process-global, so without this a test would assert on a
    count an earlier test inflated - which is exactly how "the metric works"
    tests quietly stop testing anything.
    """
    metrics.reset_metrics()
    yield
    metrics.reset_metrics()


@pytest.fixture
def captured_logs() -> Iterator[list[dict[str, Any]]]:
    """Capture every structured event as a parsed dict, via KAI's own pipeline.

    ``configure_logging(stream=...)`` is used rather than ``structlog.configure``
    so the test captures exactly the bytes a real deployment emits.
    """
    records: list[dict[str, Any]] = []

    class _CollectingStream:
        """A tee that keeps events queryable as dicts while still emitting them."""

        def write(self, payload: str) -> int:
            for line in payload.splitlines():
                if line.strip():
                    records.append(json.loads(line))
            return len(payload)

        def flush(self) -> None:
            """Events are parsed on write, so there is nothing to flush."""

    configure_logging(stream=_CollectingStream())
    try:
        yield records
    finally:
        configure_logging()


@asynccontextmanager
async def _session_scope() -> AsyncIterator[AsyncSession]:
    async with async_session_factory() as session:
        yield session


def _exposition() -> str:
    return metrics.render_latest().decode()


def _sample(metric_name: str, **labels: str) -> float | None:
    """One labelled sample read back out of the rendered exposition.

    Reading the text rather than a metric object is deliberate: it asserts what
    a scraper actually receives, including label names and values.
    """
    for line in _exposition().splitlines():
        if not line.startswith(metric_name):
            continue
        series, _, value = line.rpartition(" ")
        if all(f'{key}="{val}"' in series for key, val in labels.items()):
            return float(value)
    return None


def _sample_count(metric_name: str, **labels: str) -> int:
    """How many distinct label combinations exist for a metric family."""
    return sum(
        1
        for line in _exposition().splitlines()
        if line.startswith(metric_name + "{")
        and all(f'{key}="{val}"' in line for key, val in labels.items())
    )


def _series(metric_name: str) -> list[str]:
    return [ln for ln in _exposition().splitlines() if ln.startswith(metric_name + "{")]


def _spans(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in records if r.get("event") == "trace.span"]


def _events(records: list[dict[str, Any]], event: str) -> list[dict[str, Any]]:
    return [r for r in records if r.get("event") == event]


async def _register(client: httpx.AsyncClient, email: str) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/auth/register", json={"email": email, "password": PASSWORD}
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _auth(client: httpx.AsyncClient, email: str) -> dict[str, str]:
    created = await _register(client, email)
    return {"Authorization": f"Bearer {created['access_token']}"}


@contextmanager
def _env(**overrides: str) -> Iterator[None]:
    """Apply settings overrides for one block, then restore the cached settings."""
    previous = {key: os.environ.get(key) for key in overrides}
    os.environ.update(overrides)
    get_settings.cache_clear()
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        get_settings.cache_clear()


@contextmanager
def _span_capture() -> Iterator[list[dict[str, Any]]]:
    """Collect the structured events emitted inside the block."""
    records: list[dict[str, Any]] = []

    class _CollectingStream:
        def write(self, payload: str) -> int:
            for line in payload.splitlines():
                if line.strip():
                    records.append(json.loads(line))
            return len(payload)

        def flush(self) -> None:
            """Events are parsed on write, so there is nothing to flush."""

    configure_logging(stream=_CollectingStream())
    try:
        yield records
    finally:
        configure_logging()


# ---------------------------------------------------------------------------
# HTTP metrics
# ---------------------------------------------------------------------------


async def test_a_served_request_reaches_the_exposition(
    client: httpx.AsyncClient, clean_metrics: None
) -> None:
    """The headline gate: a real request must be visible in the metrics text."""
    await client.get("/api/v1/health")

    assert _sample("kai_http_requests_total", route="/api/v1/health", method="GET") == 1.0
    assert _sample("kai_http_request_duration_seconds_count", route="/api/v1/health") == 1.0


async def test_the_route_label_is_the_template_not_the_concrete_path(
    client: httpx.AsyncClient, clean_metrics: None
) -> None:
    """The cardinality guarantee, proved end to end on a real path.

    Reading the route before routing happened would collapse every labelled
    route to "unmatched", and this is the assertion that catches it.
    """
    headers = await _auth(client, "template@acme.example")
    await client.post(
        "/api/v1/tasks",
        headers=headers,
        json={
            "goal": "g",
            "success_criteria": ["c"],
            "fulfillment_profile": "calendar_meeting",
            "fallback": "ask the user",
        },
    )

    first = await client.get("/api/v1/tasks", headers=headers)
    assert first.status_code == 200
    task_id = first.json()["tasks"][0]["id"]

    for _ in range(3):
        response = await client.get(f"/api/v1/tasks/{task_id}", headers=headers)
        assert response.status_code == 200
        await client.get(f"/api/v1/tasks/{uuid4()}", headers=headers)  # 404s differ too

    assert (
        _sample("kai_http_requests_total", route="/api/v1/tasks/{task_id}", method="GET")
        == 3.0
    ), "the concrete path was used as a label, or routing never resolved"
    assert _sample("kai_http_requests_total", route="unmatched") is None
    assert task_id not in _exposition(), "a task id leaked into the exposition"


async def test_the_status_label_is_a_class_not_a_code(
    client: httpx.AsyncClient, clean_metrics: None
) -> None:
    """A per-code label would multiply series without adding alertable signal."""
    await client.get("/api/v1/health")
    await client.get("/api/v1/auth/me")
    await client.get("/api/v1/nope")

    classes = {
        line.split('status_class="')[1].split('"')[0]
        for line in _series("kai_http_requests_total")
    }
    assert classes == {"2xx", "4xx"}


async def test_a_server_fault_increments_the_5xx_metric(clean_metrics: None) -> None:
    """A fault the service produced itself must be counted as one.

    ``raise_app_exceptions=False`` is what makes this an end-to-end 500 rather
    than an exception escaping the ASGI call, so the assertion covers the real
    middleware response path an operator would see.
    """
    from kai.main import create_app

    with pytest.MonkeyPatch.context() as mp:
        from kai.api import health as health_module

        async def _boom() -> None:
            raise RuntimeError("injected")

        # The route resolves this name through the module, so patching the
        # module attribute is what the handler actually calls.
        mp.setattr(health_module, "check_database_reachable", _boom)
        app = create_app()
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as raw:
            response = await raw.get("/api/v1/health")

    assert response.status_code == 500
    assert _sample("kai_http_5xx_total", route="/api/v1/health", method="GET") == 1.0


async def test_a_4xx_does_not_increment_the_5xx_metric(
    client: httpx.AsyncClient, clean_metrics: None
) -> None:
    """Otherwise a burst of bad tokens reads as an outage."""
    for _ in range(3):
        await client.get("/api/v1/auth/me", headers={"Authorization": "Bearer nope"})

    assert _sample("kai_http_5xx_total", method="GET") is None


async def test_a_raising_handler_logs_a_failure_event(
    client: httpx.AsyncClient, clean_metrics: None, captured_logs: list[dict[str, Any]]
) -> None:
    """The failure event must carry the type, never the message."""
    with pytest.MonkeyPatch.context() as mp:
        from kai.api import health as health_module

        async def _boom() -> None:
            raise RuntimeError("secret-in-the-message")

        mp.setattr(health_module, "check_database_reachable", _boom)
        with pytest.raises(RuntimeError):
            await client.get("/api/v1/health")

    failures = _events(captured_logs, "http_request_failed")
    assert failures, "no http_request_failed event was recorded"
    assert failures[0]["error_type"] == "RuntimeError"
    assert failures[0]["status_code"] == 500
    assert "secret-in-the-message" not in json.dumps(failures, default=str)


async def test_the_scrape_endpoint_does_not_count_itself(
    client: httpx.AsyncClient, clean_metrics: None
) -> None:
    """~5,760 scrapes a day would otherwise mask the request rate."""
    from kai.main import create_app

    with _env(METRICS_ENABLED="true", METRICS_PATH="/api/v1/metrics"):
        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as raw:
            for _ in range(5):
                assert (await raw.get("/api/v1/metrics")).status_code == 200
            await raw.get("/api/v1/health")

    assert _sample("kai_http_requests_total", route="/api/v1/metrics") is None
    assert _sample("kai_http_requests_total", route="/api/v1/health", method="GET") == 1.0


# ---------------------------------------------------------------------------
# Auth metrics
# ---------------------------------------------------------------------------


async def test_auth_failures_are_counted_by_reason(
    client: httpx.AsyncClient, clean_metrics: None
) -> None:
    missing = await client.get("/api/v1/auth/me")
    garbage = await client.get(
        "/api/v1/auth/me", headers={"Authorization": "Bearer not-a-jwt"}
    )

    assert missing.status_code == garbage.status_code == 401
    assert _sample("kai_auth_failures_total", reason="missing") == 1.0
    assert _sample("kai_auth_failures_total", reason="invalid") == 1.0


async def test_a_revoked_token_is_counted_as_revoked(
    client: httpx.AsyncClient, clean_metrics: None
) -> None:
    headers = await _auth(client, "revoked-metric@acme.example")
    assert (await client.post("/api/v1/auth/logout", headers=headers)).status_code == 200

    assert (await client.get("/api/v1/auth/me", headers=headers)).status_code == 401
    assert _sample("kai_auth_failures_total", reason="revoked") == 1.0


async def test_login_failure_does_not_distinguish_unknown_email_from_wrong_password(
    client: httpx.AsyncClient, clean_metrics: None
) -> None:
    """Splitting these two would turn the metric into an account-enumeration
    oracle for whoever can read the monitoring system."""
    unknown = await client.post(
        "/api/v1/auth/login", json={"email": "nobody@acme.example", "password": "wrong"}
    )
    await _register(client, "exists@acme.example")
    wrong = await client.post(
        "/api/v1/auth/login", json={"email": "exists@acme.example", "password": "wrong"}
    )

    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json()
    assert _sample("kai_auth_failures_total", reason="bad_credentials") == 2.0
    for secret in ("nobody@acme.example", "exists@acme.example"):
        assert secret not in _exposition()


async def test_a_rate_limited_request_is_counted_as_a_http_request(
    client: httpx.AsyncClient, clean_metrics: None
) -> None:
    """The 429 is produced by middleware that never reaches a route.

    This is the assertion that holds the envelope in place: if the observability
    middleware ever moves back inside the limiter, these rejections vanish from
    the metrics and the test fails.
    """
    ratelimit.reset_store()
    with _env(AUTH_RATE_LIMIT_REQUESTS="2", AUTH_RATE_LIMIT_WINDOW_SECONDS="60"):
        responses = [
            await client.post(
                "/api/v1/auth/login",
                json={"email": f"rl{i}@acme.example", "password": "wrong"},
            )
            for i in range(6)
        ]
    ratelimit.reset_store()

    codes = [r.status_code for r in responses]
    rejected = sum(1 for code in codes if code == 429)
    assert rejected == 4, f"expected the limiter to reject the last 4 attempts: {codes}"
    assert (
        _sample("kai_auth_rate_limit_rejections_total", reason="exceeded") == float(rejected)
    )
    # Every attempt, rejected or served, is an HTTP request with a status class.
    assert _sample("kai_http_requests_total", route="/api/v1/auth/login", method="POST") == 6.0
    assert _sample("kai_http_requests_total", status_class="4xx") == 6.0


async def test_a_rate_limited_response_still_carries_a_request_id(
    client: httpx.AsyncClient, clean_metrics: None
) -> None:
    """The correlation id must exist even on a response no handler produced."""
    ratelimit.reset_store()
    with _env(AUTH_RATE_LIMIT_REQUESTS="1", AUTH_RATE_LIMIT_WINDOW_SECONDS="60"):
        await client.post(
            "/api/v1/auth/login",
            json={"email": "first@acme.example", "password": "wrong"},
        )
        rejected = await client.post(
            "/api/v1/auth/login",
            json={"email": "second@acme.example", "password": "wrong"},
        )
    ratelimit.reset_store()

    assert rejected.status_code == 429
    assert 0 < len(rejected.headers.get("X-Request-ID", "")) <= MAX_REQUEST_ID_LENGTH


# ---------------------------------------------------------------------------
# Task metrics
# ---------------------------------------------------------------------------


async def test_a_task_start_and_completion_are_counted(
    client: httpx.AsyncClient, clean_metrics: None
) -> None:
    from kai.tasks.service import INTERNAL_ACTOR, transition_task

    headers = await _auth(client, "tasks@acme.example")
    created = await client.post(
        "/api/v1/tasks",
        headers=headers,
        json={
            "goal": "schedule a review",
            "success_criteria": ["an event exists"],
            "fulfillment_profile": "calendar_meeting",
            "fallback": "ask the user to pick a time",
        },
    )
    assert created.status_code == 201, created.text
    assert _sample("kai_tasks_started_total", profile="calendar_meeting") == 1.0

    task_id = UUID(created.json()["id"])
    tenant_id = UUID(created.json()["tenant_id"])
    user_id = UUID(created.json()["user_id"])

    # Lifecycle states are the runtime's to assert, so they are driven through
    # the service as the internal actor rather than over the API.
    with _span_capture() as records:
        async with _session_scope() as session:
            for target in (
                "UNDERSTANDING",
                "PLANNING",
                "POLICY_CHECK",
                "EXECUTING",
                "VERIFYING",
                "COMPLETED",
            ):
                await transition_task(
                    session,
                    task_id=task_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    target=target,
                    actor=INTERNAL_ACTOR,
                )

    assert _sample("kai_tasks_completed_total", profile="calendar_meeting") == 1.0
    assert _sample("kai_tasks_failed_total") is None
    assert _spans(records), "the task transition produced no span"


async def test_a_failed_task_is_counted_under_failed(
    client: httpx.AsyncClient, clean_metrics: None
) -> None:
    from kai.tasks.service import INTERNAL_ACTOR, transition_task

    headers = await _auth(client, "failing@acme.example")
    created = await client.post(
        "/api/v1/tasks",
        headers=headers,
        json={
            "goal": "fail",
            "success_criteria": ["never"],
            "fulfillment_profile": "calendar_meeting",
            "fallback": "none",
        },
    )
    task = created.json()

    async with _session_scope() as session:
        await transition_task(
            session,
            task_id=UUID(task["id"]),
            tenant_id=UUID(task["tenant_id"]),
            user_id=UUID(task["user_id"]),
            target="FAILED",
            actor=INTERNAL_ACTOR,
        )

    assert _sample("kai_tasks_failed_total", profile="calendar_meeting") == 1.0
    assert _sample("kai_tasks_completed_total") is None


# ---------------------------------------------------------------------------
# Provider metrics
# ---------------------------------------------------------------------------


async def test_a_provider_rejection_is_counted(clean_metrics: None) -> None:
    from kai.calendar.client import GoogleInvalidTokenError, fetch_events

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(GoogleInvalidTokenError):
            await fetch_events(
                http,
                access_token="token",
                start=datetime(2026, 1, 1, tzinfo=UTC),
                end=datetime(2026, 1, 2, tzinfo=UTC),
            )

    assert (
        _sample("kai_provider_errors_total", provider="google", operation="list_events") == 1.0
    )


async def test_provider_latency_is_recorded_on_success(clean_metrics: None) -> None:
    from kai.calendar.client import fetch_events

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await fetch_events(
            http,
            access_token="token",
            start=datetime(2026, 1, 1, tzinfo=UTC),
            end=datetime(2026, 1, 2, tzinfo=UTC),
        )

    assert (
        _sample(
            "kai_provider_latency_seconds_count", provider="google", operation="list_events"
        )
        == 1.0
    )


async def test_a_failed_call_is_excluded_from_the_latency_histogram(clean_metrics: None) -> None:
    """Otherwise the p99 stops describing healthy calls."""
    from kai.calendar.client import GoogleApiError, create_event
    from kai.calendar.service_models import CreateEventRequest

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={})

    request = CreateEventRequest(
        title="x",
        start=datetime(2026, 1, 1, 9, tzinfo=UTC),
        end=datetime(2026, 1, 1, 10, tzinfo=UTC),
        timezone="UTC",
        attendees=[],
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(GoogleApiError):
            await create_event(http, access_token="token", req=request)

    assert (
        _sample("kai_provider_errors_total", provider="google", operation="create_event")
        == 1.0
    )
    assert (
        _sample(
            "kai_provider_latency_seconds_count",
            provider="google",
            operation="create_event",
        )
        is None
    )


# ---------------------------------------------------------------------------
# Database metrics
# ---------------------------------------------------------------------------


async def test_a_statement_the_database_refuses_is_counted(clean_metrics: None) -> None:
    """A DB fault must be visible without waiting for someone to read a log."""
    async with _session_scope() as session:
        with pytest.raises(ProgrammingError):
            await session.execute(text("SELECT * FROM a_table_that_is_not_there"))
        await session.rollback()

    assert _sample("kai_db_errors_total", operation="execute") == 1.0


async def test_a_healthy_statement_adds_no_error_sample(clean_metrics: None) -> None:
    async with _session_scope() as session:
        await session.execute(text("SELECT 1"))
        await session.rollback()

    assert _sample("kai_db_errors_total") is None


# ---------------------------------------------------------------------------
# Request-id policy
# ---------------------------------------------------------------------------


async def test_a_safe_inbound_request_id_is_echoed(
    client: httpx.AsyncClient, clean_metrics: None
) -> None:
    response = await client.get("/api/v1/health", headers={"X-Request-ID": "client-supplied_1234"})
    assert response.headers["X-Request-ID"] == "client-supplied_1234"


@pytest.mark.parametrize(
    ("hostile", "why"),
    [
        ("a" * (MAX_REQUEST_ID_LENGTH + 1), "oversized amplification channel"),
        ("has space", "not id-shaped"),
        ("has\nnewline", "log-line forgery"),
        ("has\rcarriage", "header injection"),
        ('quote"inject', "json/log escaping"),
        ("semi;colon", "ambiguous in a log line"),
        ("203.0.113.7", "an address is not a correlation id"),
        ("", "empty"),
        ("   ", "whitespace only"),
    ],
)
async def test_a_hostile_inbound_request_id_is_replaced(
    client: httpx.AsyncClient, clean_metrics: None, hostile: str, why: str
) -> None:
    """Refusing is not cosmetic: these values are an attack surface, not a
    formatting preference."""
    response = await client.get("/api/v1/health", headers={"X-Request-ID": hostile})
    returned = response.headers["X-Request-ID"]

    assert 0 < len(returned) <= MAX_REQUEST_ID_LENGTH
    assert returned != hostile, f"{why}: the hostile value was honoured"


async def test_a_request_id_is_minted_when_absent(
    client: httpx.AsyncClient, clean_metrics: None
) -> None:
    response = await client.get("/api/v1/health")
    assert 0 < len(response.headers.get("X-Request-ID", "")) <= MAX_REQUEST_ID_LENGTH


def test_the_route_label_falls_back_when_nothing_matched() -> None:
    """The unmatched bucket is a fixed value, which is what bounds the label."""
    table = build_route_table(["/api/v1/health", "/api/v1/tasks/{task_id}"])

    class _Route:
        path = "/api/v1/tasks/{task_id}"

    assert route_label({"path": "/api/v1/health"}, table) == "/api/v1/health"
    assert route_label({"path": "/api/v1/tasks/abc"}, table) == "/api/v1/tasks/{task_id}"
    assert route_label({"path": "/api/v1/nope"}, table) == "unmatched"
    assert route_label({"path": "/etc/passwd"}, table) == "unmatched"
    assert route_label({}, table) == "unmatched"
    # A scope whose route object survived a table built too early still labels.
    assert route_label({"path": "/api/v1/nope", "route": _Route()}, table) == (
        "/api/v1/tasks/{task_id}"
    )


def test_a_concrete_path_is_never_used_as_a_label() -> None:
    """The cardinality guarantee, asserted against the matcher itself."""
    table = build_route_table(["/api/v1/tasks/{task_id}"])
    for _ in range(5):
        assert table.label("/api/v1/tasks/" + "x" * 8) == "/api/v1/tasks/{task_id}"
    assert len(table.patterns) == 1


def test_a_template_does_not_match_across_segments() -> None:
    """Otherwise ``/tasks/{id}`` would label ``/tasks/a/b/c`` and hide 404s."""
    table = build_route_table(["/api/v1/tasks/{task_id}"])
    assert table.label("/api/v1/tasks/a/b/c") == "unmatched"


# ---------------------------------------------------------------------------
# Label bounding
# ---------------------------------------------------------------------------
#
# Every label value is drawn from a closed set or is a route template. The clamp
# is what makes that true, so it is tested directly here rather than being left to
# be covered incidentally by whichever recording test happens to pass an
# out-of-set value.


@pytest.mark.parametrize(
    ("recorder", "sample", "attacker_controlled"),
    [
        (
            lambda value: metrics.record_auth_failure(value),
            "kai_auth_failures_total",
            "someone@example.com",
        ),
        (
            lambda value: metrics.record_rate_limit_rejection(value),
            "kai_auth_rate_limit_rejections_total",
            "203.0.113.7",
        ),
        (
            lambda value: metrics.record_provider_call(
                provider=value, operation="op", duration_seconds=0.01, error="boom"
            ),
            "kai_provider_errors_total",
            "internal-host-42",
        ),
        (
            lambda value: metrics.record_db_error(value),
            "kai_db_errors_total",
            "postgresql://kai:hunter2@db/kai",
        ),
    ],
)
def test_an_out_of_set_label_value_collapses_to_unknown(
    recorder, sample: str, attacker_controlled: str, clean_metrics: None
) -> None:
    """An unrecognised value must not become a new time series.

    Without the clamp, anything a caller can influence that reaches a label
    becomes a permanent, unbounded series in whatever scrapes the metric — which
    is how a single client can be handed a storage-exhaustion primitive through
    the observability path.
    """
    recorder(attacker_controlled)

    rendered = metrics.render_latest().decode()
    assert attacker_controlled not in rendered
    assert metrics.UNKNOWN_LABEL in rendered, rendered
    assert sample in rendered


def test_a_declared_label_value_is_not_clamped() -> None:
    """The clamp must not swallow legitimate values, or every series collapses
    into ``unknown`` and the metric stops being useful."""
    metrics.record_auth_failure("bad_credentials")
    metrics.record_db_error("execute")

    rendered = metrics.render_latest().decode()
    assert 'reason="bad_credentials"' in rendered
    assert 'operation="execute"' in rendered


def test_an_unknown_http_method_does_not_create_a_series() -> None:
    """``method`` reaches a label from a request line, so it is caller input."""
    metrics.record_http_request(
        method="BREW",
        route="/api/v1/health",
        status_code=200,
        duration_seconds=0.01,
    )

    rendered = metrics.render_latest().decode()
    assert "BREW" not in rendered
    assert 'method="unknown"' in rendered


# ---------------------------------------------------------------------------
# Structured logging
# ---------------------------------------------------------------------------


async def test_one_request_emits_exactly_one_request_event(
    client: httpx.AsyncClient, clean_metrics: None, captured_logs: list[dict[str, Any]]
) -> None:
    await client.get("/api/v1/health")

    events = _events(captured_logs, "http_request")
    assert len(events) == 1
    event = events[0]
    assert event["request_id"]
    assert event["method"] == "GET"
    assert event["route"] == "/api/v1/health"
    assert event["status_code"] == 200
    assert event["level"] == "info"
    assert isinstance(event["duration_seconds"], float)


async def test_the_request_id_reaches_the_failure_event(
    client: httpx.AsyncClient, clean_metrics: None, captured_logs: list[dict[str, Any]]
) -> None:
    """Correlation has to survive a failure - that is the reason it exists."""
    with pytest.MonkeyPatch.context() as mp:
        from kai.api import health as health_module

        async def _boom() -> None:
            raise RuntimeError("injected")

        mp.setattr(health_module, "check_database_reachable", _boom)
        with pytest.raises(RuntimeError):
            await client.get("/api/v1/health", headers={"X-Request-ID": "trace-me-please"})

    failures = _events(captured_logs, "http_request_failed")
    assert failures and failures[0]["request_id"] == "trace-me-please"


async def test_no_credential_shaped_value_reaches_a_log_line(
    client: httpx.AsyncClient, clean_metrics: None, captured_logs: list[dict[str, Any]]
) -> None:
    """The round's hard prohibition, asserted end to end.

    Drives a real login, a real authenticated call, a real logout, and a real
    post-logout rejection, then scans every emitted event for the token, the
    password, and any JWT-shaped value.
    """
    password = "Sup3rSecret-Passphrase!"
    response = await client.post(
        "/api/v1/auth/register",
        json={"email": "nologs@acme.example", "password": password},
    )
    assert response.status_code == 201, response.text
    token = response.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    await client.get("/api/v1/auth/me", headers=headers)
    await client.post("/api/v1/auth/logout", headers=headers)
    await client.get("/api/v1/auth/me", headers=headers)
    await client.post(
        "/api/v1/auth/login",
        json={"email": "nologs@acme.example", "password": password},
    )

    rendered = json.dumps(captured_logs, default=str)
    assert token not in rendered, "an access token was logged"
    assert password not in rendered, "a password was logged"
    assert "Bearer" not in rendered, "an Authorization header was logged"
    assert "eyJ" not in rendered, "a JWT-shaped value was logged"


async def test_the_exporter_emits_one_json_object_per_line(
    clean_metrics: None,
) -> None:
    """The span exporter is the log stream KAI already ships; prove the shape."""
    buffer = io.StringIO()
    configure_logging(stream=buffer)
    try:
        from kai.logging import get_logger

        with span("exporter.probe", attributes={"method": "GET", "route": "/probe"}):
            get_logger("kai.test").info("probe", request_id="abc")
    finally:
        configure_logging()

    lines = [ln for ln in buffer.getvalue().splitlines() if ln.strip()]
    payloads = [json.loads(ln) for ln in lines]
    events = {p["event"] for p in payloads}

    assert "probe" in events
    assert "trace.span" in events
    assert all("timestamp" in p and "level" in p for p in payloads)
    finished = next(p for p in payloads if p["event"] == "trace.span")
    assert finished["status"] == "ok"
    assert finished["route"] == "/probe"


# ---------------------------------------------------------------------------
# Tracing
# ---------------------------------------------------------------------------


async def test_a_request_produces_a_finished_span(
    client: httpx.AsyncClient, clean_metrics: None, captured_logs: list[dict[str, Any]]
) -> None:
    await client.get("/api/v1/health")

    request_spans = [s for s in _spans(captured_logs) if s["span"] == "http.request"]
    assert len(request_spans) == 1
    span_event = request_spans[0]
    assert len(span_event["trace_id"]) == 32
    assert len(span_event["span_id"]) == 16
    assert span_event["parent_span_id"] is None
    assert span_event["status"] == "ok"
    assert span_event["route"] == "/api/v1/health"
    assert span_event["method"] == "GET"
    assert span_event["status_code"] == 200


async def test_auth_nests_under_the_request_span(
    client: httpx.AsyncClient, clean_metrics: None, captured_logs: list[dict[str, Any]]
) -> None:
    """request -> auth in one trace, which is the stated minimum for tracing.

    This is the assertion that fails if the request span is not made current for
    the downstream task: the auth span would then be its own root trace.
    """
    headers = await _auth(client, "nested@acme.example")
    await client.get("/api/v1/auth/me", headers=headers)

    spans = {s["span"]: s for s in _spans(captured_logs)}
    assert "http.request" in spans, "no request span"
    assert "auth.verify" in spans, "the auth dependency produced no span"
    assert spans["auth.verify"]["parent_span_id"] == spans["http.request"]["span_id"]
    assert spans["auth.verify"]["trace_id"] == spans["http.request"]["trace_id"]


async def test_the_request_span_is_closed_when_the_handler_raises(
    client: httpx.AsyncClient, clean_metrics: None, captured_logs: list[dict[str, Any]]
) -> None:
    """An exception must not skip the span teardown, or traces never complete."""
    with pytest.MonkeyPatch.context() as mp:
        from kai.api import health as health_module

        async def _boom() -> None:
            raise RuntimeError("injected")

        mp.setattr(health_module, "check_database_reachable", _boom)
        with pytest.raises(RuntimeError):
            await client.get("/api/v1/health")

    request_spans = [s for s in _spans(captured_logs) if s["span"] == "http.request"]
    assert len(request_spans) == 1
    assert request_spans[0]["status"] == "error"
    assert request_spans[0]["error_type"] == "RuntimeError"
    assert "injected" not in json.dumps(request_spans)


async def test_concurrent_requests_do_not_share_a_trace(
    client: httpx.AsyncClient, clean_metrics: None, captured_logs: list[dict[str, Any]]
) -> None:
    """A contextvar, not a global: interleaved requests must stay separate."""
    await asyncio.gather(*(client.get("/api/v1/health") for _ in range(8)))

    request_spans = [s for s in _spans(captured_logs) if s["span"] == "http.request"]
    assert len(request_spans) == 8
    assert len({s["trace_id"] for s in request_spans}) == 8


async def test_a_span_records_error_type_and_never_the_message(
    captured_logs: list[dict[str, Any]],
) -> None:
    with pytest.raises(ValueError), span("demo.op"):
        raise ValueError("secret-value-must-not-be-logged")

    event = _spans(captured_logs)[-1]
    assert event["status"] == "error"
    assert event["error_type"] == "ValueError"
    assert "secret-value-must-not-be-logged" not in json.dumps(captured_logs)


def test_spans_nest_under_the_active_parent() -> None:
    root = SpanContext(trace_id=new_trace_id(), span_id=new_span_id())
    with span("outer", context=root) as outer, span("inner") as inner:
        assert inner.parent_span_id == outer.span_id
        assert inner.trace_id == outer.trace_id == root.trace_id
        assert current_span() is not None
    assert format_traceparent(root) == f"00-{root.trace_id}-{root.span_id}-01"


def test_traceparent_is_parsed_when_well_formed() -> None:
    trace_id, parent = "a" * 32, "b" * 16
    context = parse_traceparent(f"00-{trace_id}-{parent}-01")

    assert context is not None
    assert context.trace_id == trace_id
    assert context.span_id == parent


@pytest.mark.parametrize(
    ("header", "why"),
    [
        (None, "absent"),
        ("", "empty"),
        ("not-a-traceparent", "not the specified shape"),
        (f"00-{'a' * 31}-{'b' * 16}-01", "trace id too short"),
        (f"00-{'0' * 32}-{'b' * 16}-01", "all-zero trace id is invalid"),
        (f"00-{'a' * 32}-{'0' * 16}-01", "all-zero span id is invalid"),
        (f"00-{'a' * 32}-{'b' * 15}-01", "span id too short"),
        (f"00-{'a' * 32}-{'b' * 16}-01-extra", "unexpected trailing field"),
        (f"zz-{'a' * 32}-{'b' * 16}-01", "version is not hex"),
        (f"0-{'a' * 32}-{'b' * 16}-01", "version is one character"),
        (f"00-{'A' * 32}-{'b' * 16}-01", "trace id must be lowercase"),
        (f"00-{'a' * 32}-{'B' * 16}-01", "span id must be lowercase"),
    ],
)
def test_a_malformed_traceparent_is_refused(header: str | None, why: str) -> None:
    """A client must not be able to name the trace its own failure is filed under."""
    assert parse_traceparent(header) is None, why


async def test_an_inbound_trace_is_ignored_unless_configured(
    client: httpx.AsyncClient, clean_metrics: None, captured_logs: list[dict[str, Any]]
) -> None:
    trace_id = "c" * 32
    await client.get("/api/v1/health", headers={"traceparent": f"00-{trace_id}-{'d' * 16}-01"})

    request_spans = [s for s in _spans(captured_logs) if s["span"] == "http.request"]
    assert request_spans[-1]["trace_id"] != trace_id, "an unconfigured inbound trace was adopted"


async def test_an_inbound_trace_is_continued_when_configured(
    client: httpx.AsyncClient, clean_metrics: None, captured_logs: list[dict[str, Any]]
) -> None:
    trace_id, parent = "e" * 32, "f" * 16
    with _env(TRACING_ACCEPT_INBOUND="true"):
        response = await client.get(
            "/api/v1/health", headers={"traceparent": f"00-{trace_id}-{parent}-01"}
        )

    request_spans = [s for s in _spans(captured_logs) if s["span"] == "http.request"]
    assert request_spans[-1]["trace_id"] == trace_id
    assert request_spans[-1]["parent_span_id"] == parent
    assert response.headers["X-Trace-ID"] == trace_id


async def test_a_malformed_inbound_trace_yields_a_fresh_trace(
    client: httpx.AsyncClient, clean_metrics: None, captured_logs: list[dict[str, Any]]
) -> None:
    with _env(TRACING_ACCEPT_INBOUND="true"):
        await client.get(
            "/api/v1/health", headers={"traceparent": f"00-{'0' * 32}-{'f' * 16}-01"}
        )

    request_spans = [s for s in _spans(captured_logs) if s["span"] == "http.request"]
    assert request_spans[-1]["trace_id"] != "0" * 32
    assert request_spans[-1]["parent_span_id"] is None


async def test_an_unsampled_request_is_still_logged_and_counted(
    client: httpx.AsyncClient, clean_metrics: None, captured_logs: list[dict[str, Any]]
) -> None:
    """Sampling may drop a span; it must never drop the request itself."""
    with _env(TRACING_SAMPLE_RATIO="0.0"):
        response = await client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.headers.get("X-Request-ID")
    assert [s for s in _spans(captured_logs) if s["span"] == "http.request"] == []
    assert len(_events(captured_logs, "http_request")) == 1
    assert _sample("kai_http_requests_total", route="/api/v1/health") == 1.0


async def test_disabling_tracing_keeps_correlation_and_metrics(
    client: httpx.AsyncClient, clean_metrics: None, captured_logs: list[dict[str, Any]]
) -> None:
    with _env(TRACING_ENABLED="false"):
        response = await client.get("/api/v1/health")

    assert response.status_code == 200
    assert _spans(captured_logs) == []
    assert "X-Trace-ID" not in response.headers
    assert response.headers.get("X-Request-ID")


# ---------------------------------------------------------------------------
# Metrics export
# ---------------------------------------------------------------------------


async def test_metrics_are_served_only_when_enabled(clean_metrics: None) -> None:
    """Opt-in, because the endpoint is unauthenticated: Prometheus cannot
    present a KAI token, so serving it is an exposure decision."""
    from kai.main import create_app

    with _env(METRICS_ENABLED="false"):
        app = create_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as raw:
            assert (await raw.get("/api/v1/metrics")).status_code == 404

    with _env(METRICS_ENABLED="true"):
        app = create_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as raw:
            await raw.get("/api/v1/auth/me")
            response = await raw.get("/api/v1/metrics")

    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    body = response.text
    for expected in (
        "kai_http_requests_total",
        "kai_http_request_duration_seconds",
        "kai_http_5xx_total",
        "kai_auth_failures_total",
        "kai_db_errors_total",
    ):
        assert expected in body, f"{expected} is missing from the exposition"


async def test_a_custom_metrics_path_is_served(clean_metrics: None) -> None:
    from kai.main import create_app

    with _env(METRICS_ENABLED="true", METRICS_PATH="/internal/metrics"):
        app = create_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as raw:
            assert (await raw.get("/internal/metrics")).status_code == 200
            assert (await raw.get("/api/v1/metrics")).status_code == 404


async def test_no_user_identifier_reaches_the_exposition(
    client: httpx.AsyncClient, clean_metrics: None
) -> None:
    created = await client.post(
        "/api/v1/auth/register",
        json={"email": "private@acme.example", "password": PASSWORD},
    )
    assert created.status_code == 201, created.text
    token = created.json()["access_token"]
    user_id = created.json()["user_id"]
    headers = {"Authorization": f"Bearer {token}"}

    await client.get("/api/v1/auth/me", headers=headers)
    await client.post("/api/v1/auth/logout", headers=headers)
    await client.get("/api/v1/auth/me", headers=headers)

    body = _exposition()
    for forbidden in ("private@acme.example", token, user_id, "eyJ", "Bearer", "password"):
        assert forbidden not in body, f"{forbidden!r} leaked into the exposition"


# ---------------------------------------------------------------------------
# Cardinality: the guarantees that keep this affordable and safe
# ---------------------------------------------------------------------------


def test_no_high_cardinality_label_is_used() -> None:
    """Structural check on the label names themselves.

    A metric can be correct and still be unusable if its labels are unbounded,
    so the forbidden names are asserted absent from the collector set by
    construction rather than inferred from one sample.
    """
    forbidden = {
        "email",
        "user_id",
        "user",
        "token",
        "jti",
        "request_id",
        "task_id",
        "tenant_id",
        "path",
        "url",
        "trace_id",
        "span_id",
        "client_ip",
        "client",
    }
    for collector in _collectors():
        labels = set(collector._labelnames)  # noqa: SLF001
        assert not (labels & forbidden), f"{collector._name} has labels {labels & forbidden}"


def _collectors() -> list[Any]:
    """Every metric object declared on the holder, labels or not."""
    holder = metrics.get_metrics()
    return [value for value in vars(holder).values() if hasattr(value, "_labelnames")]


def test_the_exposed_metric_set_is_a_declared_and_bounded_one() -> None:
    """No metric may reach production without appearing in this list."""
    declared = {
        "kai_http_requests_total",
        "kai_http_request_duration_seconds",
        "kai_http_5xx_total",
        "kai_auth_failures_total",
        "kai_auth_rate_limit_rejections_total",
        "kai_tasks_started_total",
        "kai_tasks_completed_total",
        "kai_tasks_failed_total",
        "kai_provider_errors_total",
        "kai_provider_latency_seconds",
        "kai_db_errors_total",
        "kai_revocation_purge_runs_total",
        "kai_revocation_purged_rows_total",
        "kai_process_up",
    }
    exposed = {
        line.split("{")[0].split(" ")[0]
        for line in _exposition().splitlines()
        if line.startswith("kai_")
    }
    # prometheus_client emits an automatic ``<counter>_created`` series per
    # counter, named from the counter's stem with the ``_total`` suffix dropped.
    # That is library bookkeeping rather than a KAI metric, so it is not part of
    # the declared surface - but it must correspond to a declared counter.
    counters = {name for name in declared if name.endswith("_total")}
    expected_created = {name.removesuffix("_total") + "_created" for name in counters}
    created = {name for name in exposed if name.endswith("_created")}
    exposed -= created
    assert created <= expected_created, f"a _created series with no counter: {created}"
    assert exposed <= declared, f"undeclared metrics exposed: {exposed - declared}"


def test_the_reason_label_comes_from_a_closed_set() -> None:
    """Unbounded reason strings are how cardinality incidents actually start."""
    from kai.observability.metrics import AUTH_FAILURE_REASONS

    assert set(AUTH_FAILURE_REASONS) == {
        "missing",
        "invalid",
        "revoked",
        "inactive",
        "bad_credentials",
    }


# ---------------------------------------------------------------------------
# Telemetry must never become an application outage
# ---------------------------------------------------------------------------


async def test_a_broken_metric_loses_the_sample_not_the_request(
    client: httpx.AsyncClient, clean_metrics: None
) -> None:
    from unittest.mock import patch

    with patch.object(
        metrics.get_metrics().http_requests_total,
        "labels",
        side_effect=RuntimeError("instrumentation bug"),
    ):
        response = await client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.headers.get("X-Request-ID")


async def test_a_broken_span_emitter_loses_the_span_not_the_request(
    client: httpx.AsyncClient, clean_metrics: None
) -> None:
    from unittest.mock import patch

    with patch(
        "kai.observability.tracing.end_span", side_effect=RuntimeError("emitter bug")
    ):
        response = await client.get("/api/v1/health")

    assert response.status_code == 200


async def test_a_broken_metric_recorder_cannot_break_a_request(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The production invariant: telemetry is best-effort, always.

    A registry defect, an unreachable collector or a label bug must cost the
    metrics, not the user's request. If this test ever needs the assertion
    relaxed, the telemetry has been promoted to a dependency without a decision
    having been made.
    """
    from unittest.mock import patch

    from kai.main import create_app

    registry = metrics.get_metrics()
    app = create_app()
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=True)
    with patch.object(
        registry.http_requests_total, "labels", side_effect=RuntimeError("registry")
    ), patch.object(
        registry.http_request_duration_seconds, "labels", side_effect=RuntimeError("registry")
    ):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as raw:
            response = await raw.get("/api/v1/health")

    assert response.status_code == 200, response.text
    # The loss is reported rather than silent: a permanently broken registry
    # should be visible in the logs, not just absent from the metrics. Asserted on
    # stdout because that is where a deployment's logs actually go, and
    # ``create_app`` re-points logging at stdout.
    emitted = capsys.readouterr().out
    assert "metrics_recording_failed" in emitted, emitted
    assert "kai_http_requests_total" in emitted
    # The exception type is logged; the message is not, because it is not ours.
    assert "RuntimeError" in emitted


async def test_a_broken_span_emitter_cannot_break_a_request() -> None:
    """Same invariant for tracing: an exporter that raises must not become an
    application outage.

    ``tracing.end_span`` already swallows its own failures, so this patches it to
    raise anyway. That is deliberate: it exercises the outer guard, which is the
    only thing standing between a broken telemetry dependency and a 500.
    """
    from unittest.mock import patch

    from kai.main import create_app
    from kai.observability import tracing

    app = create_app()
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=True)
    with patch.object(tracing, "end_span", side_effect=RuntimeError("exporter is down")):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as raw:
            response = await raw.get("/api/v1/health")

    assert response.status_code == 200, response.text


async def test_a_broken_downstream_span_still_lets_the_request_finish() -> None:
    """A nested span (auth, task, provider) that raises to record itself must
    not take the handler's result with it."""
    from unittest.mock import patch

    from kai.main import create_app
    from kai.observability import tracing

    app = create_app()
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=True)
    with patch.object(tracing, "end_span", side_effect=RuntimeError("exporter")):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as raw:
            response = await raw.post(
                "/api/v1/auth/register",
                json={"email": "span-break@acme.example", "password": "Str0ng-Passw0rd!"},
            )

    assert response.status_code == 201, response.text


async def test_a_broken_renderer_only_breaks_the_metrics_route(
    clean_metrics: None,
) -> None:
    """There is no push exporter, so the only thing telemetry can take down is
    the endpoint itself."""
    from unittest.mock import patch

    from kai.main import create_app

    with _env(METRICS_ENABLED="true"):
        app = create_app()
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        with patch.object(metrics, "render_latest", side_effect=RuntimeError("renderer")):
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as raw:
                broken = await raw.get("/api/v1/metrics")
                healthy = await raw.get("/api/v1/health")

    assert broken.status_code == 500
    assert healthy.status_code == 200


async def test_the_limiter_still_fails_closed_on_a_backend_outage(
    client: httpx.AsyncClient, clean_metrics: None
) -> None:
    """The security round's fail-closed decision must survive this round."""
    from unittest.mock import patch

    with patch.object(
        ratelimit._get_store(),  # noqa: SLF001
        "hit",
        side_effect=ConnectionError("redis is down"),
    ):
        response = await client.post(
            "/api/v1/auth/login",
            json={"email": "x@acme.example", "password": "wrong"},
        )

    assert response.status_code == 429
    assert (
        _sample("kai_auth_rate_limit_rejections_total", reason="backend_unavailable") == 1.0
    )


async def test_a_user_lookup_still_reports_the_same_opaque_401(
    client: httpx.AsyncClient, clean_metrics: None
) -> None:
    """Instrumentation must not widen what a rejected caller can learn."""
    headers = await _auth(client, "opaque@acme.example")
    alive = await client.get("/api/v1/auth/me", headers=headers)
    await client.post("/api/v1/auth/logout", headers=headers)
    revoked = await client.get("/api/v1/auth/me", headers=headers)
    forged = await client.get("/api/v1/auth/me", headers={"Authorization": "Bearer x.y.z"})

    assert alive.status_code == 200
    assert revoked.json() == forged.json() == {"detail": "token is invalid or expired"}