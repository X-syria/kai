"""Pure policy for database-target decisions.

The truncation functions were extracted verbatim from ``tests/conftest.py`` so
the same decisions can be reused without importing pytest. The Alembic helpers
decide whether an explicit migration target was supplied at all, and whether a
target used by the *migration tests* is a safe test target. None of these
functions perform I/O: the caller supplies the declared ``DATABASE_URL`` and,
when available, the SQLAlchemy engine's effective URL (``engine.url``).

The caller still owns everything stateful: reading the process environment,
asking the engine for its effective URL, and deciding when to run the guard
(before every truncation, in setup and teardown). This module only decides.
"""

from __future__ import annotations

import re
from typing import Final

from sqlalchemy.engine import URL
from sqlalchemy.engine.url import make_url

DEFAULT_DB_NAME: Final = "kai"
SAFE_TEST_DB_NAME_RE: Final = re.compile(r"^kai_test(?:_[A-Za-z0-9]+)*$")
#: Per-run migration databases are deliberately narrower than the truncation
#: allowlist: a fixed name is shared state, so it is not accepted here.
MIGRATION_DB_NAME_PREFIX: Final = "kai_test_migrations_"
MIGRATION_DB_NAME_RE: Final = re.compile(r"^kai_test_migrations_[0-9a-f]{16}$")
#: The dedicated, non-superuser runtime role the suite must connect as. The
#: destructive truncation is authorized only for this login.
SAFE_TEST_DB_ROLE: Final = "kai_test"
TRUTHY_FLAGS: Final = frozenset({"1", "true", "yes", "on"})
#: Async drivers KAI can actually construct an engine with.
#:
#: ``postgresql+asyncpg`` is the configured default (``config.py``, Compose, CI,
#: and the docs). ``postgresql+psycopg`` is also declared in ``pyproject.toml``
#: and SQLAlchemy resolves it to its async dialect (``PGDialectAsync_psycopg``).
#: Bare ``postgresql://`` and ``postgresql+psycopg2://`` select the synchronous
#: psycopg2 driver, which cannot back an ``AsyncEngine`` and is not installed.
SUPPORTED_ASYNC_DRIVERS: Final = frozenset(
    {"postgresql+asyncpg", "postgresql+psycopg"}
)
#: Query parameters that can override the connection target or credentials that
#: this module validates.
#:
#: SQLAlchemy merges ``URL.query`` *after* translating the URL authority/path
#: into driver arguments (``asyncpg`` does ``opts.update(url.query)``; ``psycopg``
#: forwards the extra keys as libpq connection parameters).  A URL whose path
#: names ``kai_test`` can therefore connect to another database, host, or role
#: while every name-based check above still passes.  These keys are refused
#: outright rather than reconciled, because no legitimate test target needs them.
#:
#: ``dsn`` is deliberately absent: asyncpg resolves it first and the explicit
#: arguments win, and psycopg does not accept it as a connection keyword, so it
#: cannot override the validated target.  ``options`` is absent because it sets
#: session behaviour (``search_path``), not the connection identity.
TARGET_OVERRIDING_QUERY_KEYS: Final = frozenset(
    {
        "database",
        "dbname",
        "host",
        "hostaddr",
        "passfile",
        "password",
        "port",
        "service",
        "servicefile",
        "user",
    }
)


def overriding_query_keys(url: URL) -> tuple[str, ...]:
    """Return the query keys that would override the validated connection target.

    Pure helper: no I/O and no connection.  Keys are compared case-insensitively
    because environment and query names are not consistently cased; the result
    is sorted so diagnostics are deterministic.
    """
    return tuple(
        sorted({key.lower() for key in url.query} & TARGET_OVERRIDING_QUERY_KEYS)
    )


def query_override_error(url: URL, *, action: str) -> str | None:
    """Return a refusal when ``url`` carries connection-overriding query keys.

    Pure decision function: no I/O and no connection.  Only the *names* of the
    offending keys are echoed -- never a value, a password, or the URL -- and the
    names come from :data:`TARGET_OVERRIDING_QUERY_KEYS`, so a caller-controlled
    string can never reach the message.
    """
    keys = overriding_query_keys(url)
    if not keys:
        return None
    return (
        "DATABASE_URL query overrides the connection target with "
        + ", ".join(repr(key) for key in keys)
        + f"; refusing to {action}"
    )


def truncate_authorization_error(
    database_url: str | None, allow_flag: str | None
) -> str | None:
    """Return a refusal reason, or ``None`` when truncation is authorized.

    Pure decision function: no I/O and no connection, so it is safe to import
    and unit-test. The returned message never contains a password or the full
    URL -- at most the parsed database name is echoed.
    """
    if allow_flag is None or allow_flag.strip().lower() not in TRUTHY_FLAGS:
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
    override_error = query_override_error(url, action="truncate")
    if override_error is not None:
        return override_error
    db_name = url.database
    if not db_name:
        return "DATABASE_URL does not name a database; refusing to truncate"
    if db_name == DEFAULT_DB_NAME:
        return "refusing to truncate the default database 'kai'"
    if SAFE_TEST_DB_NAME_RE.match(db_name) is None:
        return (
            f"database {db_name!r} is not an allowlisted test database"
            " (must match ^kai_test(?:_[A-Za-z0-9]+)*$); refusing to truncate"
        )
    if MIGRATION_DB_NAME_RE.match(db_name) is not None:
        return (
            "refusing to truncate a per-run migration database;"
            " run it through the migration orchestrator"
        )
    return None


def parse_target_url(value: str | URL | None) -> URL | None:
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


def unsupported_async_driver_error(url: URL) -> str | None:
    """Return a refusal when ``url`` does not select a supported async driver.

    Pure decision function: no I/O and no connection. Bare ``postgresql://`` and
    ``postgresql+psycopg2://`` select the synchronous psycopg2 driver, which
    cannot back an ``AsyncEngine``; matching the PostgreSQL backend alone is
    therefore not enough. The message echoes only the driver name, never the URL.
    """
    if url.drivername in SUPPORTED_ASYNC_DRIVERS:
        return None
    return f"DATABASE_URL driver {url.drivername!r} is not a supported async driver"


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
    effective = parse_target_url(effective_url)
    if effective is None:
        return (
            "cannot verify the engine's effective database target;"
            " refusing to truncate"
        )

    declared = parse_target_url(declared_url)
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


def require_explicit_database_url(database_url: str | None) -> str:
    """Return the explicit Alembic target, or raise ``ValueError``.

    Pure decision function: no I/O and no connection. Alembic must never guess
    a target: an absent ``DATABASE_URL`` is refused rather than filled in from
    the application settings, whose default is the development database.

    This is a presence, parse, and async-driver check, not the test-database
    allowlist. An operator may migrate development or production by naming that
    target explicitly; the test-specific restrictions live in
    :func:`migration_target_error` and in the migration test runner.

    ``postgresql://`` and ``postgresql+psycopg2://`` are refused because they
    select the synchronous psycopg2 driver, which cannot back the AsyncEngine
    used by ``alembic/env.py``.

    The raised message never contains a password or the full URL.
    """
    if database_url is None or not database_url.strip():
        raise ValueError(
            "DATABASE_URL is not set; Alembic requires an explicit database target"
        )
    try:
        url = make_url(database_url)
    except Exception:
        raise ValueError(
            "DATABASE_URL is not a valid database URL; refusing to run Alembic"
        ) from None
    driver_error = unsupported_async_driver_error(url)
    if driver_error is not None:
        raise ValueError(f"{driver_error}; refusing to run Alembic")
    override_error = query_override_error(url, action="run Alembic")
    if override_error is not None:
        raise ValueError(override_error)
    return database_url


def escape_configparser_percent(value: str) -> str:
    """Escape ``%`` for Alembic's ``Config.set_main_option``.

    Alembic passes the value to ``ConfigParser``, which treats ``%`` as
    interpolation syntax; a password containing ``%`` would otherwise raise or
    be silently rewritten. Storing ``%%`` makes ``get_main_option`` return the
    original text. Pure: the value is never logged.
    """
    return value.replace("%", "%%")


def migration_target_error(database_url: str | None) -> str | None:
    """Return why a migration-test target is unsafe, or ``None`` when it is safe.

    Pure decision function: no I/O and no connection. The checks mirror the
    test-database policy -- PostgreSQL, an async driver supported by the
    application engine, an allowlisted ``kai_test*`` database, and the dedicated
    ``kai_test`` role -- without consulting the truncation opt-in: running
    migrations is not a truncation and must not be authorized by
    ``KAI_TEST_ALLOW_DB_TRUNCATE``.

    The returned message never contains a password or the full URL. As with the
    truncation policy, matching the database and role names does not prove the
    cluster or host identity.
    """
    if database_url is None or not database_url.strip():
        return "DATABASE_URL is not set; refusing to run migrations"
    try:
        url = make_url(database_url)
        backend = url.get_backend_name()
    except Exception:
        return "DATABASE_URL is not a valid PostgreSQL URL; refusing to run migrations"
    if backend != "postgresql":
        return (
            f"DATABASE_URL backend {backend!r} is not postgresql;"
            " refusing to run migrations"
        )
    driver_error = unsupported_async_driver_error(url)
    if driver_error is not None:
        return f"{driver_error}; refusing to run migrations"
    override_error = query_override_error(url, action="run migrations")
    if override_error is not None:
        return override_error
    db_name = url.database
    if not db_name:
        return "DATABASE_URL does not name a database; refusing to run migrations"
    if db_name == DEFAULT_DB_NAME:
        return "refusing to run migrations against the default database 'kai'"
    if SAFE_TEST_DB_NAME_RE.match(db_name) is None:
        return (
            f"database {db_name!r} is not an allowlisted test database"
            " (must match ^kai_test(?:_[A-Za-z0-9]+)*$); refusing to run migrations"
        )
    if not url.username:
        return "migration target names no user; refusing to run migrations"
    if url.username != SAFE_TEST_DB_ROLE:
        return (
            "migration target user is not the dedicated runtime test role;"
            " refusing to run migrations"
        )
    return None


def migration_run_target_error(
    database_url: str | None, run_id: str | None = None
) -> str | None:
    """Return a refusal reason unless this is a per-run migration database.

    P2 deliberately does not reuse :func:`migration_target_error`: that policy
    accepts every ``kai_test*`` name, including the shared ``kai_test`` database
    and a fixed ``kai_test_migrations``. Migration tests must only target the
    database provisioned for *this* run.

    ``run_id`` is optional for policy unit tests; when supplied, the database
    name must be exactly ``kai_test_migrations_<run_id>``. The function is pure
    and its messages never include a URL, username, or password.
    """
    error = migration_target_error(database_url)
    if error is not None:
        return error

    assert database_url is not None
    url = make_url(database_url)
    db_name = url.database
    assert db_name is not None

    if MIGRATION_DB_NAME_RE.match(db_name) is None:
        return (
            f"database {db_name!r} is not a per-run migration database"
            " (must match ^kai_test_migrations_[0-9a-f]{16}$);"
            " refusing to run migrations"
        )

    if run_id is not None:
        expected = f"{MIGRATION_DB_NAME_PREFIX}{run_id}"
        if db_name != expected:
            return (
                "migration database does not match this run's generated name;"
                " refusing to run migrations"
            )
    return None


def build_migration_database_url(runtime_url: str, database_name: str) -> str:
    """Return ``runtime_url`` with only the database name replaced.

    SQLAlchemy's URL API preserves the driver, credentials, host, port, and
    query parameters. The caller must still validate the result with
    :func:`migration_run_target_error` before using it.
    """
    url = make_url(runtime_url)
    return url.set(database=database_name).render_as_string(hide_password=False)


def runtime_role_error(
    effective_url: str | URL | None,
    expected_role: str = SAFE_TEST_DB_ROLE,
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
    effective = parse_target_url(effective_url)
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
