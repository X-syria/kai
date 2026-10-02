# PRODUCTION-RELEASE-01 report

**Verdict: DEPLOYMENT-READY — EXTERNAL ALERT DESTINATION REQUIRED**

Not `PRODUCTION-READY`. The code, the scheduler and the alert rules are all
verified against a running stack. What is missing is a human-facing channel:
nothing in this repository can page anyone, and a rule nobody receives is not
alerting. See [Gap 1](#gap-1-no-alert-destination-exists).

| | |
| --- | --- |
| Base commit | `1c6e57b` |
| Head commit | `52a8232` |
| Commits | 4 |
| Diff | 18 files, +1944 / -18 |
| Migrations added | none (`d4e5f6a7b8c9` remains head) |
| Tests | 1173 passed, 9 skipped (baseline 1136) |
| Semantic freeze | intact |

---

## What was asked, and what now exists

| Item | State |
| --- | --- |
| Alembic column-comment drift | **Resolved** — metadata issue, no migration |
| Production scheduler | **Implemented and verified** — external cron, 15 min UTC |
| Prometheus alert rules | **Implemented and verified firing** |
| Alert destination | **Not configured** — external, Gap 1 |
| Purge Prometheus metrics | **Structurally unreachable** — Gap 2, by design |
| Production configuration | **Reviewed** — three real defects found and fixed |
| Semantic freeze | **Unchanged** |

---

## 1. Alembic drift: the model was wrong, not the database

`alembic check` reported six columns as drifted. Autogenerating a migration to
add six comments would have been the worst available response.

The comments were **already in the database**. Migrations `b3d5f7a9c1e2` and
`c8e1f3a5b7d9` applied them; SQLAlchemy never declared them, so every
autogenerate run saw a schema the model did not describe and asked to "add" a
description that had existed since the column was created.

Declared the six `comment=` values in `RuntimeExecution` (`plan_version`,
`plan_hash`, `fulfillment_profile`, `evaluator_version`, `fulfillment_verdict`)
and `Task.fulfillment_profile`. No type, nullability, default or index moves.

`tests/test_schema_metadata_parity.py` compares live PostgreSQL DDL against
live SQLAlchemy metadata, so a column that loses its comment fails the suite
rather than the next deploy. Verified by mutation: removing one comment fails it.

**Known deferred gap.** `alembic/env.py` does not set
`compare_server_default=True`, so six DB-only server defaults would not be
detected: `approval_rule_suggestions.recipient_class`, `approvals.recipient_class`,
and the JSON/array or zero defaults on `runtime_identities`, `runtime_evidence`,
`cursor` and `steps`. Real, and out of scope for this release — the values are
benign and enabling it would surface them all at once.

---

## 2. Production scheduler

An external cron runs the purge every 15 minutes UTC. The application owns no
timer and exposes no scheduler endpoint — that was the specified design, and it
is what the code does.

- `deploy/scheduler/kai.cron` — the schedule
- `deploy/scheduler/run-purge-revocations.sh` — the entrypoint

The wrapper's contract is its exit code, so that is what is tested
(`tests/test_scheduler_deployment.py`, 15 tests):

| Situation | Exit | Recorded |
| --- | --- | --- |
| Success | 0 | `status: ok` |
| **Overlap (lost advisory lock)** | **0** | `status: ok` |
| Database failure | 1 | `status: failed` |
| Timeout | 124 / 137 | `status: timeout` |

Every run emits exactly one parseable `scheduler_run` record. Silence is how a
stopped job stays invisible, so success logs too.

`lock_not_acquired` exits 0 deliberately. A 15-minute job that pages every time
two windows collide trains people to ignore it.

**Verified against a real stack**, not only stubbed:

- The real wrapper drove a real `docker compose run --rm -T purge-revocations`
  against the deployed image: `status: ok`, `exit_code: 0`, 8 s.
- Two concurrent invocations both exited 0 with a parseable record.
- Separately, a throwaway PostgreSQL was seeded with expired and future-dated
  revocations: expired rows deleted, future rows retained, second run a no-op,
  overlap returned `lock_not_acquired` and exit 0, and an invalid database URL
  exited 1 with no traceback and no credential in the output.

**`.gitattributes` pins LF** for the shell script and crontab. This is the
repository's first POSIX-executed files, and `core.autocrlf=true` would hand a
Windows checkout CRLF, where `/bin/sh` treats the carriage return as part of the
command name and the job dies on *every* run — after being installed, scheduled,
and reported as working. Proven: a clone made with `autocrlf=true` active still
contains LF-only files with trailing newlines.

---

## 3. Alerting

`deploy/alerting/kai-alerts.yml` — 14 rules, promtool-valid.
`deploy/alerting/kai-alerts.scheduler-pending.yml` — 3 rules, promtool-valid,
deliberately **not loaded** (Gap 2).

`tests/test_alerting_rules.py` binds every expression to a metric that KAI's own
registry actually emits, closes label values to the sets the code produces, and
rejects any rule missing a severity or an actionable annotation. Mutation-checked:
renaming a metric, corrupting a label value, and dropping a `for:` each fail it.

### Verified live, not just parsed

Real Prometheus, real application, real PostgreSQL failure:

| Phase | Result |
| --- | --- |
| Healthy stack | all 14 rules `inactive`, `ALERTS` empty |
| PostgreSQL stopped | `KaiDatabaseErrors` pending → **firing** (warning) |
| PostgreSQL stopped | `KaiDatabaseUnavailable` pending → **firing** (critical) |
| Throughout | `kai_process_up` stayed `1` — the alerts are about the database, not the process |
| PostgreSQL restored | both resolved to `inactive`, health back to `{"status": "ok"}` |

Not fired live: `KaiAuthFailureSpike` and `KaiRateLimitRejectionSpike`. Their
thresholds sit above what one host can produce on purpose, so tripping them
needs a distributed flood, not a test.

### Two traps that silently blind every rule

Both found by running the real thing, and both fail quietly.

**`TRUSTED_HOSTS` blocks Prometheus with HTTP 400.** Host-header middleware
answers an unlisted `Host` with 400 — including the scrape. Prometheus scrapes
`http://kai-app:8000/...`, so `TRUSTED_HOSTS=localhost,127.0.0.1` makes *every
scrape* a 400. The result is `up == 0`, which fires `KaiApplicationDown` on a
perfectly healthy application, while every metric rule sees no series at all.
Add the scrape target's host name, or set an explicit `Host` header.

**The health endpoint returns HTTP 200 even when degraded.** This is deliberate
(`src/kai/api/health.py`): it is a probe, and failing it on a database blip
turns one dependency's outage into a restart of every replica. The consequence
is that `KaiReadinessFailing` does **not** fire on a database outage — only if
the health route itself raises. Database degradation is carried by
`KaiDatabaseErrors` and `KaiDatabaseUnavailable`, which read
`kai_db_errors_total`; the health endpoint increments that counter on every
failed probe precisely so a metrics-only monitor can see what a
status-code-only monitor cannot.

This gap had already produced a defect: `KaiReadinessFailing` annotated itself
as *"the endpoint reports 200 only when the database is reachable"*, which is
the opposite of what the endpoint does. Verified against the live stack
(health answered 200 with `status: degraded`) and corrected.

**Alerts identify a symptom, not a replica.** The request-rate rules aggregate
with `sum(...)`, dropping `job` and `instance`. Correct for paging — a shared
dependency being down should not produce one page per replica — but you cannot
route from the alert to a pod. Use Prometheus's own target view.

---

## 4. Production configuration review

Ten `Settings` fields had no documentation at all: `APP_NAME`, the four
`MEMORY_*` and the five `APPROVAL_*` values. All now documented in
`.env.example`, with the consequence of changing each, plus new scheduler and
alerting sections.

Three real defects found and fixed in `docker-compose.yml`:

1. **The one-shot jobs could not start in production.** `RATE_LIMIT_BACKEND` and
   `REDIS_URL` were declared only on `app`. Production validation is
   process-wide and fails closed without them, so `migrate` and
   `purge-revocations` raised `RuntimeError` at startup: the migration would
   never be applied and the purge would never delete anything, silently, on
   every invocation. Neither job serves HTTP, but that does not matter to
   process-wide validation. Declared on both; the fail-closed check is untouched.

2. **`ENVIRONMENT` defaulted to `development`** on all three services. A
   deployment that set only `ENVIRONMENT=production` and the secrets ran with no
   production checks at all. Now defaults to `production`.

3. **`RATE_LIMIT_BACKEND` defaulted to `auto`,** which the validator rejects by
   name — even though the limiter resolves `auto` to `redis` whenever
   `REDIS_URL` is set, which Compose always sets. Behaviourally identical,
   textually refused: `ENVIRONMENT=production` alone could not boot the stack.
   Now `redis`.

`tests/test_compose_production_config.py` resolves each service's real
environment and runs it through the real production validators, so a service
that cannot start is a test failure rather than an incident. Mutation-checked:
reintroducing the original omission fails it.

**Test isolation.** `Settings` reads `.env` from the working directory, so
creating one silently changes test behaviour. A production-shaped `.env` sets
`TRUSTED_HOSTS`, which rejects the `testserver` Host header httpx sends, and
every HTTP test fails with 400 for reasons unrelated to the code under test.
Any developer doing deployment work — exactly what this release documents —
would hit it. `conftest.py` now disables the env *file* at import time, before
`kai.main` builds the application and its host middleware. Real environment
variables still apply, so CI configuration is unaffected.

---

## 5. Gaps

### Gap 1 — no alert destination exists

The rules load and fire; nothing receives them. There is deliberately no
destination setting in `.env`, because Alertmanager, PagerDuty and Slack
integrations belong to the monitoring system, not to this service.
`docs/ALERTING.md` gives the wiring for Alertmanager, Slack, webhook and email.

**This is the single thing standing between this release and `PRODUCTION-READY`.**
Before calling it ready: configure a destination and confirm a test alert is
delivered end to end. Correct rules are not the same as a told human.

### Gap 2 — purge metrics are unreachable by design

`kai_revocation_purge_runs_total` is recorded by the purge **process** into that
process's own `CollectorRegistry`, which is destroyed when it exits. Nothing
scrapes it.

This is inherent to the agreed architecture, not a defect to fix in KAI: an
external scheduler runs a one-shot command, so the one-shot process's registry
dies with it. The only consequence is that the three purge rules are parked in
`kai-alerts.scheduler-pending.yml` rather than the loaded file — loading them
today would produce rules that evaluate `absent()` forever, permanently blind
while appearing to be coverage.

Alert on the job instead, via exit status (systemd `OnFailure`) or the log
records documented in `docs/ALERTING.md`. The log path is recommended because it
also catches a run that crashed before it could report anything. Load the pending
rules when a Pushgateway exists.

### Gap 3 — cron is configured, not installed

`deploy/scheduler/kai.cron` is a correct, tested schedule. Whether it is
*installed* on the production host is not verifiable from this repository. Before
go-live, install it (`crontab deploy/scheduler/kai.cron`) and confirm one run
appears in the logs.

### Gap 4 — thresholds are unvalidated by real traffic

KAI has never run under production load. Every threshold is a declared starting
point chosen by rule, not by fit: ratios wherever a denominator exists, and limits
derived from the configured rate limit where one does not. Expect to tune the
numbers. What must not change is the shape — a single transient failure is never
sufficient to fire anything.

### Gap 5 — cosmetic: one commit message has a BOM

`d4a838e`'s subject begins with a UTF-8 BOM, an artifact of writing the message
file from PowerShell. It is invisible in most viewers. Fixing it requires an
interactive rebase, which is outside the constraints for this release, so it was
left alone. (Commit `d421326` has the same pre-existing quirk.)

---

## 6. Verification record

### Regression

| Check | Result |
| --- | --- |
| `pytest` (working tree) | **1173 passed, 9 skipped** |
| `pytest` (clean clone) | **1173 passed, 9 skipped** |
| `ruff check .` | All checks passed |
| `mypy src` | Success, no issues in 129 files |
| `alembic check` (existing DB) | No new upgrade operations |
| `alembic check` (fresh DB) | No new upgrade operations |
| `alembic heads` | `d4e5f6a7b8c9` (single head) |
| `git diff --check` | clean |

Baseline was 1136 collected. The 46 new tests are: 15 scheduler, 14 alerting,
9 compose configuration, 6 schema parity, 1 health degraded-response, 1
signature-tampering determinism. The 9 skips are the POSIX scheduler tests,
which require GNU `timeout`; all 15 pass under Git Bash.

### Deployment

Docker 29.8.1, Compose v5.5.1. Built images; brought the full stack up on
shipped production defaults with only secrets supplied.

- `migrate` exited 0; `purge-revocations` exited 0 and emitted
  `revocation_purge_complete`; `app` reached `healthy`.
- `GET /api/v1/health` → `{"status":"ok","database":"reachable"}`
- `GET /api/v1/metrics` served all 14 metric families
- Register → login → `GET /auth/me` succeeded end to end
- Wrong-password logins returned 429 once the budget was spent, and
  `kai_auth_rate_limit_rejections_total{reason="exceeded"}` incremented to 9
- With PostgreSQL stopped: health returned 200 with `status: degraded`,
  `kai_db_errors_total` incremented, container stayed `healthy`
- Prometheus scraped `app:8000` and loaded all 14 rules

### Clean clone

Cloned to a fresh directory outside the repository, `HEAD = 52a8232`, with
`PYTHONPATH` pinned so it imported its own `src`. All release artifacts present;
full suite identical; `alembic check` clean; `promtool` accepted both rule files;
`sh -n` accepted the scheduler script; scheduler artifacts still LF-only after a
clone with `autocrlf=true`.

### Semantic freeze

Unchanged, and confirmed by diff rather than by assertion:

- `LIVE_SEALABLE_PROFILES = frozenset({"calendar_meeting"})` — untouched
- `gmail_send` remains a sealed profile, not a driven one — untouched
- Generic fulfillment remains partially proven; recipient intent unresolved
- The only `src/` changes are six `comment=` declarations. Verified by reading
  the diff: every changed line is a `comment=` addition or a reflow to add one.
  No type, nullability, default or index moved.

---

## 7. Go-live checklist

1. Configure an alert destination and confirm a test alert is delivered
   (`docs/ALERTING.md`). **Blocks `PRODUCTION-READY`.**
2. Install the schedule: `crontab deploy/scheduler/kai.cron`; confirm one run in
   the logs.
3. Alert on the purge by exit status or log record
   (`docs/ALERTING.md`), not by Prometheus.
4. Set `METRICS_ENABLED=true`, and put the Prometheus target's host name in
   `TRUSTED_HOSTS`.
5. Copy `.env.example` to `.env`; set the secrets and `POSTGRES_*`.
6. Confirm `/targets` shows `health=up` with an empty `lastError`.
