"""Unit tests for the migration test harness (no database, no Alembic process)."""

from __future__ import annotations

import subprocess

import pytest

from alembic_runner import AlembicTimeout
from migration_harness import (
    MigrationConfigurationError,
    MigrationGroupAborted,
    MigrationGroupState,
    MigrationRestoreError,
    current_head,
    migration_schema,
    migration_target_from_env,
    restore_to_head,
    validate_migration_selection,
    verify_at_head,
)

pytestmark = pytest.mark.db_guard_unit

_RUN_ID = "a1b2c3d4e5f60718"
_URL = (
    "postgresql+asyncpg://kai_test:kai@127.0.0.1:55432/"
    f"kai_test_migrations_{_RUN_ID}"
)


class _Alembic:
    def __init__(self, *, returncode: int = 0, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        return subprocess.CompletedProcess(
            args=list(args), returncode=self.returncode, stdout=self.stdout
        )


def _head_stdout() -> str:
    return f"{current_head()} (head)\n"


def test_target_requires_both_explicit_environment_values() -> None:
    with pytest.raises(MigrationConfigurationError, match="KAI_TEST_MIGRATIONS"):
        migration_target_from_env({})
    with pytest.raises(MigrationConfigurationError, match="DATABASE_URL"):
        migration_target_from_env({"KAI_TEST_MIGRATIONS_DATABASE_URL": _URL})
    with pytest.raises(MigrationConfigurationError, match="RUN_ID"):
        migration_target_from_env(
            {
                "KAI_TEST_MIGRATIONS_DATABASE_URL": _URL,
                "DATABASE_URL": _URL,
            }
        )


def test_target_rejects_mismatched_environment_values() -> None:
    with pytest.raises(MigrationConfigurationError, match="same target"):
        migration_target_from_env(
            {
                "KAI_TEST_MIGRATIONS_DATABASE_URL": _URL,
                "DATABASE_URL": "postgresql+asyncpg://kai_test:kai@127.0.0.1:55432/kai_test",
                "KAI_TEST_MIGRATIONS_RUN_ID": _RUN_ID,
            }
        )


def test_target_rejects_another_runs_database() -> None:
    with pytest.raises(MigrationConfigurationError, match="does not match"):
        migration_target_from_env(
            {
                "KAI_TEST_MIGRATIONS_DATABASE_URL": _URL,
                "DATABASE_URL": _URL,
                "KAI_TEST_MIGRATIONS_RUN_ID": "ffffffffffffffff",
            }
        )


def test_target_rejects_shared_database() -> None:
    normal = "postgresql+asyncpg://kai_test:kai@127.0.0.1:55432/kai_test"
    with pytest.raises(MigrationConfigurationError, match="per-run"):
        migration_target_from_env(
            {
                "KAI_TEST_MIGRATIONS_DATABASE_URL": normal,
                "DATABASE_URL": normal,
                "KAI_TEST_MIGRATIONS_RUN_ID": _RUN_ID,
            }
        )


def test_verify_at_head_accepts_only_the_repository_head() -> None:
    fake = _Alembic(stdout=_head_stdout())
    assert verify_at_head(_URL, alembic=fake) == current_head()

    fake = _Alembic(stdout="deadbeefcafe (head)\n")
    with pytest.raises(MigrationRestoreError, match="repository head"):
        verify_at_head(_URL, alembic=fake)


def test_restore_nonzero_is_a_hard_failure() -> None:
    fake = _Alembic(returncode=7)
    with pytest.raises(MigrationRestoreError, match="code 7"):
        restore_to_head(_URL, alembic=fake)
    assert fake.calls == [("upgrade", "head")]


def test_restore_timeout_is_a_hard_failure_without_a_second_attempt() -> None:
    calls: list[tuple[str, ...]] = []

    def _timeout(*args: str, **kwargs: object) -> None:
        calls.append(args)
        raise AlembicTimeout("timed out")

    with pytest.raises(MigrationRestoreError, match="timed out"):
        restore_to_head(_URL, alembic=_timeout)
    assert calls == [("upgrade", "head")]


def test_context_restores_after_a_test_exception_and_reraises() -> None:
    fake = _Alembic(stdout=_head_stdout())
    state = MigrationGroupState()
    with pytest.raises(RuntimeError, match="test body"), migration_schema(
        state, _URL, alembic=fake
    ):
        raise RuntimeError("test body")
    assert state.dirty is False
    assert fake.calls == [("upgrade", "head"), ("current",)]


def test_timeout_in_test_body_does_not_attempt_a_second_command() -> None:
    calls: list[tuple[str, ...]] = []

    def _timeout(*args: str, **kwargs: object) -> None:
        calls.append(args)
        raise AlembicTimeout("timed out")

    state = MigrationGroupState()
    with pytest.raises(AlembicTimeout), migration_schema(state, _URL, alembic=_timeout):
        raise AlembicTimeout("test command timed out")
    assert state.dirty is True
    assert calls == []


def test_failed_restore_marks_group_dirty_and_blocks_the_next_test() -> None:
    fake = _Alembic(returncode=9)
    state = MigrationGroupState()
    with pytest.raises(MigrationRestoreError), migration_schema(state, _URL, alembic=fake):
        pass
    assert state.dirty is True
    with pytest.raises(
        MigrationGroupAborted, match="untrusted"
    ), migration_schema(state, _URL, alembic=fake):
        pass


def test_selection_rejects_mixed_groups_and_parallel_migration_runs() -> None:
    validate_migration_selection(
        has_migration=True, has_normal=False, num_processes=1
    )
    validate_migration_selection(
        has_migration=False, has_normal=True, num_processes=4
    )
    with pytest.raises(MigrationConfigurationError, match="separately"):
        validate_migration_selection(
            has_migration=True, has_normal=True, num_processes=None
        )
    with pytest.raises(MigrationConfigurationError, match="xdist"):
        validate_migration_selection(
            has_migration=True, has_normal=False, num_processes=2
        )
