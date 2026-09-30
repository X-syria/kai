# KAI

Governed Agent Runtime for trusted personal AI agents.

KAI's identity is a **Governed Agent Runtime**: the model proposes, deterministic
and human authorities decide. It is not a general workflow engine.

**Deployment status: Deployment-Ready. Not Production-Ready.** See
[Known production gaps](#known-production-gaps) before exposing this to users.

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

## Quality gates

The official commands. CI runs exactly these, and each one fails the build on
error.

```powershell
ruff check .   # repository-wide, no exclusions
mypy            # type-checks src/ (117 files)
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

## Known production gaps

Deployment-Ready is not Production-Ready. These are open, and none of them are
closed by a passing test suite:

- **No rate limiting or request-size limits.** `/api/v1/auth/register` and
  `/login` are unauthenticated and unthrottled.
- **No CORS or TrustedHost middleware.** The API is same-origin only as shipped;
  adding a browser front-end requires configuring both explicitly.
- **JWT has no refresh or revocation.** Tokens live for
  `JWT_ACCESS_TOKEN_MINUTES` (default 120) with no deny-list, so logout does not
  invalidate an issued token and secret rotation is the only global kill switch.
- **No request correlation or metrics export.** Failures are logged as structured
  JSON, but there is no tracing, no Prometheus endpoint, and no alerting hook.
- **Tests are outside the type-check gate.** `mypy src tests` reports 196
  findings, all in tests, none in `src`; tightening them is separate work. Every
  test still runs in CI.
- **`gmail_send` is not live-sealable.** `LIVE_SEALABLE_PROFILES` is
  `{"calendar_meeting"}` only, and this is intentional. Recipient intent is
  unresolved, so Gmail send is refused at the sealing gate. Do not add it to that
  set without a separate semantic review.
