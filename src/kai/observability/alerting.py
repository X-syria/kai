"""External operational alerting for durable task failures.

Everything else KAI emits is *pulled*: metrics off a private registry, spans as
structured log lines. Nothing about that can tell an operator that one specific
task failed. This module is the single push path - one JSON POST to a webhook the
operator already owns - and it fires twice per incident: once when a task's
durable state enters an unresolved failure, once when it leaves one.

Two decisions are load-bearing and are argued here rather than in a review.

**Why the seam is the state machine, not the runtime.** Every durable failure
and every durable resolution passes through
:func:`kai.tasks.service.transition_task`, which is the only writer of ``state``.
Hooking there means one call site covers the runtime's ``_fail``/``_halt``, a
client marking its own task failed, and anything added later. Hooking the runtime
would have covered two of those three and silently missed the third.

**What counts as an unresolved failure.** ``OPEN_FAILURE_STATES`` is the set of
states in which a failure exists and has not been put right. ``RETRY`` is in it:
a retry is only reachable from a failure, exists only to re-attempt it, and has
a budget spent against it - so leaving the failure set on the way to ``RETRY``
and re-entering on the way back would report a resolution that did not happen and
then re-open an incident nobody had fixed. ``ESCALATE`` is in it for the same
reason: escalation hands a failure to a human, it does not resolve it, and
``ESCALATE`` is terminal, so treating it as a resolution would tell an operator a
failed task was fixed when it was in fact waiting for them. The cost of that
choice is stated plainly: escalation emits nothing of its own, so the operator
keeps the original firing alert and is not told the task was escalated. A silent
extra is recoverable; a false "resolved" is not.

**What is deliberately not in the payload.** No task ``goal``, no
``last_error``, no provider message, no webhook URL. The alert names *which*
operation failed so it can be looked up - ``task_id``, ``tenant_id``, the states
either side of the edge, the sealed profile, and the retry counters - and carries
nothing else. ``last_error`` is provider-supplied free text that can embed an
email address, a calendar event summary, or a signed URL; the goal is whatever a
person privately asked KAI to do. An operator who needs detail opens the task.
That makes "no secrets in the alert" a structural property rather than a
redaction effort that has to be kept complete.

Alerting is never allowed to become a dependency of the operation it observes.
:func:`attempt_delivery` returns an outcome instead of raising, and the modules
that use it - :mod:`kai.alerts.service` to enqueue in the transition's own
transaction, :mod:`kai.maintenance.alert_dispatch` to deliver later - treat a
receiver that is down, slow, or misconfigured as a recorded fact rather than an
exception. The state transition itself never opens a socket.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

import httpx

#: The classification contract - which edges alert, of which kind, and the wire
#: schema - lives in :mod:`kai.alerts.classification` and is consumed here rather
#: than re-declared. This module owns the *transport*: building the payload and
#: performing the POST. Re-exported names keep the module's public surface stable
#: for the dispatcher, the tests and the docs.
from kai.alerts.classification import (
    ALERT_KIND_FAILURE,
    ALERT_KIND_RESOLUTION,
    ALERT_SCHEMA,
    OPEN_FAILURE_STATES,
    classify_transition,
)

#: Fixed field order. The payload is built from this tuple rather than a dict
#: literal at the call site so two alerts of the same kind serialise to
#: byte-identical strings, which is what makes them comparable in a receiver's
#: own logs and in a test.
_ALERT_FIELDS: Final[tuple[str, ...]] = (
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
)


@dataclass(frozen=True, slots=True)
class TaskAlert:
    """One external alert. Immutable, and built only by :func:`build_alert`.

    ``schema_version`` defaults to the current :data:`ALERT_SCHEMA`. It is a
    field rather than a constant in :meth:`payload` so a dispatcher rebuilding a
    payload from a durable row can render the schema that was in force when the
    incident was recorded, instead of relabelling an old incident as a new one.
    """

    kind: str
    task_id: str
    tenant_id: str
    from_state: str
    to_state: str
    fulfillment_profile: str
    attempts: int
    max_retries: int
    occurred_at: str
    schema_version: str = ALERT_SCHEMA

    def payload(self) -> dict[str, Any]:
        """Return the wire body with a fixed key order and no user content."""
        values: dict[str, Any] = {
            "schema": self.schema_version,
            "kind": self.kind,
            "task_id": self.task_id,
            "tenant_id": self.tenant_id,
            "from_state": self.from_state,
            "to_state": self.to_state,
            "fulfillment_profile": self.fulfillment_profile,
            "attempts": self.attempts,
            "max_retries": self.max_retries,
            "occurred_at": self.occurred_at,
        }
        return {name: values[name] for name in _ALERT_FIELDS}

    def render(self) -> bytes:
        """Serialise deterministically: same alert in, same bytes out."""
        return json.dumps(
            self.payload(), separators=(",", ":"), sort_keys=False, default=str
        ).encode("utf-8")


def build_alert(
    task: Any, *, from_state: str, occurred_at: datetime | None = None
) -> TaskAlert | None:
    """Build the alert for a settled transition, or ``None`` if it is not one.

    ``task`` is read for identifiers and counters only. The type is ``Any``
    because this transport module must not import :mod:`kai.tasks.models`: it
    reads the row structurally (``id``, ``state``, ``attempts``, ...) and must
    stay importable without the ORM.
    """
    kind = classify_transition(from_state, task.state)
    if kind is None:
        return None
    # ``failed_at`` is written by the state machine on the same commit that
    # created this failure, so it is the honest "when it broke" time. For a
    # resolution there is no such column, so the transition time is used.
    moment = task.failed_at if kind == ALERT_KIND_FAILURE else None
    moment = moment or occurred_at or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return TaskAlert(
        kind=kind,
        task_id=str(task.id),
        tenant_id=str(task.tenant_id),
        from_state=from_state,
        to_state=str(task.state),
        # Already a bounded, sealed vocabulary - it is a metrics label today.
        fulfillment_profile=str(task.fulfillment_profile),
        attempts=int(task.attempts),
        max_retries=int(task.max_retries),
        occurred_at=moment.astimezone(UTC).isoformat().replace("+00:00", "Z"),
    )


def _redact(url: str) -> str:
    """Reduce a webhook URL to scheme and host for a log line, never more.

    Every common receiver keeps its live token in the URL path, so the path and
    query are dropped entirely rather than truncated - a prefix of a token is
    still a leak. An unparseable value collapses to a placeholder.
    """
    try:
        parsed = httpx.URL(url)
    except Exception:  # noqa: BLE001 - logging must not raise
        return "<unparseable>"
    if not parsed.host:
        return "<no-host>"
    return f"{parsed.scheme}://{parsed.host}"


async def _post(
    client: httpx.AsyncClient, url: str, body: bytes, *, timeout: float
) -> httpx.Response:
    return await client.post(
        url,
        content=body,
        headers={"Content-Type": "application/json"},
        timeout=timeout,
    )


#: HTTP statuses that mean "try again". Split from the rest because the two
#: directions call for opposite action: a 429 is the receiver asking KAI to slow
#: down and a 5xx is the receiver failing, both transient, while every other 4xx
#: is the receiver saying this body will never be accepted. Retrying a 400
#: forever is how an outbox becomes a denial of service against the destination.
RETRYABLE_STATUS_CODES: Final[frozenset[int]] = frozenset({408, 429})


def is_retryable_http_status(status_code: int) -> bool:
    """Whether a non-2xx response is worth another attempt."""
    return status_code in RETRYABLE_STATUS_CODES or status_code >= 500


@dataclass(frozen=True, slots=True)
class DeliveryAttempt:
    """The classified result of one delivery attempt.

    ``error`` is a bounded exception class or the fixed ``HTTPStatusError``
    marker - never a message, a URL, or a body, so it is safe to persist in the
    alert row's ``last_error`` and to log.
    """

    delivered: bool
    retryable: bool
    error: str | None


async def attempt_delivery(
    alert: TaskAlert,
    *,
    url: str,
    timeout: float,
    client: httpx.AsyncClient | None = None,
) -> DeliveryAttempt:
    """POST one alert and classify the outcome. Never raises.

    ``client`` exists so tests can supply a ``MockTransport``; production builds
    a short-lived client per attempt. Alerts are rare and each is delivered by a
    one-shot sweep, so pooling would add lifecycle to manage for no throughput.
    """
    body = alert.render()
    try:
        if client is not None:
            response = await _post(client, url, body, timeout=timeout)
        else:
            async with httpx.AsyncClient() as owned:
                response = await _post(owned, url, body, timeout=timeout)
    except httpx.TimeoutException as exc:
        return DeliveryAttempt(delivered=False, retryable=True, error=type(exc).__name__)
    except httpx.HTTPError as exc:
        # Connection refused, DNS failure, TLS failure: all transient.
        return DeliveryAttempt(delivered=False, retryable=True, error=type(exc).__name__)
    except Exception as exc:  # noqa: BLE001 - alerting must never propagate
        # An unexpected fault is treated as transient: the alert is not lost, and
        # a defect that made every attempt raise should be visible as a queue
        # that keeps retrying rather than as a silently dropped alert.
        return DeliveryAttempt(delivered=False, retryable=True, error=type(exc).__name__)

    if 200 <= response.status_code < 300:
        return DeliveryAttempt(delivered=True, retryable=False, error=None)
    return DeliveryAttempt(
        delivered=False,
        retryable=is_retryable_http_status(response.status_code),
        error="HTTPStatusError",
    )


def alert_kinds() -> Sequence[str]:
    """The kinds this module can emit, for tests and documentation."""
    return (ALERT_KIND_FAILURE, ALERT_KIND_RESOLUTION)


__all__ = [
    "ALERT_SCHEMA",
    "OPEN_FAILURE_STATES",
    "RETRYABLE_STATUS_CODES",
    "DeliveryAttempt",
    "TaskAlert",
    "alert_kinds",
    "attempt_delivery",
    "build_alert",
    "classify_transition",
    "is_retryable_http_status",
]