# KAI

SaaS-ready platform for trusted personal AI agents.

KAI's identity is a **Governed Agent Runtime**: the model proposes, deterministic
and human authorities decide. It is not a general workflow engine.

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
- Database: PostgreSQL (Docker) + SQLAlchemy 2 + Alembic
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
- Python 3.14
- (Windows PowerShell 5.1 assumed)

## Getting started

```powershell
# 1. Environment
Copy-Item .env.example .env
#    edit .env if needed (secrets, DB URL)

# 2. Postgres (Docker)
docker compose up -d db

# 3. Python env
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"

# 4. Migrations
alembic upgrade head

# 5. Run
uvicorn kai.main:app --reload
# -> http://127.0.0.1:8000/docs
```

## Health check

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
```

## Tests

```powershell
docker compose up -d db
pytest
```

Tests run against a dedicated `kai_test` database on the compose Postgres
(created/dropped per test session).

## Layout

```text
src/kai/           application package
  api/             HTTP routers (11 routers mounted under /api/v1)
  auth/            JWT, password hashing, current-user dependency
  db/              engine, session, tenant-aware models
  config.py        pydantic-settings configuration
  logging.py       structured logging setup
docs/             architecture and specification documents
alembic/           database migrations
tests/             pytest suite
docker-compose.yml PostgreSQL
Dockerfile         app image (build-time; dev workflow runs on host)
```

## Principles enforced from Phase 1

- `tenant_id` present in every table; all queries scoped by tenant (MVP: 1 tenant = 1 user).
- Cross-tenant access denied at the application layer (see `tests/test_tenant_isolation.py`).
- Structured JSON logs; no secrets/logged tokens.