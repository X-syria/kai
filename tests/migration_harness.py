"""Shared harness for the isolated Alembic migration test group.

Every helper here takes the migration target explicitly.  There is deliberately
no ``get_settings().database_url`` fallback: the normal test database and the
per-run migration database must never be confused.

The module is safe to import without a database.  It opens a connection only
when a caller invokes :func:`table_names` or one of the schema verification
helpers, which the unit tests replace or avoid.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.engine.url import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from alembic_runner import AlembicTimeout, run_alembic_checked
from kai.db.target_policy import migration_run_target_error

MIGRATION_MARKER = "db_migration"
MIGRATION_DB_NAME_PREFIX = "kai_test_migrations_"
_REVISION_RE = re.compile(r"^([0-9a-f]{12,})(?:\s|$)")
_ROOT = Path(__file__).resolve().parents[1]
_EXPECTED_TABLES = (
    "alembic_version",
    "tenants",
    "users",
    "tasks",
    "task_plans",
    "memory_entries",
    "runtime_executions",
    "approvals",
    "alerts",
)


class MigrationConfigurationError(RuntimeError):
    """The migration target/context is missing, ambiguous, or unsafe."""


class MigrationRestoreError(RuntimeError):
    """The schema could not be proven restored to the repository head."""


class MigrationGroupAborted(RuntimeError):
    """A previous failure left the migration database untrusted."""


@dataclass
class MigrationGroupState:
    """Session-scoped trust state for one migration pytest process."""

    dirty: bool = False
    reason: str = ""

    def mark_dirty(self, reason: str) -> None:
        self.dirty = True
        self.reason = reason

    def ensure_clean(self) -> None:
        if self.dirty:
            raise MigrationGroupAborted(
                "migration database is untrusted; refusing to run another"
                f" migration test ({self.reason})"
            )


@dataclass(frozen=True)
class SchemaBaseline:
    """Read-only schema identity used to prove the normal DB did not move."""

    revision: str
    tables: tuple[str, ...] = field(default_factory=tuple)


def migration_target_from_env(env: dict[str, str] | None = None) -> str:
    """Return the one explicit target shared by Alembic and the test helpers."""
    import os

    values = os.environ if env is None else env
    migration_url = values.get("KAI_TEST_MIGRATIONS_DATABASE_URL")
    database_url = values.get("DATABASE_URL")
    run_id = values.get("KAI_TEST_MIGRATIONS_RUN_ID")

    if not migration_url:
        raise MigrationConfigurationError(
            "KAI_TEST_MIGRATIONS_DATABASE_URL is required for migration tests"
        )
    if not database_url:
        raise MigrationConfigurationError(
            "DATABASE_URL is required for migration tests"
        )
    if not run_id:
        raise MigrationConfigurationError(
            "KAI_TEST_MIGRATIONS_RUN_ID is required for migration tests"
        )

    error = migration_run_target_error(migration_url, run_id)
    if error is not None:
        raise MigrationConfigurationError(error)

    try:
        migration = make_url(migration_url)
        declared = make_url(database_url)
    except Exception as exc:
        raise MigrationConfigurationError(
            "migration target is not a valid PostgreSQL URL"
        ) from exc

    if migration != declared:
        raise MigrationConfigurationError(
            "DATABASE_URL and KAI_TEST_MIGRATIONS_DATABASE_URL do not name"
            " the same target"
        )
    return migration_url


def current_head() -> str:
    """Read the repository head without opening a database."""
    config = Config(str(_ROOT / "alembic.ini"))
    script = ScriptDirectory.from_config(config)
    head = script.get_current_head()
    if head is None:
        raise MigrationConfigurationError("Alembic repository has no head revision")
    return head


def _revision_from_current(stdout: str) -> str | None:
    for line in stdout.splitlines():
        match = _REVISION_RE.match(line.strip())
        if match:
            return match.group(1)
    return None


def verify_at_head(
    target_url: str,
    *,
    expected_head: str | None = None,
    alembic: Callable[..., object] = run_alembic_checked,
) -> str:
    """Require ``alembic current`` to report exactly the repository head."""
    head = expected_head or current_head()
    result = alembic("current", target_url=target_url)
    stdout = getattr(result, "stdout", "")
    actual = _revision_from_current(stdout)
    if actual != head:
        raise MigrationRestoreError(
            "alembic_version does not match the repository head;"
            " refusing to continue on an untrusted schema"
        )
    return actual


def table_names(target_url: str) -> tuple[str, ...]:
    """Inspect the public schema on a disposable engine/event loop."""
    from sqlalchemy import inspect as sa_inspect

    async def _names() -> tuple[str, ...]:
        engine = create_async_engine(target_url)
        try:
            async with engine.connect() as conn:
                names = await conn.run_sync(
                    lambda sync: sa_inspect(sync).get_table_names()
                )
                return tuple(names)
        finally:
            await engine.dispose()

    return asyncio.run(_names())


def verify_schema(
    target_url: str,
    *,
    expected_tables: tuple[str, ...] = _EXPECTED_TABLES,
    expected_head: str | None = None,
    alembic: Callable[..., object] = run_alembic_checked,
    inspect_tables: Callable[[str], tuple[str, ...]] = table_names,
) -> None:
    """Require the expected revision and tables before/after the group."""
    verify_at_head(target_url, expected_head=expected_head, alembic=alembic)
    present = set(inspect_tables(target_url))
    missing = sorted(set(expected_tables) - present)
    if missing:
        raise MigrationRestoreError(
            "migration schema is missing expected tables: " + ", ".join(missing)
        )


def restore_to_head(
    target_url: str,
    *,
    expected_head: str | None = None,
    alembic: Callable[..., object] = run_alembic_checked,
) -> None:
    """Restore head and prove it.  A non-zero result is never ignored."""
    head = expected_head or current_head()
    try:
        result = alembic("upgrade", "head", target_url=target_url)
    except AlembicTimeout as exc:
        raise MigrationRestoreError(
            "schema restore timed out; target is untrusted"
        ) from exc
    except Exception as exc:
        raise MigrationRestoreError(
            "schema restore failed before completing; target is untrusted"
        ) from exc

    returncode = getattr(result, "returncode", 1)
    if returncode != 0:
        raise MigrationRestoreError(
            f"schema restore exited with code {returncode}; target is untrusted"
        )

    try:
        verify_at_head(target_url, expected_head=head, alembic=alembic)
    except AlembicTimeout as exc:
        raise MigrationRestoreError(
            "schema verification timed out; target is untrusted"
        ) from exc
    except Exception as exc:
        raise MigrationRestoreError(
            "schema could not be verified at head; target is untrusted"
        ) from exc


@contextmanager
def migration_schema(
    state: MigrationGroupState,
    target_url: str,
    *,
    alembic: Callable[..., object] = run_alembic_checked,
) -> Iterator[None]:
    """Run one mutating test and always restore head, or abort the group.

    A timeout in the test body is not followed by an automatic second Alembic
    command: the schema state is unknown, so the group is marked dirty instead.
    """
    state.ensure_clean()
    try:
        yield
    except AlembicTimeout:
        state.mark_dirty("Alembic timed out during the test body")
        raise
    except BaseException as original:
        try:
            restore_to_head(target_url, alembic=alembic)
        except BaseException:
            state.mark_dirty("schema restore failed")
            raise MigrationRestoreError(
                "schema restore failed after a test error; target is untrusted"
            ) from original
        raise
    else:
        try:
            restore_to_head(target_url, alembic=alembic)
        except BaseException as restore_error:
            state.mark_dirty("schema restore failed")
            raise MigrationRestoreError(
                "schema restore failed; target is untrusted"
            ) from restore_error


def validate_migration_selection(
    *,
    has_migration: bool,
    has_normal: bool,
    num_processes: int | None = None,
) -> None:
    """Reject mixed groups and xdist, without touching a database."""
    if has_migration and has_normal:
        raise MigrationConfigurationError(
            "run migration tests separately: use -m db_migration or"
            ' -m "not db_migration"'
        )
    if has_migration and num_processes not in (None, 0, 1):
        raise MigrationConfigurationError(
            "db_migration tests must not run with xdist/parallel workers"
        )
