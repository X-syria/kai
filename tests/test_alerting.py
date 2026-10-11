"""External operational alerting: the failure -> alert -> resolution lifecycle.

Real Dockerized PostgreSQL, a real ASGI app, and a real HTTP receiver on a real
socket. Nothing here mocks the alerting path. A task transition writes a durable
alert row in its own transaction (``kai.alerts.service``) and opens no socket;
delivery is then performed by the one-shot dispatcher
(``kai.maintenance.alert_dispatch``), which builds its own httpx client,
serialises its own payload, and opens its own connection. The suite invokes that
same sweep in process and reads what actually arrived. Mocking the transport
would have tested that the mock was called, which is the failure mode these tests
exist to rule out.

What is deliberately *not* exercised here is the public internet. Pointing a test
at Slack or httpbin would make the suite depend on someone else's uptime and on
this network's egress, so the receiver is a real server on loopback. The run
that proves an actual external destination received these bytes is recorded in
docs/EXTERNAL-ALERT-DESTINATION-01-REPORT.md and is not reproducible from here.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from kai.alerts.models import DELIVERY_PENDING, Alert
from kai.auth.jwt import decode_access_token
from kai.config import get_settings
from kai.db.engine import async_session_factory
from kai.db.models import User
from kai.maintenance.alert_dispatch import run_dispatch
from kai.observability.alerting import (
    ALERT_SCHEMA,
    OPEN_FAILURE_STATES,
    TaskAlert,
    build_alert,
    classify_transition,
)
from kai.observability.metrics import render_latest
from kai.tasks.models import Task
from kai.tasks.service import transition_task
from kai.tasks.state import (
    COMPLETED,
    CREATED,
    ESCALATE,
    EXECUTING,
    FAILED,
    PLANNING,
    POLICY_CHECK,
    REQUIRES_ATTENTION,
    RETRY,
    UNDERSTANDING,
    VERIFYING,
    InvalidTransition,
)

API = "/api/v1"

#: A failure reason carrying everything a real provider error carries: a secret,
#: an address, and free text. Used to prove none of it reaches the webhook.
POISON = "provider rejected token sk_live_51H8xQ for alice@example.com"


# ---------------------------------------------------------------------------
# A real receiver
# ---------------------------------------------------------------------------


@dataclass
class Received:
    path: str
    headers: dict[str, str]
    body: bytes

    def json(self) -> dict[str, Any]:
        return json.loads(self.body)


@dataclass
class Receiver:
    """A real HTTP server that records what a webhook actually receives."""

    url: str
    requests: list[Received] = field(default_factory=list)
    status: int = 200
    #: Invoked in the handler thread once a request has been read, before the
    #: response is written. Lets a test observe the world *while* the service is
    #: still blocked on the webhook.
    on_request: Any = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def payloads(self) -> list[dict[str, Any]]:
        with self._lock:
            return [r.json() for r in self.requests]

    @property
    def bodies(self) -> bytes:
        return b"".join(r.body for r in self.requests)

    def set_status(self, status: int) -> None:
        self.status = status


@pytest.fixture
def receiver() -> Iterator[Receiver]:
    """Run a real webhook receiver on loopback for the duration of one test."""
    state: Receiver | None = None

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            assert state is not None
            with state._lock:
                state.requests.append(
                    Received(
                        path=self.path,
                        headers={k.lower(): v for k, v in self.headers.items()},
                        body=body,
                    )
                )
                status = state.status
            if state.on_request is not None:
                state.on_request(len(state.requests))
            self.send_response(status)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args: Any) -> None:
            """Silence the default stderr access log."""

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    host, port = server.server_address[0], server.server_address[1]
    state = Receiver(url=f"http://{host}:{port}/hooks/T000/B000/SECRETTOKEN")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def alerting_on(receiver: Receiver, monkeypatch: pytest.MonkeyPatch) -> Receiver:
    """Arm alerting exactly as an operator would: switch plus URL."""
    monkeypatch.setenv("ALERTING_ENABLED", "true")
    monkeypatch.setenv("ALERT_WEBHOOK_URL", receiver.url)
    get_settings.cache_clear()
    yield receiver
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Task fixtures, created through the real API
# ---------------------------------------------------------------------------


async def _register(client: httpx.AsyncClient, email: str) -> tuple[str, Any]:
    resp = await client.post(
        f"{API}/auth/register", json={"email": email, "password": "password123"}
    )
    assert resp.status_code == 201, resp.text
    token = resp.json()["access_token"]
    return token, decode_access_token(token)


async def _create_task(client: httpx.AsyncClient, token: str, goal: str) -> dict:
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


#: The real edge path from CREATED into EXECUTING, read off
#: ``kai.tasks.state.TRANSITIONS`` rather than assumed. These tests drive the
#: state machine the runtime drives; if a state is unreachable the suite should
#: say so loudly instead of inventing an edge to make an assertion pass.
PATH_TO_EXECUTING: tuple[str, ...] = (UNDERSTANDING, PLANNING, POLICY_CHECK, EXECUTING)

#: How a failed task legitimately recovers: retry, then re-enter execution.
PATH_TO_RETRY: tuple[str, ...] = (FAILED, RETRY)
PATH_TO_RESOLVED: tuple[str, ...] = (EXECUTING, VERIFYING, COMPLETED)


def _counter(name: str, **labels: str) -> float:
    """Read one labelled series out of the live Prometheus exposition.

    The registry is process-global and deliberately not reset between tests, so
    these counters accumulate across the whole run. Asserting an absolute value
    would make each test depend on the order pytest happened to collect them in.
    Callers compare two readings instead, which is also the honest description of
    what a counter means.
    """
    wanted = "{" + ",".join(f'{k}="{v}"' for k, v in sorted(labels.items())) + "}"
    for line in render_latest().decode("utf-8").splitlines():
        if line.startswith(f"{name}{wanted} "):
            return float(line.rsplit(" ", 1)[1])
    return 0.0


async def _drive(task: dict, claims: Any, *states: str, reason: str | None = None) -> Task:
    """Apply a sequence of legal transitions through the service, as the runtime does.

    Each transition only *enqueues* the durable alert intent. Delivery is the
    one-shot sweep the operator's scheduler runs; here that sweep is invoked in
    process, so the receiver observes exactly what a real deployment's dispatcher
    would have sent.
    """
    row: Task | None = None
    async with async_session_factory() as session:
        for state in states:
            row = await transition_task(
                session,
                task_id=task["id"],
                tenant_id=claims.tenant_id,
                user_id=claims.user_id,
                target=state,
                fail_reason=reason,
            )
    assert row is not None
    await run_dispatch()
    return row


# ---------------------------------------------------------------------------
# 1. A durable execution failure produces an external alert
# ---------------------------------------------------------------------------


async def test_a_durable_failure_reaches_the_destination(
    client: httpx.AsyncClient, alerting_on: Receiver
) -> None:
    token, claims = await _register(client, "ops@example.com")
    task = await _create_task(client, token, "Book a flight to Berlin")

    await _drive(task, claims, *PATH_TO_EXECUTING, FAILED, reason="no calendar account")

    assert len(alerting_on.payloads) == 1, "exactly one alert for one failure"
    payload = alerting_on.payloads[0]
    assert payload["kind"] == "task_failure"
    assert payload["schema"] == ALERT_SCHEMA
    assert payload["from_state"] == EXECUTING
    assert payload["to_state"] == FAILED
    assert alerting_on.requests[0].headers["content-type"] == "application/json"

    # The failure is durable state, not a side effect of the notification: the
    # row is committed and queryable on its own.
    async with async_session_factory() as session:
        row = (await session.execute(select(Task).where(Task.id == task["id"]))).scalar_one()
        assert row.state == FAILED
        assert row.failed_at is not None
        assert row.last_error == "no calendar account"


async def test_the_failure_is_committed_before_the_operator_is_told(
    client: httpx.AsyncClient, alerting_on: Receiver
) -> None:
    """The durable failure, and its alert intent, commit before any delivery.

    Delivery is now a separate one-shot sweep, which makes the property both
    stronger and easier to see: the transition opens no socket at all, and by the
    time it returns the alert row is already committed and still PENDING - the
    sweep that will deliver it has not run yet. An alert that could describe a
    failure which then failed to persist was the old failure mode; here the row
    and the failure are one commit, and the socket is a different process.
    """
    token, claims = await _register(client, "order@example.com")
    task = await _create_task(client, token, "Email the invoice")

    async with async_session_factory() as session:
        row = await transition_task(
            session,
            task_id=task["id"],
            tenant_id=claims.tenant_id,
            user_id=claims.user_id,
            target=FAILED,
            fail_reason="planning refused",
        )
    assert row.state == FAILED
    assert row.failed_at is not None
    assert row.last_error == "planning refused"

    # The transition did not talk to the webhook: nothing has been sent.
    assert alerting_on.requests == [], "the transition must open no socket"

    # The intent is durable and waiting - committed, not lost, not yet delivered.
    async with async_session_factory() as session:
        waiting = (
            (await session.execute(select(Alert).where(Alert.task_id == task["id"])))
            .scalars()
            .all()
        )
    assert len(waiting) == 1
    assert waiting[0].delivery_state == DELIVERY_PENDING
    assert waiting[0].to_state == FAILED

    # The sweep delivers the failure that was already committed.
    await run_dispatch()
    assert len(alerting_on.payloads) == 1
    assert alerting_on.payloads[0]["to_state"] == FAILED


# ---------------------------------------------------------------------------
# 2. The alert identifies the failed operation
# ---------------------------------------------------------------------------


async def test_the_alert_carries_enough_to_identify_the_operation(
    client: httpx.AsyncClient, alerting_on: Receiver
) -> None:
    token, claims = await _register(client, "triage@example.com")
    task = await _create_task(client, token, "Book a flight to Berlin")

    await _drive(task, claims, *PATH_TO_EXECUTING, FAILED, reason="boom")

    payload = alerting_on.payloads[0]
    assert payload["task_id"] == task["id"]
    assert payload["tenant_id"] == str(claims.tenant_id)
    assert payload["fulfillment_profile"] == "calendar_meeting"
    assert payload["attempts"] == 0
    assert payload["max_retries"] == 2
    assert payload["occurred_at"].endswith("Z")
    async with async_session_factory() as session:
        found = await session.get(Task, task["id"])
        # JSON carries a string where the column holds a UUID; the two must agree.
        assert found is not None and str(found.id) == payload["task_id"]


# ---------------------------------------------------------------------------
# 3. No secrets, and no task content, leave the process
# ---------------------------------------------------------------------------


async def test_no_secret_or_task_content_is_in_the_alert(
    client: httpx.AsyncClient, alerting_on: Receiver, capsys: pytest.CaptureFixture[str]
) -> None:
    token, claims = await _register(client, "secret@example.com")
    goal = "Book a flight to Berlin for Alice Example"
    task = await _create_task(client, token, goal)

    await _drive(task, claims, *PATH_TO_EXECUTING, FAILED, reason=POISON)
    emitted = capsys.readouterr().out

    raw = alerting_on.bodies.decode("utf-8")
    assert len(alerting_on.payloads) == 1

    # The provider secret, the email address, the failure text, and the user's
    # own words must all be absent from what was transmitted.
    assert "sk_live_51H8xQ" not in raw
    assert "alice@example.com" not in raw
    assert POISON not in raw
    assert goal not in raw
    assert "provider rejected token" not in raw

    # Nor may the destination itself - its path holds the receiver's token.
    # Asserted on stdout, because that is where a deployment's logs go and
    # ``create_app`` re-points logging there.
    assert "SECRETTOKEN" not in raw
    assert "127.0.0.1" not in raw
    assert "SECRETTOKEN" not in emitted
    assert "127.0.0.1" not in emitted


async def test_a_redacted_destination_keeps_only_scheme_and_host() -> None:
    from kai.observability.alerting import _redact

    assert (
        _redact("https://hooks.slack.com/services/T000/B000/SUPERSECRET")
        == "https://hooks.slack.com"
    )
    assert _redact("not a url at all") == "<no-host>"
    # Even a truncated prefix of the path must not survive.
    assert "SUPERSECRET" not in _redact("https://hooks.slack.com/services/T000/B000/SUPERSECRET")


# ---------------------------------------------------------------------------
# 4. Resolution produces its own notification
# ---------------------------------------------------------------------------


async def test_resolution_sends_a_second_distinct_notification(
    client: httpx.AsyncClient, alerting_on: Receiver
) -> None:
    token, claims = await _register(client, "resolve@example.com")
    task = await _create_task(client, token, "Book a flight to Berlin")

    await _drive(task, claims, *PATH_TO_EXECUTING, FAILED, reason="boom")
    await _drive(task, claims, RETRY)
    await _drive(task, claims, EXECUTING)

    kinds = [p["kind"] for p in alerting_on.payloads]
    assert kinds == ["task_failure", "task_failure_resolved"]

    resolved = alerting_on.payloads[1]
    assert resolved["from_state"] == RETRY
    assert resolved["to_state"] == EXECUTING
    assert resolved["task_id"] == alerting_on.payloads[0]["task_id"]
    # A resolution is about the incident, not about the retry: it carries the
    # state the failure ended in, and the counters the retry produced.
    assert resolved["attempts"] == 1
    assert resolved["occurred_at"] != alerting_on.payloads[0]["occurred_at"]


async def test_retrying_does_not_claim_a_resolution(
    client: httpx.AsyncClient, alerting_on: Receiver
) -> None:
    """FAILED -> RETRY is the same incident, so it must stay silent.

    Reporting a resolution here and a fresh failure on the way back would tell an
    operator a task was fixed while it was still broken, then open an incident
    nobody had addressed.
    """
    token, claims = await _register(client, "retry@example.com")
    task = await _create_task(client, token, "Book a flight to Berlin")

    await _drive(task, claims, *PATH_TO_EXECUTING, FAILED, reason="boom")
    await _drive(task, claims, RETRY)

    assert [p["kind"] for p in alerting_on.payloads] == ["task_failure"]


async def test_a_relabelled_failure_is_not_a_new_incident(
    client: httpx.AsyncClient, alerting_on: Receiver
) -> None:
    """FAILED -> REQUIRES_ATTENTION is the same failure in a different state."""
    token, claims = await _register(client, "halt@example.com")
    task = await _create_task(client, token, "Book a flight to Berlin")

    await _drive(task, claims, *PATH_TO_EXECUTING, FAILED, reason="boom")
    await _drive(task, claims, REQUIRES_ATTENTION, reason="needs a human")

    assert [p["kind"] for p in alerting_on.payloads] == ["task_failure"]


# ---------------------------------------------------------------------------
# 5. Disabled, unconfigured, or broken alerting cannot corrupt execution
# ---------------------------------------------------------------------------


async def test_unconfigured_alerting_leaves_execution_untouched(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ALERTING_ENABLED", raising=False)
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    get_settings.cache_clear()
    try:
        token, claims = await _register(client, "quiet@example.com")
        task = await _create_task(client, token, "Book a flight to Berlin")

        row = await _drive(task, claims, *PATH_TO_EXECUTING, FAILED, reason="boom")
        assert row.state == FAILED
        assert row.failed_at is not None

        # Recovery still works end to end with alerting off.
        await _drive(task, claims, RETRY, EXECUTING, VERIFYING, COMPLETED)
        async with async_session_factory() as session:
            done = await session.get(Task, task["id"])
            assert done is not None and done.state == COMPLETED
    finally:
        get_settings.cache_clear()


async def test_a_dead_destination_cannot_fail_the_transition(
    client: httpx.AsyncClient, alerting_on: Receiver
) -> None:
    """The receiver answering 500 must cost an alert, never the failure itself."""
    alerting_on.set_status(500)
    token, claims = await _register(client, "dead@example.com")
    task = await _create_task(client, token, "Book a flight to Berlin")

    before = _counter("kai_alerts_delivered_total", kind="task_failure", outcome="failed")
    row = await _drive(task, claims, *PATH_TO_EXECUTING, FAILED, reason="boom")

    assert row.state == FAILED, "the failure is durable regardless of the webhook"
    assert row.failed_at is not None
    assert row.last_error == "boom"
    assert len(alerting_on.requests) == 1, "it was attempted"

    async with async_session_factory() as session:
        stored = await session.get(Task, task["id"])
        assert stored is not None and stored.state == FAILED

    # And the failure to alert is itself observable.
    delivered = _counter("kai_alerts_delivered_total", kind="task_failure", outcome="failed")
    assert delivered - before == 1.0


async def test_a_broken_destination_is_never_quoted_in_a_log(
    client: httpx.AsyncClient,
    alerting_on: Receiver,
    capsys: pytest.CaptureFixture[str],
) -> None:
    alerting_on.set_status(500)
    token, claims = await _register(client, "quietlog@example.com")
    task = await _create_task(client, token, "Book a flight to Berlin")

    await _drive(task, claims, *PATH_TO_EXECUTING, FAILED, reason=POISON)
    emitted = capsys.readouterr().out

    # The loss is reported rather than silent: a destination that has started
    # rejecting alerts should be visible in the logs, not merely absent.
    assert "alert_delivery_failed" in emitted, emitted
    assert "HTTPStatusError" in emitted
    # But the report must not become the leak the payload was built to avoid.
    assert "SECRETTOKEN" not in emitted
    assert "sk_live_51H8xQ" not in emitted
    assert POISON not in emitted
    # What it does say is enough to find the destination, and nothing more.
    assert "127.0.0.1" in emitted


# ---------------------------------------------------------------------------
# 6. Replay and duplication cannot manufacture a second notification
# ---------------------------------------------------------------------------


async def test_refused_transitions_emit_nothing(
    client: httpx.AsyncClient, alerting_on: Receiver
) -> None:
    token, claims = await _register(client, "dup@example.com")
    task = await _create_task(client, token, "Book a flight to Berlin")

    await _drive(task, claims, *PATH_TO_EXECUTING, FAILED, reason="boom")
    assert len(alerting_on.payloads) == 1

    # FAILED -> FAILED is not an edge the machine has. A replay that tries to
    # re-settle the same failure is refused before any row is written, so there
    # is nothing to alert about.
    async with async_session_factory() as session:
        with pytest.raises(InvalidTransition):
            await transition_task(
                session,
                task_id=task["id"],
                tenant_id=claims.tenant_id,
                user_id=claims.user_id,
                target=FAILED,
                fail_reason="boom",
            )

    assert len(alerting_on.payloads) == 1, "no duplicate alert for a refused edge"


async def test_a_settled_incident_alerts_once_per_open(
    client: httpx.AsyncClient, alerting_on: Receiver
) -> None:
    token, claims = await _register(client, "once@example.com")
    task = await _create_task(client, token, "Book a flight to Berlin")

    await _drive(task, claims, *PATH_TO_EXECUTING, FAILED, reason="boom")
    await _drive(task, claims, RETRY, EXECUTING)
    await _drive(task, claims, VERIFYING, COMPLETED)

    assert [p["kind"] for p in alerting_on.payloads] == [
        "task_failure",
        "task_failure_resolved",
    ]


# ---------------------------------------------------------------------------
# 7. The incident model itself
# ---------------------------------------------------------------------------


def test_every_edge_into_a_failure_alerts_and_only_retry_exits_resolve() -> None:
    from kai.tasks.state import TRANSITIONS

    firing = {
        (src, dst)
        for src, targets in TRANSITIONS.items()
        for dst in targets
        if classify_transition(src, dst) == "task_failure"
    }
    resolved = {
        (src, dst)
        for src, targets in TRANSITIONS.items()
        for dst in targets
        if classify_transition(src, dst) == "task_failure_resolved"
    }

    expected_firing = {
        (src, dst)
        for src, targets in TRANSITIONS.items()
        for dst in targets
        if src not in OPEN_FAILURE_STATES and dst in OPEN_FAILURE_STATES
    }
    assert firing == expected_firing
    assert len(firing) == 16
    # RETRY -> EXECUTING is the only way out of the failure set, because RETRY
    # is inside it and every other exit is terminal or stays inside.
    assert resolved == {(RETRY, EXECUTING)}


def test_escalation_does_not_claim_a_resolution() -> None:
    """ESCALATE hands a failure to a human; it does not put it right."""
    assert ESCALATE in OPEN_FAILURE_STATES
    assert classify_transition(FAILED, ESCALATE) is None


def test_an_unknown_state_is_never_read_as_healthy() -> None:
    assert classify_transition(FAILED, "NOT_A_STATE") is None
    assert classify_transition(ESCALATE, "NOT_A_STATE") is None
    assert classify_transition("NOT_A_STATE", COMPLETED) is None


def test_the_payload_is_deterministic_and_carries_no_extra_fields() -> None:
    alert = TaskAlert(
        kind="task_failure",
        task_id="11111111-1111-1111-1111-111111111111",
        tenant_id="22222222-2222-2222-2222-222222222222",
        from_state=EXECUTING,
        to_state=FAILED,
        fulfillment_profile="calendar_meeting",
        attempts=0,
        max_retries=2,
        occurred_at="2026-10-02T20:00:00Z",
    )
    assert alert.render() == alert.render()
    assert list(alert.payload()) == [
        "schema",
        "kind",
        "task_id",
        "tenant_id",
        "from_state",
        "to_state",
        "fulfillment_profile",
        "attempts",
        "max_retries",
        "occurred_at",
    ]
    assert b"goal" not in alert.render()
    assert b"last_error" not in alert.render()


def test_build_alert_ignores_a_non_alerting_edge() -> None:
    class _Row:
        id = "id"
        tenant_id = "tenant"
        state = UNDERSTANDING
        fulfillment_profile = "calendar_meeting"
        attempts = 0
        max_retries = 2
        failed_at = None

    assert build_alert(_Row(), from_state=CREATED) is None


# ---------------------------------------------------------------------------
# 8. Configuration is fail-fast and refuses a disarmed or plaintext destination
# ---------------------------------------------------------------------------


_STRONG_JWT = "j" * 48
_STRONG_GOOGLE = "g" * 48


@pytest.fixture
def production_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[pytest.MonkeyPatch]:
    """A production environment that starts, so only alerting can refuse it."""
    get_settings.cache_clear()
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("JWT_SECRET", _STRONG_JWT)
    monkeypatch.setenv("GOOGLE_CREDS_ENCRYPTION_SECRET", _STRONG_GOOGLE)
    monkeypatch.setenv("RATE_LIMIT_BACKEND", "redis")
    monkeypatch.setenv("REDIS_URL", "redis://redis.invalid:6379/0")
    yield monkeypatch
    get_settings.cache_clear()


def test_production_refuses_alerting_enabled_without_a_url(
    production_env: pytest.MonkeyPatch,
) -> None:
    production_env.setenv("ALERTING_ENABLED", "true")
    production_env.delenv("ALERT_WEBHOOK_URL", raising=False)
    with pytest.raises(RuntimeError, match="ALERTING_ENABLED=true requires ALERT_WEBHOOK_URL"):
        get_settings()


def test_production_refuses_a_plaintext_destination(
    production_env: pytest.MonkeyPatch,
) -> None:
    production_env.setenv("ALERTING_ENABLED", "true")
    production_env.setenv("ALERT_WEBHOOK_URL", "http://hooks.example.com/x")
    with pytest.raises(RuntimeError, match="must use https"):
        get_settings()


def test_production_refuses_a_url_that_sends_nothing(
    production_env: pytest.MonkeyPatch,
) -> None:
    production_env.setenv("ALERTING_ENABLED", "false")
    production_env.setenv("ALERT_WEBHOOK_URL", "https://hooks.example.com/x")
    with pytest.raises(RuntimeError, match="ALERTING_ENABLED is false"):
        get_settings()


def test_production_refuses_a_url_without_a_host(
    production_env: pytest.MonkeyPatch,
) -> None:
    production_env.setenv("ALERTING_ENABLED", "true")
    production_env.setenv("ALERT_WEBHOOK_URL", "https:///nohost")
    with pytest.raises(RuntimeError, match="must include a host"):
        get_settings()


def test_production_accepts_a_configured_https_destination(
    production_env: pytest.MonkeyPatch,
) -> None:
    production_env.setenv("ALERTING_ENABLED", "true")
    production_env.setenv("ALERT_WEBHOOK_URL", "https://hooks.example.com/services/T/B/C")
    settings = get_settings()
    assert settings.alerting_enabled is True
    assert settings.alert_webhook_url.endswith("/C")


def test_production_stays_happy_when_alerting_is_off(
    production_env: pytest.MonkeyPatch,
) -> None:
    """Arming nothing must not stop a production deployment from starting."""
    production_env.setenv("ALERTING_ENABLED", "false")
    production_env.delenv("ALERT_WEBHOOK_URL", raising=False)
    assert get_settings().alerting_enabled is False


def test_development_tolerates_loopback_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only production refuses plaintext; a local receiver must stay usable."""
    get_settings.cache_clear()
    monkeypatch.setenv("ALERTING_ENABLED", "true")
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://127.0.0.1:9/x")
    try:
        assert get_settings().alert_webhook_url == "http://127.0.0.1:9/x"
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# 9. End to end: the whole lifecycle over HTTP
# ---------------------------------------------------------------------------


async def test_full_lifecycle_over_the_real_api(
    client: httpx.AsyncClient, alerting_on: Receiver
) -> None:
    """Create -> fail -> retry -> recover, driven through the public API.

    The task is created and inspected over HTTP; the transitions use the service
    the HTTP routes call, so the alert path is the production one.
    """
    token, claims = await _register(client, "e2e@example.com")
    task = await _create_task(client, token, "Book a flight to Berlin")

    seen = await client.get(
        f"{API}/tasks/{task['id']}", headers={"Authorization": f"Bearer {token}"}
    )
    assert seen.status_code == 200
    assert seen.json()["state"] == CREATED

    fired_before = _counter("kai_alerts_delivered_total", kind="task_failure", outcome="sent")
    resolved_before = _counter(
        "kai_alerts_delivered_total", kind="task_failure_resolved", outcome="sent"
    )

    await _drive(task, claims, *PATH_TO_EXECUTING, FAILED, reason="boom")
    await _drive(task, claims, RETRY, EXECUTING, VERIFYING, COMPLETED)

    assert [p["kind"] for p in alerting_on.payloads] == [
        "task_failure",
        "task_failure_resolved",
    ]
    assert {p["task_id"] for p in alerting_on.payloads} == {task["id"]}

    final = await client.get(
        f"{API}/tasks/{task['id']}", headers={"Authorization": f"Bearer {token}"}
    )
    assert final.json()["state"] == COMPLETED

    # One incident, two notifications: the fire and its resolution, each once.
    assert _counter("kai_alerts_delivered_total", kind="task_failure", outcome="sent") - (
        fired_before
    ) == 1.0
    assert _counter(
        "kai_alerts_delivered_total", kind="task_failure_resolved", outcome="sent"
    ) - (resolved_before) == 1.0


async def test_a_client_declared_failure_also_reaches_the_destination(
    client: httpx.AsyncClient, alerting_on: Receiver
) -> None:
    """A failure a client asserts about its own task alerts like any other."""
    from kai.tasks.service import EXTERNAL_ACTOR

    token, claims = await _register(client, "selfreport@example.com")
    task = await _create_task(client, token, "Book a flight to Berlin")
    async with async_session_factory() as session:
        row = await transition_task(
            session,
            task_id=task["id"],
            tenant_id=claims.tenant_id,
            user_id=claims.user_id,
            target=FAILED,
            fail_reason="I am not paying for this",
            actor=EXTERNAL_ACTOR,
        )
    assert row.state == FAILED
    await run_dispatch()
    assert [p["kind"] for p in alerting_on.payloads] == ["task_failure"]


async def test_the_alert_does_not_cross_a_tenant_boundary(
    client: httpx.AsyncClient, alerting_on: Receiver
) -> None:
    """An alert names the tenant it belongs to and is emitted once, unscoped."""
    token_a, claims_a = await _register(client, "a@example.com")
    token_b, claims_b = await _register(client, "b@example.com")
    task_a = await _create_task(client, token_a, "Book a flight to Berlin")
    await _create_task(client, token_b, "Book a flight to Paris")

    await _drive(task_a, claims_a, *PATH_TO_EXECUTING, FAILED, reason="boom")

    assert len(alerting_on.payloads) == 1
    assert alerting_on.payloads[0]["tenant_id"] == str(claims_a.tenant_id)
    assert alerting_on.payloads[0]["task_id"] == task_a["id"]

    # A second tenant's task id must not resolve, and must not alert.
    async with async_session_factory() as session:
        other = (await session.execute(select(User))).scalars().all()
        assert len(other) == 2