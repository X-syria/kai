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

import os
import re
from collections.abc import AsyncIterator
from typing import Final

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.engine import URL
from sqlalchemy.engine.url import make_url

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
# because CI legitimately configures the suite through them. Note that the
# Settings default (``.../kai``) is *not* a runnable target for this suite: the
# safety gate below refuses to truncate the default database, so the environment
# must name an allowlisted test database before any fixture truncates.
Settings.model_config["env_file"] = None
get_settings.cache_clear()

from kai.db.engine import engine  # noqa: E402
from kai.main import app  # noqa: E402

API_PREFIX: Final = "/api/v1"


# --------------------------------------------------------------------------- #
# Test-database safety gate
# --------------------------------------------------------------------------- #
#
# The suite truncates real PostgreSQL tables (see ``_clean_state`` below), so a
# stray ``DATABASE_URL`` pointing at a development or production database would
# wipe it. This guard refuses to truncate *before* any session is opened unless
# the environment names a dedicated test database *and* explicitly opts in.
#
# Truncation is authorized only when EVERY condition holds:
#   1. ``KAI_TEST_ALLOW_DB_TRUNCATE`` is truthy ("1"/"true"/"yes"/"on"),
#      case-insensitive; absent or false refuses.
#   2. ``DATABASE_URL`` is set and non-empty.
#   3. It parses and its backend is PostgreSQL (``postgresql`` /
#      ``postgresql+asyncpg``); sqlite and anything else are refused.
#   4. It names a database.
#   5. The name is not the built-in default ``kai``.
#   6. The name matches ``^kai_test(?:_[A-Za-z0-9]+)*$`` -- merely containing
#      the substring "test" is not enough ("mytest", "kai_testing" are refused).
#   7. The declared ``DATABASE_URL`` and the SQLAlchemy engine's *effective* URL
#      (``engine.url``) are the same connection target. Truncation happens
#      through the module-global engine built once at import, so validating the
#      environment alone is not enough: the engine could have been built from a
#      different URL (e.g. an edited ``.env`` or a cached ``Settings``). Every
#      connection-identity component must match -- drivername (backend+driver),
#      username, password, host, port, database name, and query/connection
#      options. A mismatch is refused, never "fixed" by rebuilding the engine.
#   8. The engine's *effective* connection user is the dedicated runtime role
#      ``kai_test``. This reinforces least privilege: a superuser or any other
#      login cannot authorize the destructive truncation even when the database
#      name is allowlisted. It reads the effective ``engine.url`` username (the
#      same binding checked in 7), so it is the role the suite will actually
#      connect with, not merely a name in the environment. It says nothing about
#      the cluster or host identity, which name and role matching cannot prove.
#
# How a developer configures a safe test database (never in a prod shell), using
# the dedicated test stack in docker-compose.test.yml (see docs/TEST_DATABASE.md):
#
#     # a DEDICATED test database owned by the runtime role `kai_test`
#     export DATABASE_URL=postgresql+asyncpg://kai_test:<runtime password>@127.0.0.1:55432/kai_test
#     export KAI_TEST_ALLOW_DB_TRUNCATE=1
#     alembic upgrade head                     # against the test DB
#
# The example uses the test stack's runtime role `kai_test` and host port 55432;
# the development credentials (`kai`, port 5432) are never used for the suite.
#
# This guard only prevents destructive truncation of a non-test database; it
# does NOT prove real isolation (shared clusters, over-broad credentials, or a
# mislabelled host remain possible).
_DEFAULT_DB_NAME: Final = "kai"
_SAFE_TEST_DB_NAME_RE: Final = re.compile(r"^kai_test(?:_[A-Za-z0-9]+)*$")
#: The dedicated, non-superuser runtime role the suite must connect as. The
#: destructive truncation is authorized only for this login.
_SAFE_TEST_DB_ROLE: Final = "kai_test"
_TRUTHY_FLAGS: Final = frozenset({"1", "true", "yes", "on"})


def truncate_authorization_error(
    database_url: str | None, allow_flag: str | None
) -> str | None:
    """Return a refusal reason, or ``None`` when truncation is authorized.

    Pure decision function: no I/O and no connection, so it is safe to import
    and unit-test. The returned message never contains a password or the full
    URL -- at most the parsed database name is echoed.
    """
    if allow_flag is None or allow_flag.strip().lower() not in _TRUTHY_FLAGS:
        return (
            "KAI_TEST_ALLOW_DB_TRUNCATE is not truthy; refusing to truncate"
            " (set KAI_TEST_ALLOW_DB_TRUNCATE=1 to authorize a dedicated test"
            " database)"
        )
    if not database_url:
        return "DATABASE_URL not set; refusing to truncate"
    try:
        url = make_url(database_url)
        backend = url.get_backend_name()
    except Exception:
        return "DATABASE_URL is not a valid PostgreSQL URL; refusing to truncate"
    if backend != "postgresql":
        return (
            f"DATABASE_URL backend {backend!r} is not postgresql;"
            " refusing to truncate"
        )
    db_name = url.database
    if not db_name:
        return "DATABASE_URL does not name a database; refusing to truncate"
    if db_name == _DEFAULT_DB_NAME:
        return "refusing to truncate the default database 'kai'"
    if _SAFE_TEST_DB_NAME_RE.match(db_name) is None:
        return (
            f"database {db_name!r} is not an allowlisted test database"
            " (must match ^kai_test(?:_[A-Za-z0-9]+)*$); refusing to truncate"
        )
    return None


def _parse_target_url(value: str | URL | None) -> URL | None:
    """Parse a connection target, returning ``None`` when it cannot be parsed.

    Pure helper: no I/O and no connection. An already-parsed :class:`URL` is
    accepted as-is so callers can pass ``engine.url`` directly.
    """
    if value is None:
        return None
    if isinstance(value, URL):
        return value
    try:
        return make_url(value)
    except Exception:
        return None


def effective_target_error(
    declared_url: str | None, effective_url: str | URL | None
) -> str | None:
    """Return a refusal reason when the engine's target is not the declared one.

    Pure decision function: no I/O and no connection. ``effective_url`` is the
    URL the module-global engine will actually connect to (``engine.url``).
    Returns ``None`` only when the declared URL and the engine's effective URL
    are provably the same connection target.

    The returned message never echoes a password, username, host value, or the
    full URL -- at most the *names* of the differing components.
    """
    effective = _parse_target_url(effective_url)
    if effective is None:
        return (
            "cannot verify the engine's effective database target;"
            " refusing to truncate"
        )

    declared = _parse_target_url(declared_url)
    if declared is None:
        return "DATABASE_URL is not a valid PostgreSQL URL; refusing to truncate"

    differing: list[str] = []
    if declared.drivername != effective.drivername:
        differing.append("drivername")
    if declared.username != effective.username:
        differing.append("username")
    if declared.password != effective.password:
        differing.append("password")
    if declared.host != effective.host:
        differing.append("host")
    if declared.port != effective.port:
        differing.append("port")
    if declared.database != effective.database:
        differing.append("database")
    if declared.query != effective.query:
        differing.append("query")

    if not differing:
        # The components match, so the targets are the same connection target.
        return None
    return (
        "engine effective target differs from the authorized URL in: "
        + ", ".join(differing)
        + "; refusing to truncate"
    )


def runtime_role_error(
    effective_url: str | URL | None,
    expected_role: str = _SAFE_TEST_DB_ROLE,
) -> str | None:
    """Return a refusal reason unless the engine connects as the runtime role.

    Pure decision function: no I/O and no connection. ``effective_url`` is the
    URL the module-global engine will actually connect to (``engine.url``), so
    the check reads the *effective* connection user rather than a name in the
    environment.

    Least privilege: the destructive truncation is authorized only for the
    dedicated, non-superuser runtime role (``kai_test``). A superuser or any
    other login is refused even when the database name is allowlisted. This does
    not prove the cluster or host identity -- matching a name and a role cannot.

    The returned message never echoes the username, a password, or the URL.
    """
    effective = _parse_target_url(effective_url)
    if effective is None:
        return (
            "cannot verify the engine's effective database role;"
            " refusing to truncate"
        )
    if not effective.username:
        return (
            "the engine's effective connection names no user;"
            " refusing to truncate"
        )
    if effective.username != expected_role:
        return (
            "the engine's effective connection user is not the dedicated"
            " runtime test role; refusing to truncate"
        )
    return None


def require_truncate_authorized() -> None:
    """Fail closed unless the environment authorizes truncating a test DB.

    Synchronous on purpose: callers run it *before* opening any database
    session. Runs three checks in order: the environment-level allowlist
    decision, a binding check that the declared ``DATABASE_URL`` matches the
    SQLAlchemy engine's effective URL, and a least-privilege check that the
    engine's effective connection user is the dedicated runtime role
    ``kai_test``. On the first refusal it raises ``pytest.UsageError`` with the
    diagnostic -- pytest reports this as a configuration error during fixture
    setup, before any truncation executes -- and returns ``None`` when
    truncation is safe.
    """
    error = truncate_authorization_error(
        os.environ.get("DATABASE_URL"),
        os.environ.get("KAI_TEST_ALLOW_DB_TRUNCATE"),
    )
    if error is None:
        error = effective_target_error(
            os.environ.get("DATABASE_URL"),
            engine.url,
        )
    if error is None:
        error = runtime_role_error(engine.url)
    if error is not None:
        raise pytest.UsageError(error)


def pytest_configure(config: pytest.Config) -> None:
    """Declare the marker used to opt tests out of the destructive fixture."""
    config.addinivalue_line(
        "markers",
        "db_guard_unit: pure unit tests for the test-database safety gate;"
        " no DB is opened or truncated",
    )


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


_TRUNCATE_SQL: Final = (
    "TRUNCATE TABLE tenants, users, google_accounts, tasks, task_plans,"
    " approvals, approval_rule_suggestions, runtime_executions,"
    " memory_entries,"
    " memory_preferences, revoked_tokens,"
    " alerts, alert_dispatcher_state"
    " RESTART IDENTITY CASCADE"
)


async def _truncate_all_tables() -> None:
    """Truncate every mutable table, restarting identities.

    Extracted from ``_clean_state`` so the safety gate can be shown to run
    before this coroutine is ever awaited. Callers must run
    :func:`require_truncate_authorized` first.
    """
    from sqlalchemy import text

    async with engine.begin() as conn:
        await conn.execute(text(_TRUNCATE_SQL))


async def _clean_state_cycle() -> AsyncIterator[None]:
    """Run the guarded setup/teardown cycle, checking authorization in both.

    Fails closed on both sides: ``require_truncate_authorized`` runs
    synchronously *first* in setup and again before teardown truncation, so no
    database session is opened unless the environment and the engine's
    effective target are both proven safe. Extracted from the fixture body so
    the ordering can be unit-tested without a database.
    """
    require_truncate_authorized()
    await _truncate_all_tables()
    yield
    require_truncate_authorized()
    await _truncate_all_tables()


@pytest_asyncio.fixture(autouse=True)
async def _clean_state(request: pytest.FixtureRequest) -> AsyncIterator[None]:
    """Start every test from the same empty database, done for real.

    Delegates to :func:`_clean_state_cycle`. Tests marked ``db_guard_unit``
    never touch the database, so the fixture yields immediately without
    checking or truncating.
    """
    if request.node.get_closest_marker("db_guard_unit") is not None:
        yield
        return
    async for _ in _clean_state_cycle():
        yield


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
