# Alerting

What alerts exist, where they come from, and how to wire them to a human. KAI
contains no notification code and ships no monitoring stack: it exposes metrics
and this repository ships the rules.

| Artifact | Role |
| --- | --- |
| `deploy/alerting/kai-alerts.yml` | Loaded rules. 14 alerts over the metrics KAI actually serves. |
| `deploy/alerting/kai-alerts.scheduler-pending.yml` | Purge rules. Syntax-checked, deliberately **not** loaded. See below. |
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

- All 14 rules loaded; with a healthy stack every rule evaluated to `inactive`
  and `ALERTS` was empty — no false positives from a cold metric surface.
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

## Destinations

There is no destination configured in this repository, and that is deliberate:
Alertmanager, PagerDuty and Slack integrations belong to the monitoring system,
not to the application. Point the rules at whatever already exists.

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

Until a destination is configured and a test alert has been delivered, this is
`DEPLOYMENT-READY — EXTERNAL ALERT DESTINATION REQUIRED`, not
`PRODUCTION-READY`. The rules being correct is not the same as anyone being told.