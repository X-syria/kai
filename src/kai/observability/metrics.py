"""Low-cardinality counters and histograms for KAI.

Design constraints (from the round brief):

* **Minimum actionable metrics.** No explosion.
* **Bounded labels.** No ``email``, ``user_id``, ``token``, ``request_id``,
  ``task_id``, or raw URL. Every label value comes from a closed set: HTTP
  method, *route template*, response status class, provider name, operation
  class, and the enums defined in this module. Correlation IDs belong in logs
  and traces, never in metric labels, because they are unbounded.
* **Exporter availability is not the app's problem.** Metrics live in this
  process's registry and are rendered on demand at ``/api/v1/metrics``. There
  is no push to a scrape target, so a monitoring backend being unreachable
  cannot affect request handling. Every recording call is exception-safe, so
  even a defect in a metric loses the sample rather than the request.

Names are prefixed ``kai_`` to stay namespaced in a shared monitoring system.
Units follow Prometheus convention: ``_seconds`` for durations, ``_total`` for
monotonically increasing counters.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, get_args

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

from kai.alerts.classification import ALERT_KIND_FAILURE, ALERT_KIND_RESOLUTION
from kai.config import get_settings
from kai.tasks.schemas import FulfillmentProfile

#: Route label used when a request does not resolve to a registered route.
#: One fixed value for every 404 is what keeps ``route`` bounded: labelling by
#: the raw path would let any caller mint unbounded time series.
UNMATCHED_ROUTE: Final[str] = "unmatched"

#: Providers this service calls out to. A closed set: adding one is a code
#: change, never a runtime value.
PROVIDER_GOOGLE: Final[str] = "google"
PROVIDER_PLANNER: Final[str] = "planner"

PROVIDERS: Final[frozenset[str]] = frozenset({PROVIDER_GOOGLE, PROVIDER_PLANNER})

#: Provider operations this service reports. A closed set: the operations are
#: declared by the client methods themselves, not taken from a request, so this
#: list only grows when a call site is added deliberately.
PROVIDER_OPERATIONS: Final[frozenset[str]] = frozenset(
    {
        # Google Calendar
        "list_events",
        "create_event",
        "update_event",
        "delete_event",
        # Google Gmail
        "send_message",
        "list_messages",
        "get_message",
        # Google Contacts
        "list_contacts",
        "get_contact",
        # Google OAuth
        "refresh_token",
        # Planner model
        "plan",
    }
)

#: Fulfillment profiles a task may declare, derived from the schema's own
#: ``FulfillmentProfile`` literal rather than copied here.
#:
#: This is the *declared* vocabulary (``calendar_meeting`` and ``gmail_send``),
#: not the sealable one: a task can be accepted with ``gmail_send`` and then
#: refused at the sealing gate, and that is a real lifecycle event worth
#: counting. Deriving the set means a future profile is labelable the moment the
#: schema admits it, with no edit here to forget. Note that recording a profile
#: is not a claim that it is live or proven - ``gmail_send`` is NOT
#: LIVE-SEALABLE and ``generic`` fulfillment remains partially proven.
LIVE_PROFILES: Final[frozenset[str]] = frozenset(get_args(FulfillmentProfile))

#: Provider call outcomes that are recorded as a failure. Anything else is a
#: success. Kept as enums of *operation*, not of exception class, so the label
#: set stays finite.
PROVIDER_ERROR_TRANSPORT: Final[str] = "transport"

#: Bounded task failure reasons. These are states, not messages.
TASK_FAILURE_STATES: Final[frozenset[str]] = frozenset({"FAILED", "REQUIRES_ATTENTION"})

#: Authentication failure classes. This is the *complete* set the service can
#: report; a caller cannot distinguish more than this about a rejection.
AUTH_FAILURE_REASONS: Final[frozenset[str]] = frozenset(
    {"missing", "invalid", "revoked", "inactive", "bad_credentials"}
)

#: Why the auth limiter refused a request: the limit was hit, or the limiter's
#: own backend was unreachable and it fails closed.
RATE_LIMIT_REJECTION_REASONS: Final[frozenset[str]] = frozenset(
    {"exceeded", "backend_unavailable"}
)

#: Database operations reported as failures. A closed set of operation classes,
#: never a SQL statement or a driver message.
DB_ERROR_OPERATIONS: Final[frozenset[str]] = frozenset({"execute", "commit", "transaction"})

#: The two external alert kinds are the classification contract's, defined once
#: in :mod:`kai.alerts.classification` and imported here so the metric label
#: vocabulary cannot drift from the kinds the delivery path actually emits.

#: Whether the delivery itself worked. ``disabled`` is recorded rather than
#: skipped so that "alerting is off" is visible on the exposition instead of
#: looking identical to "nothing ever failed".
ALERT_OUTCOME_SENT: Final[str] = "sent"
ALERT_OUTCOME_FAILED: Final[str] = "failed"
ALERT_OUTCOME_DISABLED: Final[str] = "disabled"

ALERT_KINDS: Final[frozenset[str]] = frozenset({ALERT_KIND_FAILURE, ALERT_KIND_RESOLUTION})
ALERT_OUTCOMES: Final[frozenset[str]] = frozenset(
    {ALERT_OUTCOME_SENT, ALERT_OUTCOME_FAILED, ALERT_OUTCOME_DISABLED}
)

#: HTTP methods this service labels. A closed set for the same cardinality
#: reason as every other label: an ASGI server can hand a middleware any token
#: in the request line, so the method is untrusted input too.
HTTP_METHODS: Final[frozenset[str]] = frozenset(
    {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
)

#: Bucket for any label value outside its declared set.
#: Every recording call below routes its label values through this, so the
#: bounded-cardinality claim is enforced by the metric layer rather than trusted
#: to each call site. A future call site that invents a new reason string gets
#: one extra series instead of an unbounded family, and the mistake still shows
#: up in a review of the exposition. It must never be dropped silently, because
#: a silently-unrecorded reason is a metric that lies about being complete.
UNKNOWN_LABEL: Final[str] = "unknown"

#: Longest accepted route template. Route templates are code-defined, so this is
#: a backstop against a pathological mount path reaching a label.
MAX_ROUTE_LABEL_LENGTH: Final[int] = 128

#: Purge outcomes recorded by the one-shot revocation maintenance command.
PURGE_RESULT_PURGED: Final[str] = "purged"
PURGE_RESULT_NOTHING_EXPIRED: Final[str] = "nothing_expired"
PURGE_RESULT_LOCK_NOT_ACQUIRED: Final[str] = "lock_not_acquired"
PURGE_RESULT_FAILED: Final[str] = "failed"

PURGE_RESULTS: Final[frozenset[str]] = frozenset(
    {
        PURGE_RESULT_PURGED,
        PURGE_RESULT_NOTHING_EXPIRED,
        PURGE_RESULT_LOCK_NOT_ACQUIRED,
        PURGE_RESULT_FAILED,
    }
)


@dataclass(frozen=True)
class _MetricSet:
    """Every metric this service exposes, plus the registry that owns them."""

    registry: CollectorRegistry
    http_requests_total: Counter
    http_request_duration_seconds: Histogram
    http_5xx_total: Counter
    auth_failures_total: Counter
    auth_rate_limit_rejections_total: Counter
    tasks_started_total: Counter
    tasks_completed_total: Counter
    tasks_failed_total: Counter
    provider_errors_total: Counter
    provider_latency_seconds: Histogram
    db_errors_total: Counter
    revocation_purge_runs_total: Counter
    revocation_purged_rows_total: Counter
    alerts_delivered_total: Counter
    alerts_queue_pending: Gauge
    alerts_queue_retryable: Gauge
    alerts_queue_oldest_seconds: Gauge
    alerts_delivering: Gauge
    alerts_permanently_failed: Gauge
    alerts_dispatcher_last_success_age_seconds: Gauge
    alerts_dispatcher_last_start_age_seconds: Gauge
    process_up: Gauge


def _build() -> _MetricSet:
    """Create the registry and every metric on it.

    A dedicated registry rather than the process-global default one, so the
    exposed set is exactly the set written here: reviewable, and bounded by
    construction rather than by whatever else happens to be installed.
    """
    registry = CollectorRegistry()

    return _MetricSet(
        registry=registry,
        http_requests_total=Counter(
            "kai_http_requests_total",
            "Total HTTP requests handled, by route template and status class.",
            ["method", "route", "status_class"],
            registry=registry,
        ),
        http_request_duration_seconds=Histogram(
            "kai_http_request_duration_seconds",
            "Wall-clock duration of a handled HTTP request.",
            ["method", "route"],
            # Buckets around this service's real cost profile: password
            # verification is deliberately scrypt (tens of ms) while ordinary
            # reads are single-digit ms.
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
            registry=registry,
        ),
        http_5xx_total=Counter(
            "kai_http_5xx_total",
            "HTTP responses this service failed to handle (5xx).",
            ["method", "route"],
            registry=registry,
        ),
        auth_failures_total=Counter(
            "kai_auth_failures_total",
            "Authentication failures, by bounded reason class.",
            ["reason"],
            registry=registry,
        ),
        auth_rate_limit_rejections_total=Counter(
            "kai_auth_rate_limit_rejections_total",
            "Requests rejected by the auth rate limiter, by bounded reason.",
            ["reason"],
            registry=registry,
        ),
        tasks_started_total=Counter(
            "kai_tasks_started_total",
            "Tasks accepted into the lifecycle.",
            ["profile"],
            registry=registry,
        ),
        tasks_completed_total=Counter(
            "kai_tasks_completed_total",
            "Tasks that reached COMPLETED from real evidence.",
            ["profile"],
            registry=registry,
        ),
        tasks_failed_total=Counter(
            "kai_tasks_failed_total",
            "Tasks that entered a failure state.",
            ["profile"],
            registry=registry,
        ),
        provider_errors_total=Counter(
            "kai_provider_errors_total",
            "Outbound provider calls that failed, by provider and operation.",
            ["provider", "operation"],
            registry=registry,
        ),
        provider_latency_seconds=Histogram(
            "kai_provider_latency_seconds",
            "Wall-clock duration of a successful outbound provider call.",
            ["provider", "operation"],
            # Providers are network calls: under 100ms is a healthy provider,
            # multiple seconds is a degraded or blocked one.
            buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0),
            registry=registry,
        ),
        db_errors_total=Counter(
            "kai_db_errors_total",
            "Database operations that failed, by bounded operation class.",
            ["operation"],
            registry=registry,
        ),
        revocation_purge_runs_total=Counter(
            "kai_revocation_purge_runs_total",
            "Runs of the expired-revocation purge, by bounded outcome.",
            ["result"],
            registry=registry,
        ),
        revocation_purged_rows_total=Counter(
            "kai_revocation_purged_rows_total",
            "Expired revoked_tokens rows deleted by the purge.",
            registry=registry,
        ),
        alerts_delivered_total=Counter(
            "kai_alerts_delivered_total",
            "External operational alerts, by bounded kind and delivery outcome.",
            ["kind", "outcome"],
            registry=registry,
        ),
        # The durable outbox is written by the application and drained by a
        # separate one-shot dispatcher, so these are read from the database at
        # scrape time (see kai.api.metrics) rather than incremented in-process:
        # the process that delivers an alert is not the process that is scraped.
        # Unlabelled on purpose - they are counts of a single queue, and a label
        # would only invite a cardinality that nothing needs.
        alerts_queue_pending=Gauge(
            "kai_alerts_queue_pending",
            "Alerts recorded and not yet delivered, with no attempt in flight.",
            registry=registry,
        ),
        alerts_queue_retryable=Gauge(
            "kai_alerts_queue_retryable",
            "Failed alerts a dispatcher will retry (next_attempt_at is set).",
            registry=registry,
        ),
        alerts_queue_oldest_seconds=Gauge(
            "kai_alerts_queue_oldest_seconds",
            "Age of the oldest undelivered alert; 0 when the queue is empty.",
            registry=registry,
        ),
        alerts_delivering=Gauge(
            "kai_alerts_delivering",
            "Alerts currently claimed by a dispatcher under a live lease.",
            registry=registry,
        ),
        alerts_permanently_failed=Gauge(
            "kai_alerts_permanently_failed",
            "Alerts parked at FAILED with no further attempt scheduled.",
            registry=registry,
        ),
        # The dispatcher is one-shot, so it cannot expose its own liveness; it
        # stamps a database row instead and the application turns the age of
        # that row into these two gauges. +Inf means no sweep has ever
        # completed/started, which is maximally stale, not unknown.
        alerts_dispatcher_last_success_age_seconds=Gauge(
            "kai_alerts_dispatcher_last_success_age_seconds",
            "Seconds since the alert dispatcher last completed a clean sweep.",
            registry=registry,
        ),
        alerts_dispatcher_last_start_age_seconds=Gauge(
            "kai_alerts_dispatcher_last_start_age_seconds",
            "Seconds since the alert dispatcher last began a sweep.",
            registry=registry,
        ),
        process_up=Gauge(
            "kai_process_up",
            "1 while the process is serving; 0 once shutdown has begun.",
            registry=registry,
        ),
    )


_metrics: _MetricSet | None = None


def get_metrics() -> _MetricSet:
    """Return the process metric set, building it on first use."""
    global _metrics
    if _metrics is None:
        _metrics = _build()
    return _metrics


def reset_metrics() -> None:
    """Drop the metric set so the next use builds a fresh one. Test seam."""
    global _metrics
    _metrics = None


def render_latest() -> bytes:
    """Render the registry in the Prometheus text exposition format."""
    return generate_latest(get_metrics().registry)


def content_type() -> str:
    return str(CONTENT_TYPE_LATEST)


def metrics_enabled() -> bool:
    return get_settings().metrics_enabled


def status_class(status_code: int) -> str:
    """Collapse a status code to a bounded label value.

    The exact code stays out of the label: the alertable question is always
    "5xx", never "503", and a per-code label multiplies series for no operator
    value.
    """
    return f"{status_code // 100}xx"


def is_server_error(status_code: int) -> bool:
    return status_code >= 500


def _bounded(value: str, allowed: frozenset[str]) -> str:
    """Return ``value`` if it is a declared label value, else the unknown bucket."""
    return value if value in allowed else UNKNOWN_LABEL


def _bounded_route(route: str) -> str:
    """Return the route template, or the unmatched bucket if it is not usable.

    Route templates are declared in code, so this only ever fires if a template
    is pathologically long. It is kept because an unbounded label is the one
    mistake that turns a metrics endpoint into an incident.
    """
    if not route or len(route) > MAX_ROUTE_LABEL_LENGTH:
        return UNMATCHED_ROUTE
    return route


def _safe(metric_name: str, action, *args, **kwargs) -> None:
    """Run one metric mutation, swallowing and logging any failure.

    Instrumentation must not be able to fail the operation it observes. If a
    metric raises, the right production behaviour is to lose the sample, not
    the request.
    """
    try:
        action(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - instrumentation must not propagate
        from kai.logging import get_logger

        get_logger("kai.observability.metrics").warning(
            "metrics_recording_failed", metric=metric_name, error=type(exc).__name__
        )


# ---------------------------------------------------------------------------
# Recording API. One function per observable event.
# ---------------------------------------------------------------------------


def record_http_request(
    *, method: str, route: str, status_code: int, duration_seconds: float
) -> None:
    """Record one finished HTTP request across the three HTTP metrics.

    Every label is resolved *inside* a guarded callable. Resolving it as an
    argument expression instead would move the label lookup outside the guard,
    so a fault in the metric layer would propagate into the request it is
    supposed to be observing.
    """
    m = get_metrics()
    method = _bounded(method, HTTP_METHODS)
    route = _bounded_route(route)
    labels = {"method": method, "route": route, "status_class": status_class(status_code)}
    _safe(
        "kai_http_requests_total", lambda: m.http_requests_total.labels(**labels).inc()
    )
    _safe(
        "kai_http_request_duration_seconds",
        lambda: m.http_request_duration_seconds.labels(method=method, route=route).observe(
            duration_seconds
        ),
    )
    if is_server_error(status_code):
        _safe(
            "kai_http_5xx_total",
            lambda: m.http_5xx_total.labels(method=method, route=route).inc(),
        )


def record_auth_failure(reason: str) -> None:
    """Record one authentication failure.

    ``reason`` is a bounded class (``missing``, ``invalid``, ``revoked``,
    ``inactive``, ``bad_credentials``), never the offending token or the email
    that was attempted.
    """
    metric = get_metrics().auth_failures_total
    label = {"reason": _bounded(reason, AUTH_FAILURE_REASONS)}
    _safe("kai_auth_failures_total", lambda: metric.labels(**label).inc())


def record_rate_limit_rejection(reason: str) -> None:
    """Record one rate-limit rejection (``exceeded`` or ``backend_unavailable``)."""
    metric = get_metrics().auth_rate_limit_rejections_total
    label = {"reason": _bounded(reason, RATE_LIMIT_REJECTION_REASONS)}
    _safe("kai_auth_rate_limit_rejections_total", lambda: metric.labels(**label).inc())


def record_task_event(kind: str, *, profile: str) -> None:
    """Record a task lifecycle event.

    ``profile`` is the sealed fulfillment profile - a property of the code's own
    vocabulary (``calendar_meeting`` today), not of caller input, so it cannot
    grow without a semantic decision.
    """
    m = get_metrics()
    label = {"profile": _bounded(profile, LIVE_PROFILES)}
    targets = {
        "started": (m.tasks_started_total, "kai_tasks_started_total"),
        "completed": (m.tasks_completed_total, "kai_tasks_completed_total"),
        "failed": (m.tasks_failed_total, "kai_tasks_failed_total"),
    }
    target = targets.get(kind)
    if target is None:
        _safe("kai_task_event_unknown_kind", lambda: None, kind)
        return
    metric, name = target
    _safe(name, lambda: metric.labels(**label).inc())


def record_provider_call(
    *, provider: str, operation: str, duration_seconds: float, error: str | None
) -> None:
    """Record one outbound provider call.

    Latency is observed for the success path and the error counter is
    incremented on failure, so a failing provider does not inflate the latency
    histogram with time spent failing. ``duration_seconds`` is still recorded
    alongside a failure in the structured log, which is where latency-on-failure
    is actually worth looking at.
    """
    m = get_metrics()
    label = {
        "provider": _bounded(provider, PROVIDERS),
        "operation": _bounded(operation, PROVIDER_OPERATIONS),
    }
    if error is None:
        _safe(
            "kai_provider_latency_seconds",
            lambda: m.provider_latency_seconds.labels(**label).observe(duration_seconds),
        )
    else:
        _safe(
            "kai_provider_errors_total",
            lambda: m.provider_errors_total.labels(**label).inc(),
        )


def record_db_error(operation: str) -> None:
    """Record one database failure, ``operation`` being a bounded class."""
    metric = get_metrics().db_errors_total
    label = {"operation": _bounded(operation, DB_ERROR_OPERATIONS)}
    _safe("kai_db_errors_total", lambda: metric.labels(**label).inc())


def record_alert_delivery(kind: str, outcome: str) -> None:
    """Record one external alert and whether it was actually delivered.

    An alerting path whose own failures are invisible is not an alerting path:
    a receiver that started returning 500 would otherwise look exactly like a
    quiet night. Both label values are bounded, so a future call site cannot
    invent a cardinality.
    """
    metric = get_metrics().alerts_delivered_total
    label = {
        "kind": _bounded(kind, ALERT_KINDS),
        "outcome": _bounded(outcome, ALERT_OUTCOMES),
    }
    _safe("kai_alerts_delivered_total", lambda: metric.labels(**label).inc())


def set_alert_queue(
    *,
    pending: float,
    retryable: float,
    in_flight: float,
    permanently_failed: float,
    oldest_pending_age_seconds: float,
) -> None:
    """Set the outbox depth gauges from a database snapshot.

    Called by the metrics endpoint on each scrape, not by the dispatcher: the
    dispatcher is a one-shot process whose counters would die with it.
    """
    m = get_metrics()
    _safe("kai_alerts_queue_pending", lambda: m.alerts_queue_pending.set(pending))
    _safe("kai_alerts_queue_retryable", lambda: m.alerts_queue_retryable.set(retryable))
    _safe("kai_alerts_delivering", lambda: m.alerts_delivering.set(in_flight))
    _safe(
        "kai_alerts_permanently_failed",
        lambda: m.alerts_permanently_failed.set(permanently_failed),
    )
    _safe(
        "kai_alerts_queue_oldest_seconds",
        lambda: m.alerts_queue_oldest_seconds.set(oldest_pending_age_seconds),
    )


def set_alert_dispatcher_freshness(
    *,
    last_success_age_seconds: float,
    last_start_age_seconds: float,
) -> None:
    """Set the dispatcher-freshness gauges. ``inf`` means no sweep on record."""
    m = get_metrics()
    _safe(
        "kai_alerts_dispatcher_last_success_age_seconds",
        lambda: m.alerts_dispatcher_last_success_age_seconds.set(
            last_success_age_seconds
        ),
    )
    _safe(
        "kai_alerts_dispatcher_last_start_age_seconds",
        lambda: m.alerts_dispatcher_last_start_age_seconds.set(last_start_age_seconds),
    )


def record_purge_run(result: str, *, purged: int = 0) -> None:
    """Record one purge run and how many rows it removed."""
    m = get_metrics()
    runs = m.revocation_purge_runs_total.labels(result=_bounded(result, PURGE_RESULTS))
    _safe("kai_revocation_purge_runs_total", runs.inc)
    if purged > 0:
        rows = m.revocation_purged_rows_total
        _safe("kai_revocation_purged_rows_total", lambda: rows.inc(purged))


def set_process_up(up: bool) -> None:
    metric = get_metrics().process_up
    value = 1.0 if up else 0.0
    _safe("kai_process_up", lambda: metric.set(value))