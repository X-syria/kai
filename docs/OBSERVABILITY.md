# Observability

What this service emits, what each signal is for, and the rules that keep it
safe to run in production. Everything here is implemented in
`src/kai/observability/` and verified by `tests/test_observability.py`.

Three independent signals, deliberately kept separate:

| Signal | Carrier | Available when | Cost of turning it on |
| --- | --- | --- | --- |
| Metrics | Prometheus text endpoint | `METRICS_ENABLED=true` | An unauthenticated route |
| Tracing | JSON log records (`event="trace.span"`) | `TRACING_ENABLED=true` (default) | Log volume |
| Structured logs | JSON on stdout | Always | Nothing |

There is no collector, no agent, no OTLP exporter and no vendor SDK. A trace is a
join over log lines, which is why tracing adds no infrastructure to operate.

## Configuration

| Setting | Default | Meaning |
| --- | --- | --- |
| `METRICS_ENABLED` | `false` | Mount the Prometheus endpoint |
| `METRICS_PATH` | `/api/v1/metrics` | Where it is mounted |
| `TRACING_ENABLED` | `true` | Emit request spans |
| `TRACING_ACCEPT_INBOUND` | `false` | Honour an inbound `traceparent` |
| `TRACING_SAMPLE_RATIO` | `1.0` | Fraction of requests that get a span |

## Metrics

Off by default, and that default is an exposure decision rather than an
oversight. Prometheus cannot present a KAI token, so the route is
unauthenticated. When disabled it is not mounted and is absent from the OpenAPI
schema — it does not exist and answers nothing, because a `403` would confirm the
endpoint's existence and shape to an unauthenticated scanner.

### Label rules

Every label value is either drawn from a closed set or is a route template.
There is no `user_id`, `task_id`, `jti`, email address, raw URL, query string or
exception message label anywhere in the output.

The reason is that a metric is long-lived and shared. A high-cardinality label is
a permanent storage cost in whatever scrapes it, and a secret in a label is a
secret in every downstream system that copies the metric. An unrecognised value
collapses to `unknown` instead of creating a new series, so a new reason code
cannot be injected by request data.

Route labels are always templates (`/api/v1/tasks/{task_id}`), never concrete
paths, so `/api/v1/tasks/1` and `/api/v1/tasks/2` are one time series rather than
two.

### Inventory

| Metric | Type | Labels |
| --- | --- | --- |
| `kai_http_requests_total` | counter | `method`, `route`, `status_class` |
| `kai_http_request_duration_seconds` | histogram | `method`, `route` |
| `kai_http_5xx_total` | counter | `method`, `route` |
| `kai_auth_failures_total` | counter | `reason` |
| `kai_auth_rate_limit_rejections_total` | counter | `reason` |
| `kai_tasks_started_total` | counter | `profile` |
| `kai_tasks_completed_total` | counter | `profile` |
| `kai_tasks_failed_total` | counter | `profile` |
| `kai_provider_errors_total` | counter | `provider`, `operation` |
| `kai_provider_latency_seconds` | histogram | `provider`, `operation` |
| `kai_db_errors_total` | counter | `operation` |
| `kai_revocation_purge_runs_total` | counter | `result` |
| `kai_revocation_purged_rows_total` | counter | none |
| `kai_process_up` | gauge | none |

Closed label sets:

- `status_class`: derived as `<n>xx` — `2xx`, `3xx`, `4xx`, `5xx`
- `reason` (auth): `missing`, `invalid`, `revoked`, `inactive`, `bad_credentials`
- `reason` (rate limit): `exceeded`, `backend_unavailable`
- `provider`: `google`, `planner`
- `operation` (db): `execute`, `commit`, `transaction`
- `result` (purge): `purged`, `nothing_expired`, `lock_not_acquired`, `failed`
- `profile`: derived from `FulfillmentProfile`; `unknown` for an unrecognised one

### Things to alert on

| Signal | Means |
| --- | --- |
| `kai_process_up == 0` | The process is shutting down, or has not finished starting |
| `rate(kai_http_5xx_total[5m]) > 0` | The service is producing faults |
| `kai_revocation_purge_runs_total{result="failed"}` rising | The purge has stopped working and the table is growing |
| `kai_revocation_purge_runs_total{result="purged"}` flat while rows exist | Same failure, seen from the other side |
| `rate(kai_auth_failures_total{reason="revoked"}[5m])` rising | Either a token-reuse pattern or a purge that deleted too early |

There is no alerting hook in this service. These are thresholds for whatever
already watches your infrastructure.

## Tracing

Spans are emitted as JSON log records:

```json
{"event": "trace.span", "status": "ok", "span": "http.request",
 "trace_id": "…32 hex…", "span_id": "…16 hex…", "parent_span_id": null,
 "duration_seconds": 0.0123, "method": "POST", "route": "/api/v1/tasks",
 "status_class": "2xx"}
```

`status="error"` records carry `error_type` — the exception **class name only**.
`str(exc)` is never used: an exception message can carry an email address, a
token fragment or a database connection string, and this goes to a log line.

`trace_id`, `span_id` and `parent_span_id` are W3C trace-context hex strings, so
an existing log pipeline can parse them into a trace without any KAI-specific
handling.

Spans nest through a `contextvar`: entering `span("…")` inside a request handler,
an auth dependency or a provider call makes it a child of the active span. That
is what turns request → auth → provider into one trace rather than three
unrelated ones.

### Inbound context

`TRACING_ACCEPT_INBOUND=false` ignores an inbound `traceparent` and starts a new
root trace. Honouring a header from an untrusted caller would let any client
attach its own ids to this service's spans, corrupting the trace it is trying to
follow. Turn it on only behind a gateway that sets the header itself.

### Correlation

Every request gets an `X-Request-ID` response header. The caller's own
`X-Request-ID` is reused when it is non-empty, at most 64 characters, matches a
restricted character set, and does not look like an IPv4 address — a header is
attacker-controlled, so an unchecked value would let a caller put arbitrary text
into every log line for that request. Anything else gets a freshly generated id.
The id is bound into the log context and is the join key when a caller's trace
does not reach this service.

### Sampling

`TRACING_SAMPLE_RATIO` decides once per request id, so one logical request keeps
one decision across its retries.

## Failure containment

Instrumentation must not be able to fail the operation it observes. The right
production behaviour when a metric raises is to lose the sample, not the request.

Each individual metric mutation runs inside a guard, and label resolution happens
*inside* that guard rather than as an argument expression, so a fault in the
metric layer cannot escape into the request path. A failed mutation is logged as
`metrics_recording_failed` with the metric name and the exception type — the loss
is visible rather than silent, because a permanently broken registry should not
look identical to a quiet service.

Span emission carries the same guard twice over: `end_span` swallows its own
failures, and the `span` context manager additionally suppresses anything that
escapes it, so the contract holds for a future exporter as well as the current
one.

Both are enforced by tests that make the collector and the span emitter raise and
assert the surrounding request still returns its real status code.

## Revocation purge

`revoked_tokens` grows on every logout and nothing else reclaims it. The purge
job deletes rows whose `expires_at` has already passed.

**Why deleting only expired revocations is safe.** A revoked token is denied on
the strength of its `jti`, and the token's own `exp` bounds how long it could ever
be presented. Once `expires_at` is in the past, no client can present that token
in a way that should be accepted, so the row carries no authority that the
database did not already stop granting. Deleting anything earlier would trade a
storage problem for a security one.

Runs take a PostgreSQL transaction-scoped advisory lock, so:

- overlapping invocations collapse into one purge instead of racing
- a lock already held is `lock_not_acquired` and exit `0`, not an error — a
  scheduler should not page anyone because two runs overlapped
- the lock is released automatically on commit and on rollback

```powershell
python -m kai.maintenance.revocation_purge   # exit 0 ok, 1 failed
docker compose run --rm purge-revocations   # same image, same command
```

Schedule it every 15 minutes. There is no HTTP endpoint that purges anything, and
no in-process scheduler: both would put a destructive write behind an
unauthenticated or ungoverned trigger.

Log records are `revocation_purge_complete`, `revocation_purge_skipped`,
`revocation_purge_failed`, `revocation_purge_aborted` and
`revocation_purge_unexpected`. Failures log the exception type only, for the
reason given under [Tracing](#tracing).
