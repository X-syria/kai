"""A single pytest-asyncio harness, zero mocks, zero SQLite.

``kai.tests`` runs against the *same* Dockerized PostgreSQL the app uses at
runtime (see ``docker compose up -d db`` + ``alembic upgrade head`` in the
README/AGENTS). The app talks to it through the real kai.db.engine factory,
and so does every fixture here.

The one loop-scoped, module-global async engine is safe because pytest-asyncio
is configured with ``loop_scope = session``: a single event loop lives for the
whole run, so pooled asyncpg connections are never handed to a closed loop.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Final

import httpx
import pytest
import pytest_asyncio

from kai.config import get_settings
from kai.db.engine import engine
from kai.main import app

API_PREFIX: Final = "/api/v1"


@pytest.fixture
def google_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give every Google/Contacts test a deterministic OAuth configuration."""
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv(
        "GOOGLE_OAUTH_REDIRECT_URI", "http://testserver/api/v1/auth/google/callback"
    )
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest_asyncio.fixture(autouse=True)
async def _clean_state() -> AsyncIterator[None]:
    """Start every test from the same empty database, done for real."""
    from sqlalchemy import text

    async with engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE TABLE tenants, users, google_accounts, tasks, task_plans,"
                " approvals, approval_rule_suggestions, memory_entries,"
                " memory_preferences RESTART IDENTITY CASCADE"
            )
        )
    yield
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE TABLE tenants, users, google_accounts, tasks, task_plans,"
                " approvals, approval_rule_suggestions, memory_entries,"
                " memory_preferences RESTART IDENTITY CASCADE"
            )
        )


@pytest_asyncio.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    """A real ASGI transport in front of the real FastAPI app.

    httpx speaks ASGI directly; the only thing standing between the test and
    the database is the same asyncpg/SQLAlchemy layer the server uses.
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as c:
        yield c
