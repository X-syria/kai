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

from kai.config import Settings, get_settings

# Run the suite as if no ``.env`` existed next to the repository.
#
# ``Settings`` reads ``.env`` from the working directory, so creating one
# silently changes how the tests behave. The dangerous case is the intended one:
# a production-shaped file. A ``TRUSTED_HOSTS`` entry rejects the ``testserver``
# Host header httpx sends, and every HTTP test then fails with 400 for reasons
# that have nothing to do with the code under test.
#
# Found while standing up the release stack: the production ``.env`` written for
# the Compose smoke broke the suite, and the failures pointed at host validation
# rather than at the stray file.
#
# This has to happen here, at import time, and not in a fixture. ``kai.main``
# builds the application - and its TrustedHostMiddleware - while this module is
# being imported, so a fixture would run long after the settings that shaped the
# app under test had already been read from disk.
#
# Only the env *file* is disabled. Real environment variables still apply,
# because CI legitimately configures the suite through them, and the database
# URL still comes from the Settings default, which addresses the Compose
# database this suite already documents.
Settings.model_config["env_file"] = None
get_settings.cache_clear()

from kai.db.engine import engine  # noqa: E402
from kai.main import app  # noqa: E402

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
async def _reset_rate_limits() -> AsyncIterator[None]:
    """Drop rate-limit counters between tests.

    The limiter keeps process-global state (that is the whole point of the
    in-memory store), so without this a test that exhausts an auth budget
    would make the next test fail for a reason that has nothing to do with it.
    """
    from kai.security.ratelimit import reset_store

    reset_store()
    yield
    reset_store()


@pytest_asyncio.fixture(autouse=True)
async def _clean_state() -> AsyncIterator[None]:
    """Start every test from the same empty database, done for real."""
    from sqlalchemy import text

    async with engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE TABLE tenants, users, google_accounts, tasks, task_plans,"
                " approvals, approval_rule_suggestions, runtime_executions,"
                " memory_entries,"
                " memory_preferences, revoked_tokens"
                " RESTART IDENTITY CASCADE"
            )
        )
    yield
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE TABLE tenants, users, google_accounts, tasks, task_plans,"
                " approvals, approval_rule_suggestions, runtime_executions,"
                " memory_entries,"
                " memory_preferences, revoked_tokens"
                " RESTART IDENTITY CASCADE"
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
