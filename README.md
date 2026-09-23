# KAI

SaaS-ready platform for trusted personal AI agents.

Source of truth: **Technical & Product Specification v0.5** (Schedule a Meeting vertical slice).

## Stack

- Backend: Python 3.14 + FastAPI
- Database: PostgreSQL (Docker) + SQLAlchemy 2 + Alembic
- Auth: JWT (HS256) + scrypt password hashing (Phase 1 local auth; Google OAuth lands in Phase 2)

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
  api/             HTTP routers (health, auth)
  auth/            JWT, password hashing, current-user dependency
  db/              engine, session, tenant-aware models
  config.py        pydantic-settings configuration
  logging.py       structured logging setup
alembic/           database migrations
tests/             pytest suite
docker-compose.yml PostgreSQL
Dockerfile         app image (build-time; dev workflow runs on host)
```

## Principles enforced from Phase 1

- `tenant_id` present in every table; all queries scoped by tenant (MVP: 1 tenant = 1 user).
- Cross-tenant access denied at the application layer (see `tests/test_tenant_isolation.py`).
- Structured JSON logs; no secrets/logged tokens.