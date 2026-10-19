"""Pure unit tests for the test-database safety gate in ``conftest``.

Every test here is marked ``db_guard_unit`` so the autouse ``_clean_state``
fixture yields immediately: no database is opened and no ``TRUNCATE`` runs.
The tests exercise the pure decision functions and the environment wrapper,
and where they need an engine they substitute a stub via ``monkeypatch``. No
PostgreSQL, no connection, and no real database session is ever used.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.engine.url import make_url

import conftest
from conftest import (
    effective_target_error,
    require_truncate_authorized,
    runtime_role_error,
    truncate_authorization_error,
)

pytestmark = pytest.mark.db_guard_unit

# The safe connection uses the dedicated, non-superuser runtime role `kai_test`
# on an allowlisted test database, matching the documented test stack.
_SAFE_URL = "postgresql+asyncpg://kai_test:kai@127.0.0.1:5432/kai_test"
_SAFE_URL_SUFFIXED = "postgresql+asyncpg://kai_test:kai@127.0.0.1:5432/kai_test_ci"
_DEFAULT_URL = "postgresql+asyncpg://kai:kai@127.0.0.1:5432/kai"

# A bootstrap-admin-style superuser login on the same allowlisted database, and
# a connection that names no user at all. Both must be refused by the role
# check even though the database name passes the allowlist.
_ADMIN_ROLE_URL = "postgresql+asyncpg://kai_test_admin:admin@127.0.0.1:5432/kai_test"
_NO_USER_URL = "postgresql+asyncpg://127.0.0.1:5432/kai_test"

# Distinctive values that must never appear in a diagnostic.
_SECRET_HOST = "10.9.8.7"
_SECRET_USER = "intruder_user"
_SECRET_PASSWORD = "hunter2secret"


def _stub_engine(url: str) -> Any:
    """A stand-in for the module-global engine with a controllable ``.url``."""
    return SimpleNamespace(url=make_url(url))


# --- reject: missing / empty DATABASE_URL ---------------------------------- #


def test_missing_database_url_rejected() -> None:
    error = truncate_authorization_error(None, "1")
    assert error is not None
    assert "DATABASE_URL not set" in error


def test_empty_database_url_rejected() -> None:
    error = truncate_authorization_error("", "1")
    assert error is not None
    assert "DATABASE_URL not set" in error


# --- reject: the default database ------------------------------------------ #


def test_default_database_kai_rejected() -> None:
    error = truncate_authorization_error(_DEFAULT_URL, "1")
    assert error is not None
    assert "default database 'kai'" in error


# --- reject: names that merely contain "test" ------------------------------ #


@pytest.mark.parametrize(
    "name",
    ["mytest", "kai_testing", "kai_testx", "test", "kai_test_", "mytest_db"],
)
def test_name_merely_containing_test_rejected(name: str) -> None:
    url = f"postgresql+asyncpg://kai:kai@127.0.0.1:5432/{name}"
    error = truncate_authorization_error(url, "1")
    assert error is not None
    assert "not an allowlisted test database" in error


# --- reject: opt-in flag absent or false ----------------------------------- #


@pytest.mark.parametrize("flag", [None, "", "0", "false", "no", "off", "nope"])
def test_missing_or_false_flag_rejected(flag: str | None) -> None:
    error = truncate_authorization_error(_SAFE_URL, flag)
    assert error is not None
    assert "KAI_TEST_ALLOW_DB_TRUNCATE" in error


@pytest.mark.parametrize("flag", ["1", "true", "TRUE", "Yes", "on", "ON", " 1 "])
def test_truthy_flag_accepts_safe_url(flag: str) -> None:
    assert truncate_authorization_error(_SAFE_URL, flag) is None


# --- reject: invalid / non-postgres URLs ----------------------------------- #


@pytest.mark.parametrize("url", ["not a url", "://broken", "postgres://"])
def test_invalid_url_rejected(url: str) -> None:
    error = truncate_authorization_error(url, "1")
    assert error is not None


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


# --- accept: the exact safe configuration ---------------------------------- #


@pytest.mark.parametrize("url", [_SAFE_URL, _SAFE_URL_SUFFIXED])
def test_only_exact_safe_config_accepted(url: str) -> None:
    assert truncate_authorization_error(url, "1") is None


def test_plain_postgresql_driver_accepted() -> None:
    assert (
        truncate_authorization_error(
            "postgresql://kai:kai@127.0.0.1:5432/kai_test", "1"
        )
        is None
    )


# --- effective-target binding: accept when declared == effective ----------- #


def test_effective_target_accepts_identical_declared_and_effective() -> None:
    assert effective_target_error(_SAFE_URL, _SAFE_URL) is None


def test_effective_target_accepts_already_parsed_url_object() -> None:
    assert effective_target_error(_SAFE_URL, make_url(_SAFE_URL)) is None


# --- effective-target binding: reject when the engine target differs -------- #


def test_effective_target_rejects_different_database() -> None:
    # Declared is an allowlisted test DB; the engine actually points at ``kai``.
    error = effective_target_error(_SAFE_URL, _DEFAULT_URL)
    assert error is not None
    assert "database" in error
    assert "refusing to truncate" in error


def test_effective_target_rejects_effective_none() -> None:
    error = effective_target_error(_SAFE_URL, None)
    assert error is not None
    assert "cannot verify the engine's effective database target" in error


def test_effective_target_rejects_unparseable_effective() -> None:
    raw = "definitely not a url TopSecret"
    error = effective_target_error(_SAFE_URL, raw)
    assert error is not None
    assert "cannot verify the engine's effective database target" in error
    assert "TopSecret" not in error
    assert raw not in error


@pytest.mark.parametrize(
    ("declared", "effective", "component", "secret_token"),
    [
        (
            _SAFE_URL,
            "postgresql://kai:kai@127.0.0.1:5432/kai_test",
            "drivername",
            "postgresql://kai:kai",
        ),
        (
            _SAFE_URL,
            f"postgresql+asyncpg://kai:kai@{_SECRET_HOST}:5432/kai_test",
            "host",
            _SECRET_HOST,
        ),
        (
            _SAFE_URL,
            "postgresql+asyncpg://kai:kai@127.0.0.1:5433/kai_test",
            "port",
            "5433",
        ),
        (
            _SAFE_URL,
            f"postgresql+asyncpg://{_SECRET_USER}:kai@127.0.0.1:5432/kai_test",
            "username",
            _SECRET_USER,
        ),
        (
            _SAFE_URL,
            _DEFAULT_URL,
            "database",
            None,
        ),
        (
            _SAFE_URL,
            f"postgresql+asyncpg://kai:{_SECRET_PASSWORD}@127.0.0.1:5432/kai_test",
            "password",
            _SECRET_PASSWORD,
        ),
        (
            _SAFE_URL,
            "postgresql+asyncpg://kai:kai@127.0.0.1:5432/kai_test?sslmode=require",
            "query",
            "sslmode=require",
        ),
    ],
)
def test_effective_target_rejects_each_differing_component(
    declared: str, effective: str, component: str, secret_token: str | None
) -> None:
    error = effective_target_error(declared, effective)
    assert error is not None
    assert component in error
    assert "refusing to truncate" in error
    # No value, credential, host, or full URL may be echoed.
    assert effective not in error
    assert declared not in error
    if secret_token is not None:
        assert secret_token not in error


# --- secret safety --------------------------------------------------------- #


def test_diagnostic_never_leaks_password() -> None:
    url = "postgresql+asyncpg://kai:SuperSecretPassword@127.0.0.1:5432/mytest"
    error = truncate_authorization_error(url, "1")
    assert error is not None
    assert "SuperSecretPassword" not in error
    assert url not in error


def test_unparseable_url_diagnostic_does_not_echo_input() -> None:
    raw = "definitely not a url TopSecret"
    error = truncate_authorization_error(raw, "1")
    assert error is not None
    assert "TopSecret" not in error
    assert raw not in error


def test_mismatch_diagnostic_never_leaks_credentials_or_host() -> None:
    declared = "postgresql+asyncpg://kai:SuperSecretPassword@127.0.0.1:5432/kai_test"
    effective = "postgresql+asyncpg://other_user:OtherSecret@db.internal:5439/kai_test"
    error = effective_target_error(declared, effective)
    assert error is not None
    for secret in (
        "SuperSecretPassword",
        "OtherSecret",
        "other_user",
        "db.internal",
        declared,
        effective,
    ):
        assert secret not in error


# --- least-privilege role binding ------------------------------------------ #


@pytest.mark.parametrize(
    "effective",
    [_SAFE_URL, _SAFE_URL_SUFFIXED, make_url(_SAFE_URL)],
)
def test_runtime_role_accepts_the_dedicated_runtime_role(effective: Any) -> None:
    assert runtime_role_error(effective) is None


def test_runtime_role_rejects_a_different_admin_role() -> None:
    error = runtime_role_error(_ADMIN_ROLE_URL)
    assert error is not None
    assert "not the dedicated runtime test role" in error
    assert "refusing to truncate" in error
    # The offending username and the URL itself are never echoed.
    assert "kai_test_admin" not in error
    assert _ADMIN_ROLE_URL not in error


def test_runtime_role_rejects_a_missing_user() -> None:
    error = runtime_role_error(_NO_USER_URL)
    assert error is not None
    assert "names no user" in error
    assert "refusing to truncate" in error
    assert _NO_USER_URL not in error


def test_runtime_role_rejects_unparseable_effective() -> None:
    raw = "definitely not a url TopSecret"
    error = runtime_role_error(raw)
    assert error is not None
    assert "cannot verify the engine's effective database role" in error
    assert "TopSecret" not in error
    assert raw not in error


def test_runtime_role_rejects_effective_none() -> None:
    error = runtime_role_error(None)
    assert error is not None
    assert "cannot verify the engine's effective database role" in error


# --- environment wrapper --------------------------------------------------- #


def test_require_truncate_authorized_raises_without_database_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("KAI_TEST_ALLOW_DB_TRUNCATE", "1")
    with pytest.raises(pytest.UsageError):
        require_truncate_authorized()


def test_require_truncate_authorized_raises_when_flag_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", _SAFE_URL)
    monkeypatch.delenv("KAI_TEST_ALLOW_DB_TRUNCATE", raising=False)
    with pytest.raises(pytest.UsageError):
        require_truncate_authorized()


def test_require_truncate_authorized_allows_safe_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", _SAFE_URL)
    monkeypatch.setenv("KAI_TEST_ALLOW_DB_TRUNCATE", "1")
    monkeypatch.setattr(conftest, "engine", _stub_engine(_SAFE_URL))
    assert require_truncate_authorized() is None


def test_require_truncate_authorized_rejects_engine_effective_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Environment is fully authorized, but the engine points at the default DB.
    monkeypatch.setenv("DATABASE_URL", _SAFE_URL)
    monkeypatch.setenv("KAI_TEST_ALLOW_DB_TRUNCATE", "1")
    monkeypatch.setattr(conftest, "engine", _stub_engine(_DEFAULT_URL))
    with pytest.raises(pytest.UsageError) as excinfo:
        require_truncate_authorized()
    message = str(excinfo.value)
    assert "engine effective target differs" in message
    assert _SAFE_URL not in message
    assert _DEFAULT_URL not in message


def test_require_truncate_authorized_rejects_admin_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The database name and the declared/effective binding are fine, but the
    # connection logs in as an administrative (non-runtime) role.
    monkeypatch.setenv("DATABASE_URL", _ADMIN_ROLE_URL)
    monkeypatch.setenv("KAI_TEST_ALLOW_DB_TRUNCATE", "1")
    monkeypatch.setattr(conftest, "engine", _stub_engine(_ADMIN_ROLE_URL))
    with pytest.raises(pytest.UsageError) as excinfo:
        require_truncate_authorized()
    message = str(excinfo.value)
    assert "not the dedicated runtime test role" in message
    assert "kai_test_admin" not in message
    assert _ADMIN_ROLE_URL not in message


# --- behavioral ordering: guard precedes truncation (setup) ---------------- #


async def test_clean_state_cycle_rejects_before_truncating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under an unsafe env, the cleanup coroutine is never awaited."""
    calls: list[str] = []

    async def _spy() -> None:  # pragma: no cover - must never run
        calls.append("truncated")

    monkeypatch.setattr(conftest, "_truncate_all_tables", _spy)
    monkeypatch.setattr(conftest, "engine", _stub_engine(_DEFAULT_URL))
    monkeypatch.setenv("DATABASE_URL", _DEFAULT_URL)
    monkeypatch.setenv("KAI_TEST_ALLOW_DB_TRUNCATE", "1")

    cycle = conftest._clean_state_cycle()
    with pytest.raises(pytest.UsageError):
        await cycle.__anext__()
    await cycle.aclose()
    assert calls == []


# --- behavioral ordering: teardown is guarded too -------------------------- #


async def test_clean_state_cycle_guards_teardown_truncation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setup truncates once; an unsafe teardown refuses before truncating."""
    calls: list[str] = []

    async def _spy() -> None:
        calls.append("truncated")

    monkeypatch.setattr(conftest, "_truncate_all_tables", _spy)
    monkeypatch.setattr(conftest, "engine", _stub_engine(_SAFE_URL))
    monkeypatch.setenv("DATABASE_URL", _SAFE_URL)
    monkeypatch.setenv("KAI_TEST_ALLOW_DB_TRUNCATE", "1")

    cycle = conftest._clean_state_cycle()
    # Setup: the guard passes, so truncation runs exactly once.
    await cycle.__anext__()
    assert calls == ["truncated"]

    # Make the environment unsafe for the teardown check.
    monkeypatch.delenv("KAI_TEST_ALLOW_DB_TRUNCATE", raising=False)
    with pytest.raises(pytest.UsageError):
        await cycle.__anext__()
    await cycle.aclose()
    # Teardown truncation never ran.
    assert calls == ["truncated"]
