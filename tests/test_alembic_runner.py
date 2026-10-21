"""Unit tests for the explicit Alembic target policy and test runner.

Marked ``db_guard_unit`` so the autouse cleanup fixture yields immediately: no
database is opened, no ``TRUNCATE`` runs, and ``subprocess.run`` is replaced by a
spy, so no Alembic process is ever spawned. These tests prove the *decision*
logic only; they say nothing about a live migration.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from alembic.config import Config

from alembic_runner import (
    AlembicTimeout,
    build_child_env,
    run_alembic,
    sanitize_text,
)
from kai.db.target_policy import (
    build_migration_database_url,
    escape_configparser_percent,
    migration_run_target_error,
    migration_target_error,
    require_explicit_database_url,
)

pytestmark = pytest.mark.db_guard_unit

_RUN_ID = "a1b2c3d4e5f60718"
_SAFE_URL = (
    "postgresql+asyncpg://kai_test:kai@127.0.0.1:55432/"
    f"kai_test_migrations_{_RUN_ID}"
)
_SAFE_PSYCOPG_URL = (
    "postgresql+psycopg://kai_test:kai@127.0.0.1:55432/"
    f"kai_test_migrations_{_RUN_ID}"
)
_NORMAL_URL = "postgresql+asyncpg://kai_test:kai@127.0.0.1:55432/kai_test"
_NORMAL_PSYCOPG_URL = (
    "postgresql+psycopg://kai_test:kai@127.0.0.1:55432/kai_test"
)
_ENV_PY = Path(__file__).resolve().parents[1] / "alembic" / "env.py"


class _RunRecorder:
    """Stand-in for ``subprocess.run`` that records calls and returns a result."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
        self.result = subprocess.CompletedProcess(
            args=[], returncode=7, stdout="stdout", stderr="stderr"
        )

    def __call__(self, *args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append((args, kwargs))
        return self.result


@pytest.fixture
def alembic_spy(monkeypatch: pytest.MonkeyPatch) -> _RunRecorder:
    recorder = _RunRecorder()
    monkeypatch.setattr("kai.db.migration_subprocess.subprocess.run", recorder)
    return recorder


@pytest.mark.parametrize("target", [None, "", "   "])
def test_missing_or_blank_target_is_refused_before_spawning(
    alembic_spy: _RunRecorder, target: str | None
) -> None:
    with pytest.raises(RuntimeError, match="DATABASE_URL is not set"):
        run_alembic("upgrade", "head", target_url=target)
    assert alembic_spy.calls == []


def test_runner_does_not_read_database_url_implicitly(
    alembic_spy: _RunRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", _SAFE_URL)
    with pytest.raises(RuntimeError, match="DATABASE_URL is not set"):
        run_alembic("upgrade", "head", target_url=None)
    assert alembic_spy.calls == []


@pytest.mark.parametrize(
    ("target", "message"),
    [
        ("not-a-url", "not a valid PostgreSQL URL"),
        ("sqlite:///tmp/kai_test.db", "is not postgresql"),
        (
            "postgresql://kai_test:kai@127.0.0.1:55432/kai_test",
            "not a supported async driver",
        ),
        (
            "postgresql+psycopg2://kai_test:kai@127.0.0.1:55432/kai_test",
            "not a supported async driver",
        ),
        ("postgresql+asyncpg://kai_test:kai@127.0.0.1:55432/kai", "default database"),
        ("postgresql+asyncpg://kai_test:kai@127.0.0.1:55432/mydb", "not an allowlisted"),
        (_NORMAL_URL, "not a per-run migration database"),
        (
            "postgresql+asyncpg://kai_test:kai@127.0.0.1:55432/kai_test_migrations",
            "not a per-run migration database",
        ),
        (
            "postgresql+asyncpg://kai_test:kai@127.0.0.1:55432/"
            "kai_test_migrations_a1b2c3d4e5f6071",
            "not a per-run migration database",
        ),
        ("postgresql+asyncpg://kai:kai@127.0.0.1:55432/kai_test", "dedicated runtime test role"),
        ("postgresql+asyncpg://127.0.0.1:55432/kai_test", "names no user"),
    ],
)
def test_unsafe_target_is_refused_before_spawning(
    alembic_spy: _RunRecorder, target: str, message: str
) -> None:
    with pytest.raises(RuntimeError, match=message):
        run_alembic("downgrade", "base", target_url=target)
    assert alembic_spy.calls == []


@pytest.mark.parametrize("target", [_SAFE_URL, _SAFE_PSYCOPG_URL])
def test_valid_target_is_passed_through_the_environment(
    alembic_spy: _RunRecorder, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    monkeypatch.setenv("P1_RUNNER_SENTINEL", "kept")

    run_alembic("upgrade", "head", target_url=target)

    assert len(alembic_spy.calls) == 1
    args, kwargs = alembic_spy.calls[0]
    assert args == ([sys.executable, "-m", "alembic", "upgrade", "head"],)
    command = args[0]
    assert isinstance(command, list)
    assert target not in " ".join(command)
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert kwargs["check"] is False
    env = kwargs["env"]
    assert isinstance(env, dict)
    assert env["DATABASE_URL"] == target
    # The parent environment is not copied, so an unrelated sentinel (and by
    # extension any future administrator secret) cannot leak into the child.
    assert "P1_RUNNER_SENTINEL" not in env


def test_child_exit_code_and_result_are_returned_sanitized(
    alembic_spy: _RunRecorder,
) -> None:
    result = run_alembic("current", target_url=_SAFE_URL)
    assert result.returncode == 7
    assert result.stdout == "stdout"
    assert result.stderr == "stderr"


def test_missing_command_is_refused_before_spawning(alembic_spy: _RunRecorder) -> None:
    with pytest.raises(ValueError, match="command is required"):
        run_alembic(target_url=_SAFE_URL)
    assert alembic_spy.calls == []


def test_child_output_is_sanitized_before_it_reaches_the_caller(
    alembic_spy: _RunRecorder,
) -> None:
    alembic_spy.result = subprocess.CompletedProcess(
        args=[],
        returncode=1,
        stdout=f"connecting to {_SAFE_URL}\n",
        stderr=f"password in {_SAFE_URL}\n",
    )
    result = run_alembic("current", target_url=_SAFE_URL)
    assert _SAFE_URL not in result.stdout
    assert _SAFE_URL not in result.stderr
    assert "<redacted" in result.stderr


def test_target_is_a_required_keyword() -> None:
    with pytest.raises(TypeError):
        run_alembic("upgrade", "head")


def test_refusal_never_echoes_the_url_or_password() -> None:
    secret = "s3cr3t-password"
    target = f"postgresql+asyncpg://kai_test:{secret}@127.0.0.1:55432/kai"
    with pytest.raises(RuntimeError) as excinfo:
        run_alembic("upgrade", "head", target_url=target)
    message = str(excinfo.value)
    assert secret not in message
    assert target not in message


def test_runner_does_not_escape_percent_for_the_child(
    alembic_spy: _RunRecorder,
) -> None:
    target = (
        "postgresql+asyncpg://kai_test:p%25ss@127.0.0.1:55432/"
        f"kai_test_migrations_{_RUN_ID}"
    )
    run_alembic("upgrade", "head", target_url=target)
    env = alembic_spy.calls[0][1]["env"]
    assert isinstance(env, dict)
    assert env["DATABASE_URL"] == target


@pytest.mark.parametrize("target", [None, "", "   "])
def test_require_explicit_database_url_refuses_missing(target: str | None) -> None:
    with pytest.raises(ValueError, match="DATABASE_URL is not set"):
        require_explicit_database_url(target)


def test_require_explicit_database_url_refuses_unparsable() -> None:
    with pytest.raises(ValueError, match="not a valid database URL"):
        require_explicit_database_url("not-a-url")


@pytest.mark.parametrize(
    "target",
    [
        _SAFE_URL,
        _SAFE_PSYCOPG_URL,
        "postgresql+asyncpg://kai:kai@127.0.0.1:5432/kai",
        "postgresql+psycopg://other_user:pw@db.internal:5432/production",
    ],
)
def test_require_explicit_database_url_accepts_supported_async_targets(
    target: str,
) -> None:
    """Development and production targets are allowed when stated explicitly.

    The test-name allowlist belongs to the migration test runner; env.py only
    refuses to guess a target. The URL must still select a driver the
    AsyncEngine can use.
    """
    assert require_explicit_database_url(target) == target


@pytest.mark.parametrize(
    "target",
    [
        "postgresql://kai_test:kai@127.0.0.1:55432/kai_test",
        "postgresql+psycopg2://kai_test:kai@127.0.0.1:55432/kai_test",
        "sqlite:///tmp/kai_test.db",
    ],
)
def test_require_explicit_database_url_refuses_non_async_drivers(target: str) -> None:
    with pytest.raises(ValueError, match="not a supported async driver"):
        require_explicit_database_url(target)


def test_require_explicit_database_url_message_has_no_secret() -> None:
    target = "s3cr3t is not a database url"
    with pytest.raises(ValueError) as excinfo:
        require_explicit_database_url(target)
    assert "s3cr3t" not in str(excinfo.value)


def test_percent_escape_round_trips_through_alembic_config() -> None:
    for target in (
        "postgresql+asyncpg://kai_test:p%25ss@127.0.0.1:55432/kai_test",
        "postgresql+asyncpg://kai_test:p%ss@127.0.0.1:55432/kai_test",
        "postgresql+asyncpg://kai_test:p%(x)s@127.0.0.1:55432/kai_test",
    ):
        config = Config()
        config.set_main_option("sqlalchemy.url", escape_configparser_percent(target))
        assert config.get_main_option("sqlalchemy.url") == target


def test_env_py_refuses_before_creating_an_engine() -> None:
    """Static ordering check: no import of env.py, so no migration runs.

    The refusal must execute at module level before the migration functions and
    before ``async_engine_from_config``; ``get_settings`` must not appear at all,
    because that is the default target this change removes.
    """
    source = _ENV_PY.read_text(encoding="utf-8")
    refusal = source.index("require_explicit_database_url")
    assert "get_settings" not in source
    assert refusal < source.index("def run_migrations_offline")
    assert refusal < source.index("connectable = async_engine_from_config")
    # ``kai.runtime`` builds the application's global engine at import time, so
    # its model import must also come after the refusal.
    assert refusal < source.index("from kai.runtime import models")
    assert refusal < source.index("\n_register_models()")
    assert "escape_configparser_percent(database_url)" in source


@pytest.mark.parametrize("target", [_NORMAL_URL, _NORMAL_PSYCOPG_URL])
def test_migration_target_error_accepts_supported_async_drivers(target: str) -> None:
    assert migration_target_error(target) is None


@pytest.mark.parametrize(
    "target",
    [
        "postgresql://kai_test:kai@127.0.0.1:55432/kai_test",
        "postgresql+psycopg2://kai_test:kai@127.0.0.1:55432/kai_test",
    ],
)
def test_migration_target_error_refuses_sync_postgresql_drivers(target: str) -> None:
    error = migration_target_error(target)
    assert error is not None
    assert "not a supported async driver" in error


@pytest.mark.parametrize("target", [_SAFE_URL, _SAFE_PSYCOPG_URL])
def test_migration_run_target_accepts_exact_per_run_name(target: str) -> None:
    assert migration_run_target_error(target) is None
    assert migration_run_target_error(target, _RUN_ID) is None


@pytest.mark.parametrize(
    "target",
    [
        _NORMAL_URL,
        "postgresql+asyncpg://kai_test:kai@127.0.0.1:55432/kai_test_migrations",
        "postgresql+asyncpg://kai_test:kai@127.0.0.1:55432/kai_test_migrations_A1B2C3D4E5F60718",
        "postgresql+asyncpg://kai_test:kai@127.0.0.1:55432/kai_test_migrations_a1b2c3d4e5f6071",
    ],
)
def test_migration_run_target_rejects_shared_or_inexact_names(target: str) -> None:
    assert migration_run_target_error(target) is not None


def test_migration_run_target_rejects_another_runs_database() -> None:
    assert migration_run_target_error(_SAFE_URL, "ffffffffffffffff") is not None


def test_build_migration_database_url_changes_only_the_database() -> None:
    source = (
        "postgresql+asyncpg://kai_test:p%25ss@127.0.0.1:55432/kai_test"
        "?ssl=disable"
    )
    built = build_migration_database_url(
        source, f"kai_test_migrations_{_RUN_ID}"
    )
    assert built.startswith("postgresql+asyncpg://kai_test:p%25ss@127.0.0.1:55432/")
    assert built.endswith(f"/kai_test_migrations_{_RUN_ID}?ssl=disable")
    assert migration_run_target_error(built) is None


def test_child_environment_is_an_allowlist_not_a_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KAI_TEST_POSTGRES_ADMIN_PASSWORD", "admin-secret")
    monkeypatch.setenv("KAI_TEST_POSTGRES_ADMIN_USER", "kai_test_admin")
    monkeypatch.setenv("PGPASSWORD", "pg-secret")
    monkeypatch.setenv("PATH", "C:\\Windows")
    env = build_child_env(_SAFE_URL)
    assert env["DATABASE_URL"] == _SAFE_URL
    assert env["PATH"] == "C:\\Windows"
    for forbidden in (
        "KAI_TEST_POSTGRES_ADMIN_PASSWORD",
        "KAI_TEST_POSTGRES_ADMIN_USER",
        "PGPASSWORD",
        "KAI_TEST_ALLOW_DB_TRUNCATE",
    ):
        assert forbidden not in env


def test_pytest_child_environment_has_only_target_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KAI_TEST_POSTGRES_ADMIN_PASSWORD", "admin-secret")
    monkeypatch.setenv("KAI_TEST_ALLOW_DB_TRUNCATE", "1")
    env = build_child_env(
        _SAFE_URL, include_migration_context=True, run_id=_RUN_ID
    )
    assert env["DATABASE_URL"] == _SAFE_URL
    assert env["KAI_TEST_MIGRATIONS_DATABASE_URL"] == _SAFE_URL
    assert env["KAI_TEST_MIGRATIONS_RUN_ID"] == _RUN_ID
    assert "KAI_TEST_POSTGRES_ADMIN_PASSWORD" not in env
    assert "KAI_TEST_ALLOW_DB_TRUNCATE" not in env


def test_timeout_is_explicit_and_does_not_echo_the_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _timeout(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd="alembic", timeout=120)

    monkeypatch.setattr("kai.db.migration_subprocess.subprocess.run", _timeout)
    with pytest.raises(AlembicTimeout) as excinfo:
        run_alembic("upgrade", "head", target_url=_SAFE_URL)
    message = str(excinfo.value)
    assert "untrusted" in message
    assert _SAFE_URL not in message
    assert "kai" not in message


def test_sanitize_text_removes_urls_and_known_passwords() -> None:
    text = (
        f"failed to connect {_SAFE_URL} password=topsecret "
        "and postgresql://other:otherpw@db.example/prod"
    )
    cleaned = sanitize_text(text, _SAFE_URL, "topsecret", "otherpw")
    assert _SAFE_URL not in cleaned
    assert "topsecret" not in cleaned
    assert "otherpw" not in cleaned
    assert "postgresql://" not in cleaned
    assert "<redacted" in cleaned
