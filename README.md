# KAI

Governed Agent Runtime for trusted personal AI agents.

KAI's identity is a **Governed Agent Runtime**: the model proposes, deterministic
and human authorities decide. It is not a general workflow engine.

**Deployment status: Deployment-Ready, with production hardening applied to the
minimum viable set.** Authentication, authorization, rate limiting, revocation,
CORS, TrustedHost, metrics export and request tracing are enforced and tested.
Remaining gaps — chiefly the test-tree typing debt — are listed under
[Known production gaps](#known-production-gaps). Read it before exposing this
to users.

## Documentation

Ordered from highest to lowest authority. Each level explains the one above it and
never contradicts it.

| Level | Document | What it decides |
| --- | --- | --- |
| Canonical | [`docs/architecture/KAI_REFERENCE_ARCHITECTURE_AR.md`](docs/architecture/KAI_REFERENCE_ARCHITECTURE_AR.md) | KAI identity, component authority boundaries, Core vs Extension vs Future |
| Specification | [`docs/kai-ir-spec-v0.md`](docs/kai-ir-spec-v0.md) | KAI-IR primitives, validation, and the control-flow contract |
| Index | [`docs/architecture/DOCUMENTATION_STATUS.md`](docs/architecture/DOCUMENTATION_STATUS.md) | which document is current, and what was archived or removed |

The "Technical & Product Specification v0.5" is **not** held in this repository. The
implemented surface it described is the Schedule-a-Meeting vertical slice, visible in
`src/kai/runtime/service.py` and the test suite; the architecture that governs it is
the canonical reference above.

## Stack

- Backend: Python 3.14 + FastAPI
- Database: PostgreSQL 17 (Docker) + SQLAlchemy 2 + Alembic
- Auth: JWT (HS256) + scrypt password hashing
- Google OAuth: the connect / callback / accounts endpoints, encrypted credential
  persistence, and a refresh-aware access-token authority. Calendar, Gmail and
  Contacts all acquire the access token through that one Google-specific
  authority, so an expired token is exchanged and persisted before the provider
  call instead of failing after expiry. Reconnecting keeps a stored refresh
  token whenever Google's token response omits one, so the account's only
  long-lived credential is never erased by a re-authorization. Open: the
  `google_oauth_access_token_minutes` setting has no consumer, and there is
  deliberately no 401 retry.

## Prerequisites

- Docker + Docker Compose
- Python 3.13+ (3.14 is what CI uses)

## Getting started

### With Docker Compose (recommended)

Reproducible local deployment: database, a one-shot migration job, and the app,
in dependency order.

```powershell
Copy-Item .env.example .env      # edit .env if needed

docker compose up -d db
docker compose run --rm migrate  # applies the schema, then exits
docker compose up -d app

Invoke-RestMethod http://127.0.0.1:8000/api/v1/health
# -> {"status":"ok","database":"reachable"}
#    interactive API docs: http://127.0.0.1:8000/docs
```

Migrations are a separate one-shot service on purpose. The app never migrates
itself: a container that migrates on boot takes schema locks on every replica
start, which turns a rolling restart into a migration race. Here exactly one job
migrates, and the app only reads the schema.

### On the host

```powershell
# 1. Environment
Copy-Item .env.example .env

# 2. Postgres
docker compose up -d db

# 3. Python env
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"

# 4. Migrations
alembic upgrade head

# 5. Run
uvicorn kai.main:app --reload
```

## Health endpoint

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/v1/health
```

All HTTP routes are mounted under the `/api/v1` prefix; there is no bare `/health`.

**What it means.** This is a readiness probe, not a liveness probe. It opens a real
connection to Postgres and returns `{"status": "degraded", "database":
"unreachable"}` with HTTP 200 when the database does not answer, so the body —
not the status code — carries the verdict. A container that is up but cannot reach
its database must not be reported as serving traffic. If you need a separate
liveness signal (process is running, dependencies irrelevant), that endpoint does
not exist yet; see gaps below.

## Observability

Three independent signals, all served or emitted by the same process. See
[`docs/OBSERVABILITY.md`](docs/OBSERVABILITY.md) for the metric and span
inventory and the label rules.

### Metrics

```powershell
# Off by default. Enabling it is an exposure decision: the route is
# unauthenticated, so Prometheus cannot present a KAI token.
$env:METRICS_ENABLED = "true"
Invoke-WebRequest http://127.0.0.1:8000/api/v1/metrics | Select-Object -Expand Content
```

`METRICS_ENABLED=false` (the default) means the route does not exist at all: it is
not mounted, and it is absent from the OpenAPI schema. There is no route that
answers 403, because a 403 would confirm the endpoint's existence and shape to an
unauthenticated scanner.

**What it means.** Every label value is drawn from a closed set or is a route
template. There is deliberately no `user_id`, `task_id`, `token`, email address,
raw URL or exception message label anywhere in the output, because a metric is
long-lived and shared: a high-cardinality label is a permanent storage cost, and a
secret in a label is a secret in every downstream system that scrapes it. An
unrecognised value collapses to `unknown` rather than creating a new series.

### Tracing

No collector and no agent. Spans are emitted as JSON log records with
`event="span_ended"`, carrying a W3C `trace_id`, `span_id`, parent `span_id`,
duration and the route template, so a trace is a join over log lines rather than a
second storage system to run.

`TRACING_ACCEPT_INBOUND=false` (the default) means an inbound `traceparent` header
is ignored and this service starts its own root trace. Honouring a header from an
untrusted caller would let any client attach its own ids to this service's spans,
which corrupts the trace it is trying to follow.

### Revocation purge

`revoked_tokens` grows on every logout, so an external scheduler runs a one-shot
job that deletes rows whose expiry has already passed:

```powershell
python -m kai.maintenance.revocation_purge   # exit 0 ok, 1 failed
docker compose run --rm purge-revocations   # same image, same command
```

**What it means.** The job deletes only rows that are *already* unusable, because a
revoked token is denied on the strength of its `jti` and an access token's own
expiry bounds how long it could ever be presented. That is the invariant that makes
this safe to run unattended. Runs take a PostgreSQL transaction-scoped advisory
lock, so overlapping invocations collapse into one purge instead of racing, and a
lock that is already held is exit 0, not an error — a scheduler should not page
anyone because two runs overlapped. No HTTP endpoint purges anything.

## Quality gates

The official commands. CI runs exactly these, and each one fails the build on
error.

```powershell
ruff check .   # repository-wide, no exclusions
mypy            # type-checks src/ (130 files)
pytest -q       # full suite against a real PostgreSQL
```

`mypy` reads its own configuration: `mypy_path = "src"` with
`packages = ["kai"]`, so the checked package is the source tree that ships. KAI
is an application and declares no `py.typed` marker, because nothing installs it
to consume its types.

## Tests

```powershell
docker compose up -d db
# point tests at a scratch database so the dev database is not truncated:
$env:DATABASE_URL = "postgresql+asyncpg://kai:kai@127.0.0.1:5432/kai_test"
pytest -q
```

`tests/conftest.py` truncates every table before and after each test. With the
default `DATABASE_URL` that is the **development** database, so tests will erase
real local data. CI creates and uses a dedicated `kai_test` database; do the same
locally, or point at a throwaway database.

## Configuration

Every setting is declared in `src/kai/config.py` and documented in
[`.env.example`](.env.example). `ENVIRONMENT=production` is fail-closed: the app
refuses to start if `JWT_SECRET` or `GOOGLE_CREDS_ENCRYPTION_SECRET` is still at
its shipped default, or if the Google secret is shorter than 32 characters.
Development defaults are not acceptable production secrets.

## Layout

```text
src/kai/           application package
  api/             HTTP routers (11 routers mounted under /api/v1)
  auth/            JWT, password hashing, current-user dependency
  db/              engine, session, tenant-aware models
  runtime/         execution, fulfillment evaluation, evidence qualification
  config.py        pydantic-settings configuration
  logging.py       structured logging setup
docs/             architecture and specification documents
alembic/           database migrations (linear history, single head)
scripts/           developer utilities
tests/             pytest suite
docker-compose.yml local deployment stack: db + migrate + app
Dockerfile         app image
.github/workflows/ CI quality gate
```

`scripts/inspect_routes.py` dumps every registered route, including FastAPI's
internally included routers. Developer utility; not used at runtime or in CI.

## Principles enforced from Phase 1

- `tenant_id` present in every table; all queries scoped by tenant (MVP: 1 tenant = 1 user).
- Cross-tenant access denied at the application layer (see `tests/test_tenant_isolation.py`).
- Structured JSON logs; no secrets/logged tokens.

## Security controls

The following are enforced in code and covered by tests in
`tests/test_security_hardening.py`.

- **Rate limiting on unauthenticated auth routes.** `/api/v1/auth/login` and
  `/api/v1/auth/register` are limited per source address
  (`AUTH_RATE_LIMIT_REQUESTS` per `AUTH_RATE_LIMIT_WINDOW_SECONDS`). Production
  refuses to start on the in-memory store, because a per-process counter is not a
  limit once more than one replica sits behind a load balancer; Redis is required
  there. If the limiter's store is unreachable the route fails closed with 429
  rather than silently becoming unlimited.
- **Access-token revocation.** Tokens carry a `jti`. `POST /api/v1/auth/logout`
  records it in `revoked_tokens` and the token stops authenticating immediately
  instead of at expiry. Revocation is per token, so signing out one device does
  not sign the account out everywhere. Expired records can be purged
  (`purge_expired_revocations`) and are safe to purge: a record only ever
  outlives a token that expiry already rejects.
- **Account deactivation is enforced.** `is_active` is checked on login and on
  every authenticated request. It used to be reported by `/auth/me` and enforced
  nowhere, so suspending an account did not stop a token already in hand.
- **Login does not disclose account existence.** Password verification runs even
  when no such account exists, so the response time does not distinguish a
  registered email from an unknown one, and both failures return one identical
  body. Unauthenticated 401s never say whether a token was forged, expired,
  revoked, or belonged to a deactivated account.
- **JWT verification is strict.** The signing algorithm is checked against a
  closed allowlist (`HS256/384/512`); production refuses `none` or an asymmetric
  algorithm. `exp`, `iat`, `sub`, `jti`, `iss` and `aud` are all required, so a
  token that omits `exp` cannot be treated as non-expiring. Production refuses a
  `JWT_SECRET` shorter than 32 characters.
- **CORS and TrustedHost are configurable and fail safe.** `CORS_ALLOWED_ORIGINS`
  is empty by default (no cross-origin access) and `*` is refused in production
  because it is incompatible with credentialed requests. `TRUSTED_HOSTS` rejects
  requests whose `Host` is not listed. Both accept a comma-separated or JSON
  list, and an unset variable means "not configured" rather than a boot failure.
- **Passwords.** Registration and login both bound the password to 128
  characters, so the unauthenticated scrypt path cannot be turned into a CPU
  exhaustion primitive by an oversized body. Verification rejects any stored
  hash whose scrypt parameters are not the ones this service writes, so a
  downgraded work factor cannot be accepted.

## Known production gaps

These are open, and none of them are closed by a passing test suite:

- **Rate limiting is per source address, and `X-Forwarded-For` is ignored.**
  Behind a proxy, every client shares the proxy's address, so the limit applies
  per proxy. Honouring the header without a trusted proxy would let any client
  mint a fresh rate-limit identity per request. Terminate the limit at a proxy
  that already does this if you need per-client limits.
- **Access tokens cannot be refreshed.** There is no refresh token; a client
  re-authenticates when its access token expires. Revocation makes expiry
  enforceable early, but it does not extend session length.
- **Tests are outside the type-check gate.** `mypy` reads `packages = ["kai"]` with
  `mypy_path = "src"`, so the checked tree is `src/` and the configured command
  cannot be pointed at `tests/` at all. Tightening that is separate work; every
  test still runs in CI.
- **`gmail_send` is not live-sealable.** `LIVE_SEALABLE_PROFILES` is
  `{"calendar_meeting", "task_preparation"}`. `task_preparation` was added after
  semantic review confirmed its criterion (`objective_digest` from the sealed
  `task.goal`) satisfies the §10.10 anchor requirement. `gmail_send` remains
  refused at the sealing gate because recipient intent is unresolved. Do not add
  it to that set without a separate semantic review.
