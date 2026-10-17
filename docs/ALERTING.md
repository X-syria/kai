# Alerting

What alerts exist, where they come from, and how to wire them to a human.

KAI has two alerting paths, and they answer different questions. Conflating them
is how a deployment ends up looking covered while paging nobody.

| Path | Scope | Delivers to | Destination lives in |
| --- | --- | --- | --- |
| Prometheus rules | The **service**: it is down, the database is unreachable, a provider is failing, errors are spiking. | `Alertmanager`, which then routes by severity | The monitoring system. Not in this repo. |
| Task-execution webhook | The **work**: one specific task failed or its failure resolved. | One HTTPS webhook a **separate dispatcher process** POSTs to | This repo, via `ALERT_WEBHOOK_URL`. |

| Artifact | Role |
| --- | --- |
| `deploy/alerting/kai-alerts.yml` | Loaded rules. 16 alerts over the metrics KAI actually serves. |
| `deploy/alerting/kai-alerts.scheduler-pending.yml` | Purge rules. Syntax-checked, deliberately **not** loaded. See below. |
| `src/kai/observability/alerting.py` | The task-execution webhook: classification, payload, one delivery attempt. |
| `src/kai/alerts/service.py` | The outbox: identity, atomic enqueue, claim/lease, retry. |
| `src/kai/alerts/models.py` | The `Alert` row and the `alert_dispatcher_state` heartbeat. |
| `src/kai/maintenance/alert_dispatch.py` | The one-shot dispatcher the scheduler runs to drain the outbox. |
| `src/kai/tasks/service.py` | `_transition_task_locked`, the only writer of `task.state`, and the only place an alert can originate. |
| `tests/test_alerting.py` | The failure → alert → resolution lifecycle, against a real receiver, driving the dispatcher. |
| `tests/test_alert_outbox.py` | The outbox's pure pieces: identity vectors, backoff, integrity check, delivery classifier. |
| `docs/OBSERVABILITY.md` | What every metric means. |
| `src/kai/observability/metrics.py` | Where the metrics are defined. |

Both rule files are validated by `promtool check rules` and by
`tests/test_alerting_rules.py`, which fails if a rule names a metric KAI does
not emit, a label value outside a closed set, or a `for: 0s`.

## Two things that silently blind every rule

Both of these were found by running a real Prometheus against the real stack, and
both fail in the same way: the scrape returns nothing useful, Prometheus looks
healthy, and no alert ever evaluates. Neither produces an error anywhere the
operator is looking.

### `TRUSTED_HOSTS` must contain the scrape target's host name

`TRUSTED_HOSTS` is enforced by host-header middleware. When it is non-empty, a
request whose `Host` header is not on the list is answered with **HTTP 400** —
including the metrics request from Prometheus.

The trap is the direction of the mistake. Prometheus scrapes
`http://kai-app:8000/api/v1/metrics`, so the `Host` header it sends is
`kai-app:8000`. If `TRUSTED_HOSTS=localhost,127.0.0.1`, every scrape is a 400.
Prometheus records `up == 0`, which fires `KaiApplicationDown` on a perfectly
healthy application, while every metric-based rule sees no series at all.

Put the name Prometheus dials in the list, or set an explicit
`Host:` in the scrape config:

```yaml
scrape_configs:
  - job_name: kai-app
    metrics_path: /api/v1/metrics
    headers:
      Host: api.example.com      # must be in TRUSTED_HOSTS
    static_configs:
      - targets: ["kai-app:8000"]
```

Or, in Compose, scrape the service alias and allow it:
`TRUSTED_HOSTS=api.example.com,app`. Check
`http://<prometheus>:9090/targets` shows `health=up` and an empty `lastError`;
a target can be `up` while serving 400s only in the sense that the *connection*
succeeded, so the metric being absent is the real tell.

### The health endpoint returns HTTP 200 even when degraded

`/api/v1/health` returns `200` with `{"status": "degraded", "database":
"unreachable"}` when the database is unreachable. This is deliberate
(`src/kai/api/health.py`): the endpoint is a probe, and failing a probe on a
database blip turns one dependency's outage into a restart of every replica.

The consequence for alerting is specific and worth stating plainly:
`KaiReadinessFailing` keys on the health route returning 5xx, so it does **not**
fire when the database is down. It only fires if the health route itself raises.
Database degradation is carried by `KaiDatabaseErrors` and
`KaiDatabaseUnavailable`, which read `kai_db_errors_total` — the health endpoint
increments that counter on every failed probe precisely so a metrics-only
monitor can see what a status-code-only monitor cannot.

There is no readiness gauge in the metric surface today. If a deployment needs
"degraded" to drive load-balancer eviction rather than a page, that needs a
separate metric, not a change to the probe.

### Alerts identify a symptom, not a replica

The request-rate rules aggregate with `sum(...)`, which drops the `job` and
`instance` labels. A firing `KaiDatabaseUnavailable` therefore names a symptom
across the fleet rather than a pod.

That is the right default for paging — a shared dependency being down would
otherwise produce one page per replica — but it means you cannot route from the
alert to a specific replica. Use Prometheus's own target view
(`/targets`, or `up` by `instance`) to find which one is unhealthy.

## Verified against a live stack

These rules were exercised against a real `kai-app`, a real PostgreSQL and a
real Prometheus, not only parsed:

- All rules at the time loaded; with a healthy stack every rule evaluated to
  `inactive` and `ALERTS` was empty — no false positives from a cold metric
  surface. (`KaiAlertDispatchWorkerStale` and `KaiUndeliveredAlerts` were added
  after this run and have **not** been exercised against a live Prometheus; they
  are covered by `promtool` and `tests/test_alerting_rules.py` only.)
- With PostgreSQL stopped, `KaiDatabaseErrors` went `pending` → `firing` and
  `KaiDatabaseUnavailable` went `pending` → `firing` (severity `critical`),
  while `kai_process_up` stayed `1`.
- After PostgreSQL was restarted, both resolved back to `inactive` and health
  returned to `{"status": "ok", "database": "reachable"}` — the alerts are not
  sticky.

`KaiAuthFailureSpike` and `KaiRateLimitRejectionSpike` were **not** fired live.
Their thresholds are set above what one host can produce on purpose (see
below), so tripping them needs a distributed flood, not a test.

## Wiring the rules

KAI exposes a Prometheus text endpoint at `METRICS_PATH` (default
`/api/v1/metrics`), and it is **not mounted at all** unless `METRICS_ENABLED=true`.
That default is an exposure decision: the endpoint is unauthenticated because
Prometheus cannot present a KAI token, so it must only be reachable from a
private network or through a scrape proxy that authenticates callers itself.

With `METRICS_ENABLED=true`, the minimum scrape config:

```yaml
scrape_configs:
  - job_name: kai
    scrape_interval: 15s
    metrics_path: /api/v1/metrics
    static_configs:
      - targets: ["kai-app:8000"]

rule_files:
  - /etc/prometheus/kai-alerts.yml
```

`scrape_interval` must be at most half the smallest `for:` window in the rules,
or an alert cannot be evaluated often enough to become pending in time. At 15s,
every window here is satisfiable.

## Alert rules

| Alert | Condition | Severity | For |
| --- | --- | --- | --- |
| `KaiApplicationDown` | `up{job=~"kai.*"} == 0` | critical | 2m |
| `KaiProcessShuttingDown` | `kai_process_up == 0` | warning | 1m |
| `KaiMetricsEndpointUnavailable` | `absent(kai_process_up)` | critical | 5m |
| `KaiReadinessFailing` | health route returns 5xx (see below) | critical | 5m |
| `KaiPersistentServerErrors` | 5xx rate > 5% of requests | critical | 10m |
| `KaiDatabaseErrors` | any `kai_db_errors_total` rate > 0 | warning | 10m |
| `KaiDatabaseUnavailable` | DB errors > 25% of requests | critical | 5m |
| `KaiAuthFailureSpike` | bad-credential failures > 5/s | warning | 10m |
| `KaiRateLimitRejectionSpike` | rejections > 10/s | warning | 10m |
| `KaiRateLimitBackendUnavailable` | limiter backend unreachable | critical | 5m |
| `KaiRevokedTokenReuse` | revoked tokens being presented | critical | 10m |
| `KaiProviderFailureSpike` | provider error ratio > 20% | warning | 15m |
| `KaiProviderLatencyDegraded` | p95 provider latency > 5s | warning | 15m |
| `KaiTaskFailureSpike` | task failure ratio > 25% | warning | 15m |
| `KaiAlertDispatchWorkerStale` | dispatcher success age > 300s | warning | 5m |
| `KaiUndeliveredAlerts` | alerts permanently failed > 0 | warning | 5m |

### On the thresholds

KAI has never run under production traffic, so there is no historical baseline to
fit and every number here is a declared starting point. Two rules were used
instead of guesses:

- **Ratios wherever a denominator exists.** `5xx / requests`, `failed / (completed
  + failed)`, `provider errors / provider calls` mean the same thing at ten
  requests a minute and at ten thousand. An absolute count cannot.
- **Derived limits where it does not.** There is no "auth successes" counter, so
  `KaiAuthFailureSpike` is derived from the rate limit itself: a single client is
  capped at `AUTH_RATE_LIMIT_REQUESTS` per `AUTH_RATE_LIMIT_WINDOW_SECONDS`
  (10/60s = 0.17/s by default), and the alert sits an order of magnitude above
  that in aggregate. A user mistyping a password cannot reach it.

### On false positives

Every rule uses a sustained condition. One 500, one provider timeout, one failed
login, one rate-limited request or one overlapping purge window cannot fire any
alert here — the shortest `for:` is 1 minute and applies to a gauge that only
goes to zero during shutdown.

## Purge alerting is not Prometheus-based, and why

`kai_revocation_purge_runs_total` is recorded by the purge **process**, into that
process's own `CollectorRegistry`. The purge is a one-shot job: it records, logs,
exits, and the registry dies with it. Nothing scrapes those series.

This is inherent to the agreed design — an external scheduler runs a one-shot
command and the application never owns a schedule — and it is not a defect to fix
in KAI. The consequence is only that these three rules are in
`kai-alerts.scheduler-pending.yml` instead of the loaded file. Loading them today
would produce rules that evaluate `absent()` forever: permanently blind while
appearing to be coverage.

Alert on the job instead, using whichever of these the environment already has.

**Exit status.** `deploy/scheduler/run-purge-revocations.sh` exits `0` on success
*and on overlap*, and non-zero on database failure, crash, or timeout. Systemd
turns that into a notification with no extra service:

```ini
[Unit]
Description=KAI expired-revocation purge

[Service]
Type=oneshot
WorkingDirectory=/opt/kai
EnvironmentFile=/etc/kai/kai.env
ExecStart=/opt/kai/deploy/scheduler/run-purge-revocations.sh
TimeoutStartSec=180
# Any non-zero exit notifies. Overlap exits 0 and is silent, by design.
OnFailure=kai-purge-alert@%n.service
```

**Log records.** This is the recommended path, because it also catches the run
that crashed before it could report anything. The wrapper emits one JSON object
per run on stdout, and the command itself emits structured records. Match on:

| Condition | Query |
| --- | --- |
| Purge failed | `job="kai_purge_revocations" AND status="failed"` |
| Purge timed out | `job="kai_purge_revocations" AND status="timeout"` |
| Purge stopped running | no `job="kai_purge_revocations"` record for 45 minutes |

45 minutes is three missed 15-minute windows. The success case emits
`status="ok"` too, which is what makes "stopped running" detectable at all.

The command's own records, on failure, are `revocation_purge_failed`,
`revocation_purge_aborted` and `revocation_purge_unexpected`. All three log the
exception **type only** — a driver message can embed the connection string, and
that is the one path where a credential would reach a log during an incident.

**Do not alert on `lock_not_acquired`.** Two overlapping runs is the scheduler
working as designed; the loser exits `0`. Paging for it trains people to ignore
this job. `tests/test_alerting_rules.py` fails if any rule selects on it.

## Task-execution failures: the durable outbox and its dispatcher

A failed task is a different event from a failing service, and it is the one an
operator usually wants by name. A database blip is one page for everyone; "the
meeting you booked did not happen" is a message about one specific commitment,
carrying the task ID needed to find it. No ratio threshold can produce that, so
this path does not go through Prometheus at all.

It used to deliver inline: the transition committed, then POSTed, and a process
that died between the two left a durable failure nobody was told about, with no
record that an alert was owed. The state machine refuses `FAILED -> FAILED`, so
nothing could re-raise it — the loss was permanent. That is closed by an outbox.

**Enqueue is atomic with the transition.** `_transition_task_locked` writes one
`alerts` row in the *same* transaction as the state change
(`src/kai/alerts/service.py`). "The failure is durable" and "an operator is owed a
notification" are one commit; neither can exist without the other. The transition
opens no socket, so a slow or down webhook can never slow a request or cost a
state change. `tests/test_alerting.py` asserts this directly: after a transition
returns, the receiver has seen nothing and the alert row is committed and
`PENDING`.

**What fires.** A transition into one of `FAILED`, `REQUIRES_ATTENTION`, `RETRY`
or `ESCALATE` from outside that set opens an incident. `RETRY -> EXECUTING` — the
only legal edge out of it — resolves it. `FAILED -> RETRY` stays silent: it is the
same incident, and reporting a resolution there would say a task was fixed while
it was still broken. `classify_transition` decides this and is the only place the
payload's `kind` is chosen.

**Identity is the deduplication key.** `Alert.id` is a uuid5 over
`(tenant_id, task_id, kind, from_state, to_state, task_attempts)`, joined by
`U+001F`, under the fixed namespace
`a4540a2c-9058-464d-82d5-1be2d327ac83`. The wall-clock `occurred_at` is
deliberately **not** in the key: a clock that moved between two writes of one
incident would otherwise manufacture a second row. The tuple is unique per
incident because `RETRY` is the only edge from a failure state back into the
lifecycle and entering it increments `attempts`. `tests/test_alert_outbox.py`
pins three vectors, so a change to the pre-image, the separator or the namespace
fails a test instead of silently dissolving the guarantee.

**What is sent.** A deterministic JSON body rebuilt by the dispatcher from frozen
columns written at enqueue — never re-read from the mutable `tasks` row, which may
have moved on or been deleted by delivery time: `schema`, `kind`, task and tenant
UUIDs, both states, the sealed fulfillment profile, the retry counters and a UTC
timestamp. It deliberately does **not** carry `goal`, `last_error`, provider text,
or the webhook URL. Those hold user content, provider secrets and the
destination's own credential; an operator who needs detail opens the task. The
webhook URL is never logged either — only its scheme and host — so a broken
destination is visible without the token being reprinted.

**Delivery is a separate one-shot process.** `kai-dispatch-alerts`
(`src/kai/maintenance/alert_dispatch.py`) runs on the operator's scheduler, like
the revocation purge and for the same reason: the application must not own a
schedule. Shipped cadence is every minute (`deploy/scheduler/kai.cron`, wrapper
`run-dispatch-alerts.sh`; also a `dispatch-alerts` service in
`docker-compose.yml`). One sweep:

1. stamps its start in `alert_dispatcher_state` and commits, so a sweep that
   wedges *during* delivery is distinguishable from a scheduler that never fired;
2. reclaims expired leases: `DELIVERING` rows past `lease_until` were claimed by a
   process that died mid-delivery and are made deliverable again;
3. claims and delivers rows that were due at sweep start, one at a time. Each is
   claimed with a conditional `UPDATE ... FOR UPDATE SKIP LOCKED` that sets a
   per-claim `claim_token` and `lease_until`, committed **before** the POST, so no
   row lock is ever held across a network call; and is finalised only
   `WHERE id = :id AND claim_token = :token`, so a sweeper that lost its lease
   cannot overwrite a faster peer's result;
4. stamps its completion.

**Guarantees, stated exactly.** Delivery is **at-least-once, not exactly-once**: a
crash between "the receiver accepted it" and "the row was marked `DELIVERED`"
leaves the row `DELIVERING`, the next sweep re-sends it, and the receiver
recognises the resend because `id` is stable. Retries are bounded — eight attempts
including the first, exponential backoff `30s * 2**(n-1)` capped at `900s`, full
jitter. A connection error, a timeout, HTTP 408/429, or any 5xx is transient;
every other non-2xx is permanent and is not retried forever (retrying a 400 is how
an outbox becomes a denial of service against its own destination). Exhaustion or
permanent refusal parks the row at `FAILED` with `next_attempt_at = NULL`, still
counted on `kai_alerts_permanently_failed`. The bounded failure data is a class
name or the fixed `HTTPStatusError` marker, never a message or a URL.

**Disabled or unconfigured alerting queues, it does not discard.** Enqueue always
happens; delivery is gated on `ALERTING_ENABLED` and `ALERT_WEBHOOK_URL`. A
disabled sweep claims nothing, so alerts stay `PENDING` and deliver once a
destination exists — discarding a durable notification because a webhook is
temporarily unset would spend the guarantee the outbox was built to provide. The
disabled sweep still stamps its start and completion, so "alerting is off" does not
read as "the worker is dead".

**Configuration is unchanged and fail-fast.** Production refuses to start with
`ALERTING_ENABLED=true` and no URL, with a URL set while disabled, with a
non-HTTPS URL, or with a URL that has no host. Loopback HTTP is accepted outside
production so a local receiver stays usable. See `.env.example`.

This path is **not** a substitute for the Prometheus rules. `KaiTaskFailureSpike`
is a fleet-level ratio over a 15-minute window and pages on a trend; this one
names a single task. The two outbox rules below are the other half. None of it
pages anybody until a destination exists.

### Monitoring the outbox

A one-shot dispatcher's own counters die with the process that recorded them — the
same trap the revocation purge documents. So the outbox is observed from the
**application** process, which reads the database on each scrape and exposes:

| Metric | Meaning |
| --- | --- |
| `kai_alerts_queue_pending` | Alerts not yet attempted. |
| `kai_alerts_queue_retryable` | Failed alerts with a retry scheduled. |
| `kai_alerts_queue_oldest_seconds` | Age of the oldest undelivered alert. |
| `kai_alerts_delivering` | Alerts holding a live lease. |
| `kai_alerts_permanently_failed` | Alerts that will not be retried without a human. |
| `kai_alerts_dispatcher_last_success_age_seconds` | Seconds since the last clean sweep. |
| `kai_alerts_dispatcher_last_start_age_seconds` | Seconds since the last sweep *began*. |

The first five come from one aggregate query over `alerts`; the last two from the
single row in `alert_dispatcher_state`. `KaiAlertDispatchWorkerStale` fires when
the success age exceeds `300s` (five missed one-minute ticks), and
`KaiUndeliveredAlerts` fires when a permanently-failed alert exists. The
dispatcher's own `kai_alerts_delivered_total` counter is recorded but is
process-local and not scraped; the gauges above are the scraped truth.

## Destinations for the Prometheus rules

There is no Alertmanager destination configured in this repository, and that is
deliberate: Alertmanager, PagerDuty and Slack integrations belong to the
monitoring system, not to the application. Point the rules at whatever already
exists.

**Alertmanager** — add to `prometheus.yml`:

```yaml
alerting:
  alertmanagers:
    - static_configs: [{targets: ["alertmanager:9093"]}]
```

then route by severity, e.g. `critical` to PagerDuty and `warning` to Slack.

**Direct to Slack or a webhook** — via Alertmanager `webhook_configs`, or with
Alertmanager's native Slack receiver.

**Email** — Alertmanager `email_configs` requires an SMTP server; on a host with
no MTA, cron mail is not a reliable path either.

Note that `ALERT_WEBHOOK_URL` does not serve these rules. It is a separate,
application-owned destination that the `kai-dispatch-alerts` worker POSTs to, and
it receives task-execution incidents only — never an Alertmanager notification.

## Readiness

Until a destination is configured and a test alert has been delivered, this is
`DEPLOYMENT-READY — EXTERNAL ALERT DESTINATION REQUIRED`, not
`PRODUCTION-READY`. The rules being correct is not the same as anyone being
told, and a payload leaving the process is not the same as a human having read
it. See `docs/EXTERNAL-ALERT-DESTINATION-01-REPORT.md` for what has and has not
actually been exercised against an external endpoint.