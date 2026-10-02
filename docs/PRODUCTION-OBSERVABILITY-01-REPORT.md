# PRODUCTION-OBSERVABILITY-01 — Final Report

## 1. Verdict

**DEPLOYMENT-READY — PRODUCTION HARDENING COMPLETE FOR THIS ROUND**

This closes the specific gap that held the previous round's verdict at
`DEPLOYMENT-READY — PRODUCTION HARDENING INCOMPLETE`: metrics export, request
correlation and tracing, and the revocation purge job. Every item in section 3 is
implemented and tested, and every gate in section 4 was run against a real
container and a clean clone.

`PRODUCTION-READY` is still **not** claimed, and section 7 lists what remains
open. Two of those items — rate limiting behind a proxy, and no refresh tokens —
are pre-existing and were not in scope for this round.

## 2. What was actually missing

Read from the code, not inferred from the ticket.

- **No metrics endpoint.** No Prometheus registry, no exposition format, no
  scrape target. Structured JSON logging existed and was the only signal.
- **No request correlation.** `X-Request-ID` was not returned to the caller, so a
  report from a user could not be tied to a log line.
- **No tracing.** No spans, no trace context, no parent/child relationship
  between a request and the auth, task and provider work inside it.
- **No cardinality or secrecy discipline anywhere**, because there was nothing to
  apply it to yet. This is the part that had to be designed rather than added: a
  metrics library will happily accept a `user_id` label, and doing so would have
  turned the observability work into a new data-leak path.
- **`purge_expired_revocations` existed but nothing invoked it.** The previous
  report recorded this as "available but not scheduled". The table grew
  indefinitely.

## 3. What changed

### Metrics

- **`src/kai/observability/metrics.py`** (new) — 14 series (11 counters, 1 gauge,
  2 histograms). Every label value passes through a closed set; an
  unrecognised value becomes `unknown` instead of a new series. Each individual
  mutation runs inside a guard so a registry fault costs the sample, not the
  request.
- **`src/kai/api/metrics.py`** (new) — the route. **Opt-in and absent by
  default**: when `METRICS_ENABLED=false` it is not mounted and does not appear
  in the OpenAPI schema. It answers nothing rather than 403, because a 403
  confirms the endpoint's shape to an unauthenticated scanner.

### Correlation and tracing

- **`src/kai/observability/context.py`** (new) — request-id derivation (a
  caller's id is reused only when it is safe: bounded length, restricted
  charset, not IPv4-shaped) and the route-template matcher.
- **`src/kai/observability/tracing.py`** (new) — W3C `traceparent` parsing,
  `contextvar`-based nesting, and spans emitted as JSON log records. No
  collector, no agent, no OpenTelemetry SDK.
- **`src/kai/middleware.py`** — the middleware is outermost, so a 429 from the
  rate limiter and a 400 from TrustedHost are measured like any other response.
  The route label is resolved *after* `call_next`, when the template is known.
- **`src/kai/logging.py`** — `configure_logging` accepts a stream, and
  `cache_logger_on_first_use` is off so configuration changes take effect.

### Purge

- **`src/kai/maintenance/revocation_purge.py`** (new) — a one-shot command. Takes
  a PostgreSQL transaction-scoped advisory lock; a lock already held is
  `lock_not_acquired` and **exit 0**, because a scheduler must not page anyone
  because two runs overlapped.
- **`docker-compose.yml`** — a `purge-revocations` one-shot service from the same
  image as the app, and the five observability variables passed to `app`.

### Instrumentation

Applied at the existing boundaries, without changing what they decide:
`auth/deps.py` (auth failures by reason, `auth.verify` span), `api/auth.py`,
`api/health.py`, `security/ratelimit.py` (rejection reasons), `tasks/service.py`
(task lifecycle and state transitions), `planner/model.py`, and the
calendar/gmail/contacts/google clients (`provider_call`).

## 4. Evidence

| Gate | Working tree | Clean clone (fresh venv) |
| --- | --- | --- |
| `ruff check .` | clean | clean |
| `mypy` | clean, 130 files | clean, 130 files (mypy 2.4.0) |
| `alembic upgrade head` | `d4e5f6a7b8c9` | `d4e5f6a7b8c9` |
| `pytest -q` | 1136 passed (362 s) | **1136 passed** (377 s) |

Baseline was 1020 passed; **+116 new test cases** from 94 new test functions
across two files (some are parameterised, which is why the case count is higher).
The clean clone installs only what `pyproject.toml` declares, which proves
`prometheus-client>=0.21` is a declared dependency rather than something the
machine happened to have.

**Smoke test: all 9 steps green** against the running container, including four
new gates: the metrics route is **404** while `METRICS_ENABLED` is false, every
response carries an `X-Request-ID`, `docker compose run --rm purge-revocations`
exits 0, and the purge **left the unexpired revocation in place**. Run twice
back-to-back to confirm it is repeatable rather than a one-shot pass.

**A latent bug in the smoke test itself was found by running it twice.** The
throwaway database is dropped and recreated at the start, but the `app` container
from the previous run was still connected to it, and PostgreSQL refuses to drop a
database with live connections. The `DROP`'s return code was never checked, so it
failed silently and the `CREATE` then reported "already exists" — surfacing as
"could not create throwaway database", which reads like a broken stack rather than
a leftover. The app container is now removed before the drop and both commands'
exit codes are checked. Verified by two consecutive clean runs, each starting from
a genuinely empty database.

**A live scrape was checked, not just the code.** With `METRICS_ENABLED=true` over
the container, route labels are full templates
(`kai_http_requests_total{method="POST",route="/api/v1/auth/register",...}`), a
404 becomes `route="unmatched"`, the scrape does **not** count itself, and no
`user_id`, email or token appears anywhere in the output.

### The tests were proven to fail against the broken code

Two mutants were introduced, the suites run, and the mutants restored with the
bytes verified by SHA-256.

| Mutant | Result |
| --- | --- |
| Purge delete predicate changed to remove the `expires_at <= now()` bound | **killed by 6 tests**, including `test_a_live_revocation_is_never_removed`, `test_logout_still_works_after_a_purge_runs` and `test_a_revoked_token_still_stops_authenticating_after_a_purge` |
| `_bounded()` made to return its argument unclamped | **survived `test_observability.py`; killed only by the purge suite** — a real coverage gap, described below |

The second mutant found a defect in the *tests*, not the code: the observability
suite did not own the clamping invariant, so the guarantee was only covered
incidentally elsewhere. Five tests were added that parameterise the clamp across
the auth, rate-limit, provider, database and HTTP-method label domains — one of
them feeding in `postgresql://kai:hunter2@db/kai` and asserting that string never
reaches a label. The mutant is now killed by five tests in the suite that owns the
invariant, and by a test that passes the legitimate values through unchanged.

### A documented invariant that had no test

`docs/OBSERVABILITY.md` claimed telemetry cannot break a request. That claim was
not backed by a test — patching `record_http_request` to raise produced a 500,
because the guard is per-mutation inside the recorder, not around it. Rather than
soften the sentence, three tests were added that break the collector and the span
emitter and assert the request still returns its real status, plus an assertion
that the loss is *logged* (`metrics_recording_failed`) rather than silent. The
documentation was then corrected to state the guarantee precisely.

## 5. Invariants verified

- `LIVE_SEALABLE_PROFILES` is byte-identical to the pre-round snapshot: still
  exactly `{"calendar_meeting"}`. `gmail_send` remains non-live-sealable. The
  observability metric's profile label domain is derived from `FulfillmentProfile`
  and is deliberately *not* the live-sealable set — a rejected profile is a
  legitimate thing to observe.
- Every diff in a semantic file (`planner/model.py`, `auth/deps.py`,
  `api/health.py`, `security/ratelimit.py`) is instrumentation and a comment
  explaining why the instrumentation is safe there. No decision logic moved.
- The four auth rejection reasons (`missing`, `invalid`, `revoked`, `inactive`)
  are unchanged; the metric reuses that same closed set.
- Tenant isolation, approval boundaries and execution authority untouched.
- Middleware order: request context is outermost, so security rejections are
  measured; CORS and TrustedHost still run before the rate limiter.

## 6. Configuration required in production

No new **required** configuration — nothing fails to start without it. The
changes are opt-in:

- `METRICS_ENABLED=false` by default. Turning it on is an exposure decision:
  the route is unauthenticated and must be reachable only from a private network
  or a scrape proxy that authenticates callers itself. `docker-compose.yml` does
  not publish the port.
- `TRACING_ENABLED=true` by default; tracing costs log volume only.
- `TRACING_ACCEPT_INBOUND=false` by default. Honouring an inbound `traceparent`
  from an untrusted caller would let any client file its failures under its own
  trace id. Enable only behind a gateway that sets the header.
- The purge must be scheduled externally, every 15 minutes, as
  `docker compose run --rm purge-revocations` or
  `python -m kai.maintenance.revocation_purge`.

## 7. What is still open

- **The purge is not scheduled by the application, by design.** It is a
  one-shot command for an external scheduler (cron, Kubernetes CronJob, Task
  Scheduler). An in-process scheduler or an HTTP trigger would put a destructive
  write behind a timer nobody governs. Until a scheduler is configured, the table
  still grows.
- **No alerting hook.** Thresholds are documented in `docs/OBSERVABILITY.md`;
  wiring them to a pager is deployment work.
- **`alembic check` reports pre-existing drift**, not introduced here: six column
  comments declared in `db/models.py` are absent from the schema. `db/models.py`
  is outside this round's delta (verified byte-identical to the snapshot's
  uncommitted state), the CI gate is `alembic upgrade head`, which passes, and
  writing a migration for unrelated columns was not authorised. Recorded rather
  than silently fixed.
- **Rate limiting is per source address and ignores `X-Forwarded-For`.**
  Pre-existing; behind a proxy all clients share one address. Deployments needing
  per-client limits should terminate them at a proxy.
- **No refresh tokens.** A client re-authenticates at expiry.
- **Test-tree typing debt.** The configured `mypy` command checks `src/` only.
  The previous report's "`mypy src tests` reports 196 findings" is not
  reproducible with the current configuration — `packages = ["kai"]` means the
  check cannot be pointed at `tests/` at all — so the README now describes the
  gate as it is rather than quoting a number that cannot be regenerated.
- **`gmail_send` remains non-live-sealable.** Recipient intent is unresolved and
  was deliberately not opened.

## 8. Notes on how this was verified

- **A snapshot was taken before any mutation**
  (`C:\Users\khaled\AppData\Local\Temp\opencode\kai-pre-observability`, 882 files)
  and this round's delta was isolated by hashing against it. That is what
  established that `db/models.py`, the `revoked_tokens` migration,
  `.env.example`, `README.md` and `tests/conftest.py` were *pre-existing*
  uncommitted work from the security round, not this round's. No destructive git
  command was used at any point.
- **Both mutants were restored from a byte-exact copy and re-verified by
  SHA-256**, not rewritten from memory, and the suites were re-run afterwards.
- **Two environmental failures were re-run rather than reported as findings.**
  The Docker engine stopped twice mid-round (once leaving 15 cascading smoke-test
  failures from a single cause). Both were recovered by restarting the service;
  the passing results are quoted above, and the cascade is recorded here so the
  15-failure run is not mistaken for a real defect.
- **The first clean clone reported 148 ruff errors.** They were all inside
  `.claude/worktrees/` — stale nested agent worktrees that a plain directory copy
  dragged in, and which `.gitignore` excludes but which a copy without `.git`
  cannot resolve. The clone was rebuilt excluding them; the numbers above are
  from the corrected clone. No source file was changed to make this pass.
- **Both clean clones are reproducible artifacts.** The reported run is
  `C:\Users\khaled\AppData\Local\Temp\opencode\kai-final-clone`, built from the
  working tree with caches, `.git` and `.claude` excluded and a fresh venv
  installed from `.[dev]` only.
