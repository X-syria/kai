"""Pure unit tests for the extracted test-database target policy.

Every test here is marked ``db_guard_unit`` so the autouse ``_clean_state``
fixture yields immediately: no database is opened and no ``TRUNCATE`` runs.
The tests exercise only the decision functions in ``kai.db.target_policy``; no
PostgreSQL, no connection, and no session is used.
"""

from __future__ import annotations

import pytest
from sqlalchemy.engine.url import make_url

import conftest
from kai.db.target_policy import (
    effective_target_error,
    migration_run_target_error,
    migration_target_error,
    require_explicit_database_url,
    runtime_role_error,
    truncate_authorization_error,
)

pytestmark = pytest.mark.db_guard_unit

_SAFE_URL = "postgresql+asyncpg://kai_test:kai@127.0.0.1:5432/kai_test"
_SAFE_URL_SUFFIXED = "postgresql+asyncpg://kai_test:kai@127.0.0.1:5432/kai_test_ci"
_ADMIN_URL = "postgresql+asyncpg://kai_test_admin:admin@127.0.0.1:5432/kai_test"
_NO_USER_URL = "postgresql+asyncpg://127.0.0.1:5432/kai_test"
_DEFAULT_URL = "postgresql+asyncpg://kai:kai@127.0.0.1:5432/kai"


def test_conftest_reexports_the_extracted_functions() -> None:
    """The guard keeps exposing the same callables after the extraction."""
    assert conftest.truncate_authorization_error is truncate_authorization_error
    assert conftest.effective_target_error is effective_target_error
    assert conftest.runtime_role_error is runtime_role_error


@pytest.mark.parametrize("flag", [None, "", "0", "false", "no", "off", "nope"])
def test_missing_or_false_flag_rejected(flag: str | None) -> None:
    error = truncate_authorization_error(_SAFE_URL, flag)
    assert error is not None
    assert "KAI_TEST_ALLOW_DB_TRUNCATE" in error


@pytest.mark.parametrize("flag", ["1", "true", "TRUE", "Yes", "on", "ON", " 1 "])
def test_truthy_flag_accepts_safe_url(flag: str) -> None:
    assert truncate_authorization_error(_SAFE_URL, flag) is None


def test_flag_is_checked_before_the_url() -> None:
    """Order is preserved: an absent flag wins over an absent URL."""
    error = truncate_authorization_error(None, None)
    assert error is not None
    assert "KAI_TEST_ALLOW_DB_TRUNCATE" in error
    assert "DATABASE_URL not set" not in error


def test_missing_or_empty_database_url_rejected() -> None:
    assert "DATABASE_URL not set" in (truncate_authorization_error(None, "1") or "")
    assert "DATABASE_URL not set" in (truncate_authorization_error("", "1") or "")


def test_default_database_kai_rejected_with_exact_message() -> None:
    assert (
        truncate_authorization_error(_DEFAULT_URL, "1")
        == "refusing to truncate the default database 'kai'"
    )


@pytest.mark.parametrize(
    "name", ["mytest", "kai_testing", "kai_testx", "test", "kai_test_", "mytest_db"]
)
def test_name_outside_the_allowlist_rejected(name: str) -> None:
    url = f"postgresql+asyncpg://kai:kai@127.0.0.1:5432/{name}"
    error = truncate_authorization_error(url, "1")
    assert error is not None
    assert "not an allowlisted test database" in error


@pytest.mark.parametrize("url", [_SAFE_URL, _SAFE_URL_SUFFIXED])
def test_exact_safe_config_accepted(url: str) -> None:
    assert truncate_authorization_error(url, "1") is None


def test_normal_suite_refuses_a_per_run_migration_database() -> None:
    error = truncate_authorization_error(
        "postgresql+asyncpg://kai_test:kai@127.0.0.1:5432/"
        "kai_test_migrations_a1b2c3d4e5f60718",
        "1",
    )
    assert error is not None
    assert "migration" in error


def test_plain_postgresql_driver_accepted() -> None:
    assert (
        truncate_authorization_error(
            "postgresql://kai:kai@127.0.0.1:5432/kai_test", "1"
        )
        is None
    )


@pytest.mark.parametrize(
    "url",
    [
        "sqlite+aiosqlite:///kai_test.db",
        "mysql+asyncmy://kai:kai@127.0.0.1:3306/kai_test",
    ],
)
def test_non_postgres_backend_rejected(url: str) -> None:
    error = truncate_authorization_error(url, "1")
    assert error is not None
    assert "not postgresql" in error


@pytest.mark.parametrize("url", ["not a url", "://broken", "postgres://"])
def test_invalid_url_rejected(url: str) -> None:
    error = truncate_authorization_error(url, "1")
    assert error is not None


def test_database_with_no_name_rejected() -> None:
    # A URL with no path component parses successfully but names no database.
    error = truncate_authorization_error(
        "postgresql+asyncpg://kai:kai@127.0.0.1:5432", "1"
    )
    assert error == "DATABASE_URL does not name a database; refusing to truncate"


def test_effective_target_accepts_identical_target() -> None:
    assert effective_target_error(_SAFE_URL, _SAFE_URL) is None
    assert effective_target_error(_SAFE_URL, make_url(_SAFE_URL)) is None


def test_effective_target_rejects_unverifiable_effective_target() -> None:
    for value in (None, "definitely not a url TopSecret"):
        error = effective_target_error(_SAFE_URL, value)
        assert error is not None
        assert "cannot verify the engine's effective database target" in error
        assert "TopSecret" not in error


def test_effective_target_rejects_invalid_declared_target() -> None:
    error = effective_target_error("not a url TopSecret", _SAFE_URL)
    assert error == "DATABASE_URL is not a valid PostgreSQL URL; refusing to truncate"
    assert "TopSecret" not in error


@pytest.mark.parametrize(
    ("effective", "component"),
    [
        ("postgresql://kai:kai@127.0.0.1:5432/kai_test", "drivername"),
        ("postgresql+asyncpg://other:kai@127.0.0.1:5432/kai_test", "username"),
        ("postgresql+asyncpg://kai:hunter2@127.0.0.1:5432/kai_test", "password"),
        ("postgresql+asyncpg://kai:kai@db.internal:5432/kai_test", "host"),
        ("postgresql+asyncpg://kai:kai@127.0.0.1:5433/kai_test", "port"),
        (_DEFAULT_URL, "database"),
        (
            "postgresql+asyncpg://kai:kai@127.0.0.1:5432/kai_test?sslmode=require",
            "query",
        ),
    ],
)
def test_effective_target_rejects_each_differing_component(
    effective: str, component: str
) -> None:
    error = effective_target_error(_SAFE_URL, effective)
    assert error is not None
    assert error.startswith(
        "engine effective target differs from the authorized URL in: "
    )
    assert component in error
    for secret in (effective, _SAFE_URL, "hunter2", "other", "db.internal", "5433"):
        assert secret not in error


def test_runtime_role_accepts_the_dedicated_runtime_role() -> None:
    assert runtime_role_error(_SAFE_URL) is None
    assert runtime_role_error(make_url(_SAFE_URL)) is None


def test_runtime_role_rejects_other_login_without_echoing_it() -> None:
    error = runtime_role_error(_ADMIN_URL)
    assert error is not None
    assert error == (
        "the engine's effective connection user is not the dedicated"
        " runtime test role; refusing to truncate"
    )
    assert "kai_test_admin" not in error
    assert _ADMIN_URL not in error


def test_runtime_role_rejects_missing_user() -> None:
    error = runtime_role_error(_NO_USER_URL)
    assert error is not None
    assert "names no user" in error
    assert _NO_USER_URL not in error


def test_runtime_role_rejects_unverifiable_target() -> None:
    for value in (None, "definitely not a url TopSecret"):
        error = runtime_role_error(value)
        assert error is not None
        assert "cannot verify the engine's effective database role" in error
        assert "TopSecret" not in error


def test_diagnostics_never_echo_credentials_or_the_full_url() -> None:
    url = "postgresql+asyncpg://kai:SuperSecretPassword@127.0.0.1:5432/mytest"
    error = truncate_authorization_error(url, "1")
    assert error is not None
    assert "SuperSecretPassword" not in error
    assert url not in error


# --- F-01: query parameters must not override the validated target -----------
#
# SQLAlchemy merges ``URL.query`` after translating the authority/path into
# driver arguments.  A URL whose path names ``kai_test`` can therefore connect
# to another database, host, or role while every name-based check still passes.
# The tests below prove the driver-level override exists and that the policy
# refuses it before authorizing anything.

_SAFE_PSYCOPG_URL = "postgresql+psycopg://kai_test:kai@127.0.0.1:5432/kai_test"
_MIGRATION_NAME = "kai_test_migrations_a1b2c3d4e5f60718"
_MIGRATION_RUN_ID = "a1b2c3d4e5f60718"

_REQUESTED_OVERRIDE_KEYS = ["database", "dbname", "user", "host", "port", "password"]
_EXTRA_OVERRIDE_KEYS = ["hostaddr", "passfile", "service", "servicefile"]


def _driver_connect_args(driver: str, url: str) -> dict[str, object]:
    """Return the arguments the driver would really be called with."""
    parsed = make_url(url)
    if driver == "asyncpg":
        from sqlalchemy.dialects.postgresql.asyncpg import PGDialect_asyncpg

        return dict(PGDialect_asyncpg().create_connect_args(parsed)[1])
    from sqlalchemy.dialects.postgresql.psycopg import PGDialectAsync_psycopg

    return dict(PGDialectAsync_psycopg().create_connect_args(parsed)[1])


@pytest.mark.parametrize(
    ("driver", "base", "key", "value", "argument", "expected"),
    [
        ("asyncpg", _SAFE_URL, "database", "production", "database", "production"),
        ("psycopg", _SAFE_PSYCOPG_URL, "dbname", "production", "dbname", "production"),
        ("asyncpg", _SAFE_URL, "user", "postgres", "user", "postgres"),
        ("psycopg", _SAFE_PSYCOPG_URL, "host", "db.internal", "host", "db.internal"),
        ("asyncpg", _SAFE_URL, "port", "6000", "port", 6000),
        ("psycopg", _SAFE_PSYCOPG_URL, "password", "other", "password", "other"),
    ],
)
def test_the_override_really_reaches_the_driver(
    driver: str,
    base: str,
    key: str,
    value: str,
    argument: str,
    expected: object,
) -> None:
    """Evidence for the fix: the path still says kai_test, the driver does not."""
    url = f"{base}?{key}={value}"
    assert make_url(url).database == "kai_test"
    assert _driver_connect_args(driver, url)[argument] == expected


@pytest.mark.parametrize("key", _REQUESTED_OVERRIDE_KEYS)
@pytest.mark.parametrize("base", [_SAFE_URL, _SAFE_PSYCOPG_URL])
def test_truncation_refuses_every_target_overriding_query_key(
    base: str, key: str
) -> None:
    url = f"{base}?{key}=elsewhere"
    error = truncate_authorization_error(url, "1")
    assert error is not None
    assert error == (
        f"DATABASE_URL query overrides the connection target with {key!r};"
        " refusing to truncate"
    )
    for secret in (url, "elsewhere", base):
        assert secret not in error


@pytest.mark.parametrize("key", _EXTRA_OVERRIDE_KEYS)
@pytest.mark.parametrize("base", [_SAFE_URL, _SAFE_PSYCOPG_URL])
def test_truncation_refuses_documented_connection_specifying_keys(
    base: str, key: str
) -> None:
    error = truncate_authorization_error(f"{base}?{key}=elsewhere", "1")
    assert error is not None
    assert "overrides the connection target" in error
    assert repr(key) in error


def test_truncation_refuses_override_keys_case_insensitively() -> None:
    error = truncate_authorization_error(f"{_SAFE_URL}?DATABASE=production", "1")
    assert error is not None
    assert "'database'" in error


def test_truncation_reports_every_offending_key_in_order() -> None:
    error = truncate_authorization_error(
        f"{_SAFE_URL}?user=postgres&host=db.internal&database=production", "1"
    )
    assert error is not None
    assert "'database', 'host', 'user'" in error


@pytest.mark.parametrize("key", _REQUESTED_OVERRIDE_KEYS)
@pytest.mark.parametrize("driver", ["postgresql+asyncpg", "postgresql+psycopg"])
def test_migration_target_refuses_override_keys(driver: str, key: str) -> None:
    url = f"{driver}://kai_test:kai@127.0.0.1:5432/{_MIGRATION_NAME}?{key}=elsewhere"
    error = migration_target_error(url)
    assert error is not None
    assert "overrides the connection target" in error
    assert "refusing to run migrations" in error


@pytest.mark.parametrize("key", _REQUESTED_OVERRIDE_KEYS)
def test_per_run_migration_target_refuses_override_keys(key: str) -> None:
    url = (
        "postgresql+asyncpg://kai_test:kai@127.0.0.1:5432/"
        f"{_MIGRATION_NAME}?{key}=elsewhere"
    )
    error = migration_run_target_error(url, _MIGRATION_RUN_ID)
    assert error is not None
    assert "overrides the connection target" in error


@pytest.mark.parametrize("key", _REQUESTED_OVERRIDE_KEYS)
@pytest.mark.parametrize("base", [_SAFE_URL, _SAFE_PSYCOPG_URL])
def test_alembic_target_refuses_override_keys(base: str, key: str) -> None:
    with pytest.raises(ValueError, match="overrides the connection target"):
        require_explicit_database_url(f"{base}?{key}=elsewhere")


@pytest.mark.parametrize(
    "query", ["sslmode=require", "application_name=kai", "connect_timeout=10"]
)
@pytest.mark.parametrize("base", [_SAFE_URL, _SAFE_PSYCOPG_URL])
def test_non_overriding_query_parameters_are_still_accepted(
    base: str, query: str
) -> None:
    """Only target- and credential-selecting keys are refused."""
    url = f"{base}?{query}"
    assert truncate_authorization_error(url, "1") is None
    assert migration_target_error(url) is None
    assert require_explicit_database_url(url) == url


def test_per_run_migration_target_still_accepts_a_benign_query() -> None:
    url = (
        "postgresql+asyncpg://kai_test:kai@127.0.0.1:5432/"
        f"{_MIGRATION_NAME}?sslmode=require"
    )
    assert migration_run_target_error(url, _MIGRATION_RUN_ID) is None
