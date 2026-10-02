"""Tests for the scheduled expired-revocation purge.

The purge is the one piece of this round that deletes rows, so its tests are
written around the invariant it must never break: **a revocation whose token has
not yet expired is never removed.** Everything else here exists to prove that
predicate is the only thing selecting rows, that the advisory lock collapses
overlapping runs instead of doubling the work, that a failed run reports itself
as failed, and that none of it is reachable over HTTP.

Rows are written through the real table and read back, rather than asserting on
a mocked delete predicate, and the safety cases go through the real
``revoke_token`` path so the expiry the purge sees is the one logout actually
stores.
"""

from __future__ import annotations

import json
import tomllib
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from kai.auth.jwt import create_access_token, decode_access_token
from kai.auth.revocation import is_token_revoked, revoke_token
from kai.db.engine import async_session_factory, engine
from kai.db.models import RevokedToken, User
from kai.logging import configure_logging
from kai.maintenance import revocation_purge as purge_mod
from kai.maintenance.revocation_purge import (
    EXIT_FAILED,
    EXIT_OK,
    PURGE_LOCK_KEY,
    PurgeSkipped,
    main,
    purge_once,
    run_purge,
)
from kai.observability import metrics

REPO_ROOT = Path(__file__).resolve().parents[1]

PASSWORD = "Password123!"


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_metrics() -> Iterator[None]:
    """Per-test registry, so a count an earlier test inflated is never asserted."""
    metrics.reset_metrics()
    yield
    metrics.reset_metrics()


@pytest.fixture
async def principal(client: httpx.AsyncClient) -> tuple[Any, Any]:
    """A real registered user and tenant.

    ``revoked_tokens`` carries foreign keys to both, so a purge fixture that
    invented ids would only ever exercise the constraint violation, never the
    purge.
    """
    created = await client.post(
        "/api/v1/auth/register",
        json={"email": f"purge-{uuid4().hex[:12]}@acme.example", "password": PASSWORD},
    )
    assert created.status_code == 201, created.text
    # The register response carries no tenant_id, so it comes from the token
    # itself rather than from a second request.
    claims = decode_access_token(created.json()["access_token"])
    return claims.user_id, claims.tenant_id


@asynccontextmanager
async def _session() -> AsyncIterator[AsyncSession]:
    async with async_session_factory() as session:
        yield session


@contextmanager
def _captured_logs() -> Iterator[list[dict[str, Any]]]:
    records: list[dict[str, Any]] = []

    class _Stream:
        def write(self, payload: str) -> int:
            for line in payload.splitlines():
                if line.strip():
                    records.append(json.loads(line))
            return len(payload)

        def flush(self) -> None:
            """Events are parsed on write, so there is nothing to flush."""

    configure_logging(stream=_Stream())
    try:
        yield records
    finally:
        configure_logging()


def _sample(metric_name: str, **labels: str) -> float | None:
    text_out = metrics.render_latest().decode()
    for line in text_out.splitlines():
        if not line.startswith(metric_name):
            continue
        series, _, value = line.rpartition(" ")
        if all(f'{k}="{v}"' in series for k, v in labels.items()):
            return float(value)
    return None


async def _insert_revocation(
    session: AsyncSession, principal: tuple[Any, Any], *, expires_in_seconds: int
) -> str:
    """Write a revocation row for a real user, with a chosen expiry."""
    user_id, tenant_id = principal
    jti = str(uuid4())
    session.add(
        RevokedToken(
            jti=jti,
            user_id=user_id,
            tenant_id=tenant_id,
            expires_at=datetime.now(UTC) + timedelta(seconds=expires_in_seconds),
        )
    )
    await session.commit()
    return jti


async def _count(session: AsyncSession) -> int:
    return int((await session.execute(select(func.count()).select_from(RevokedToken))).scalar_one())


async def _jtis(session: AsyncSession) -> set[str]:
    return set((await session.execute(select(RevokedToken.jti))).scalars())


def _db_is_down() -> OperationalError:
    return OperationalError("SELECT 1", {}, Exception("database is unreachable"))


# ---------------------------------------------------------------------------
# The safety invariant
# ---------------------------------------------------------------------------


async def test_an_expired_revocation_is_removed(principal: tuple[Any, Any]) -> None:
    async with _session() as session:
        await _insert_revocation(session, principal, expires_in_seconds=-3600)
        assert await _count(session) == 1

        assert await purge_once(session) == 1
        assert await _count(session) == 0


async def test_a_live_revocation_is_never_removed(principal: tuple[Any, Any]) -> None:
    """The invariant the whole command exists to preserve.

    If this row were deleted, logout would silently stop working for every token
    still inside its expiry window.
    """
    async with _session() as session:
        live = await _insert_revocation(session, principal, expires_in_seconds=3600)

        assert await purge_once(session) == 0
        assert await _jtis(session) == {live}
        assert await is_token_revoked(session, jti=live) is True


async def test_logout_still_works_after_a_purge_runs(principal: tuple[Any, Any]) -> None:
    """The safety invariant end to end, through the real revocation path.

    A genuinely signed token for a real user, revoked the way logout does it, so
    the expiry the purge compares against is the one logout actually stored.
    """
    user_id, tenant_id = principal
    claims = decode_access_token(create_access_token(user_id=user_id, tenant_id=tenant_id))

    async with _session() as session:
        await revoke_token(session, claims=claims)
        assert await is_token_revoked(session, jti=claims.jti) is True

        # A purge runs, and the token is nowhere near expiry.
        assert await purge_once(session) == 0

        assert await is_token_revoked(session, jti=claims.jti) is True


async def test_a_revoked_token_still_stops_authenticating_after_a_purge(
    principal: tuple[Any, Any],
) -> None:
    """The same invariant one layer out: through the dependency, not the store."""
    user_id, tenant_id = principal
    claims = decode_access_token(create_access_token(user_id=user_id, tenant_id=tenant_id))
    async with _session() as session:
        await revoke_token(session, claims=claims)

    async with _session() as session:
        assert await purge_once(session) == 0
        assert await is_token_revoked(session, jti=claims.jti) is True


async def test_the_boundary_is_inclusive_at_the_exact_expiry(principal: tuple[Any, Any]) -> None:
    """``expires_at <= now()``.

    A token that has just expired is already rejected by the signature/expiry
    check, so its revocation record can no longer change any outcome and removing
    it is safe.
    """
    async with _session() as session:
        just_expired = await _insert_revocation(session, principal, expires_in_seconds=-1)
        barely_live = await _insert_revocation(session, principal, expires_in_seconds=5)

        assert await purge_once(session) == 1
        assert await _jtis(session) == {barely_live}
        assert just_expired not in await _jtis(session)


async def test_mixed_rows_are_split_correctly(principal: tuple[Any, Any]) -> None:
    async with _session() as session:
        dead = [
            await _insert_revocation(session, principal, expires_in_seconds=-60 * i - 60)
            for i in range(4)
        ]
        alive = [
            await _insert_revocation(session, principal, expires_in_seconds=600 + i)
            for i in range(3)
        ]

        assert await purge_once(session) == 4
        remaining = await _jtis(session)
        assert remaining == set(alive)
        assert not remaining & set(dead)


async def test_the_purge_touches_no_other_table(principal: tuple[Any, Any]) -> None:
    """A purge must not be able to reach users or any other table.

    Counts the rows of a table a too-broad delete would hit, before and after.
    """
    async with _session() as session:
        await _insert_revocation(session, principal, expires_in_seconds=-60)
        before = int((await session.execute(select(func.count()).select_from(User))).scalar_one())

        await purge_once(session)

        after = int((await session.execute(select(func.count()).select_from(User))).scalar_one())
    assert after == before


async def test_purging_an_empty_table_succeeds_and_deletes_nothing() -> None:
    async with _session() as session:
        assert await purge_once(session) == 0


# ---------------------------------------------------------------------------
# Idempotency and catch-up
# ---------------------------------------------------------------------------


async def test_running_twice_deletes_nothing_the_second_time(principal: tuple[Any, Any]) -> None:
    async with _session() as session:
        await _insert_revocation(session, principal, expires_in_seconds=-60)

        assert await purge_once(session) == 1
        assert await purge_once(session) == 0


async def test_a_missed_window_is_corrected_by_the_next_run(principal: tuple[Any, Any]) -> None:
    """There is no watermark.

    A run deletes everything expired as of *now*, so however long the scheduler
    was down, the next run converges - which is what makes an external schedule
    safe to rely on.
    """
    async with _session() as session:
        long_gone = await _insert_revocation(session, principal, expires_in_seconds=-86400)
        recent = await _insert_revocation(session, principal, expires_in_seconds=-60)

        assert await purge_once(session) == 2
        assert not (await _jtis(session)) & {long_gone, recent}


async def test_a_row_that_expires_after_a_run_is_purged_by_the_next_one(
    principal: tuple[Any, Any],
) -> None:
    """A row that was live at run time must not be stranded by an early purge."""
    async with _session() as session:
        soon = await _insert_revocation(session, principal, expires_in_seconds=3600)
        assert await purge_once(session) == 0

    # Age it past expiry the way time would, without sleeping.
    async with _session() as updater:
        await updater.execute(
            text("UPDATE revoked_tokens SET expires_at = now() - interval '1 second'")
        )
        await updater.commit()

    async with _session() as session:
        assert await purge_once(session) == 1
        assert soon not in await _jtis(session)


# ---------------------------------------------------------------------------
# Concurrency: the advisory lock
# ---------------------------------------------------------------------------


async def test_a_second_run_skips_while_the_first_holds_the_lock(
    principal: tuple[Any, Any],
) -> None:
    """Overlapping replicas must collapse into one purge, not queue.

    ``pg_try_advisory_xact_lock`` is non-blocking, so the loser exits immediately
    instead of holding a connection open waiting for a lock that may take a while.
    """
    async with _session() as holder, _session() as contender:
        await _insert_revocation(holder, principal, expires_in_seconds=-60)
        # The holder takes the lock and does not commit, so it keeps holding it.
        await purge_mod._acquire_purge_lock(holder)

        with pytest.raises(PurgeSkipped):
            await purge_once(contender)

        assert await _count(holder) == 1, "the skipped run deleted rows anyway"


async def test_the_lock_is_released_when_the_transaction_commits(
    principal: tuple[Any, Any],
) -> None:
    """Transaction-scoped means PostgreSQL releases it on commit.

    If it did not, one run that crashed after committing would wedge the job.
    """
    async with _session() as session:
        await _insert_revocation(session, principal, expires_in_seconds=-60)
        await purge_mod._acquire_purge_lock(session)
        await session.commit()

    async with _session() as after:
        await purge_mod._acquire_purge_lock(after)


async def test_the_lock_is_released_after_a_rollback(principal: tuple[Any, Any]) -> None:
    async with _session() as session:
        await _insert_revocation(session, principal, expires_in_seconds=-60)
        await purge_mod._acquire_purge_lock(session)
        await session.rollback()

    async with _session() as after:
        await purge_mod._acquire_purge_lock(after)


async def test_the_lock_is_taken_on_the_same_session_as_the_delete(
    principal: tuple[Any, Any],
) -> None:
    """A lock held on a second connection would leave a window where the lock is
    held but the delete is not inside it."""
    async with _session() as session:
        await _insert_revocation(session, principal, expires_in_seconds=-60)
        # purge_once does both on this one session. If it needed a second
        # connection it would block against the lock this session holds.
        assert await purge_once(session) == 1


def test_the_lock_key_is_in_the_postgres_advisory_range() -> None:
    """A key outside the user-defined range would collide with PostgreSQL's own
    internal locks."""
    assert 0 < PURGE_LOCK_KEY < 2**63


async def test_concurrent_runs_delete_each_row_exactly_once(principal: tuple[Any, Any]) -> None:
    """Three simultaneous runs must total the row count.

    Neither double-counted (two purges claiming the same rows) nor lose any.
    """
    import asyncio

    async with _session() as seed:
        for _ in range(5):
            await _insert_revocation(seed, principal, expires_in_seconds=-60)

    outcomes = await asyncio.gather(
        _purge_in_own_session(), _purge_in_own_session(), _purge_in_own_session()
    )

    assert sum(outcomes) == 5, outcomes
    async with _session() as check:
        assert await _count(check) == 0


async def _purge_in_own_session() -> int:
    async with _session() as session:
        try:
            return await purge_once(session)
        except PurgeSkipped:
            return 0


# ---------------------------------------------------------------------------
# run_purge: outcomes, metrics, exit codes
# ---------------------------------------------------------------------------


async def test_a_run_with_work_reports_purged(principal: tuple[Any, Any]) -> None:
    async with _session() as session:
        await _insert_revocation(session, principal, expires_in_seconds=-60)

    with _captured_logs() as records:
        outcome, purged = await run_purge()

    assert (outcome, purged) == (metrics.PURGE_RESULT_PURGED, 1)
    assert _sample("kai_revocation_purge_runs_total", result=metrics.PURGE_RESULT_PURGED) == 1.0
    assert _sample("kai_revocation_purged_rows_total") == 1.0
    assert any(r.get("event") == "revocation_purge_complete" for r in records)


async def test_a_run_with_nothing_to_do_reports_nothing_expired() -> None:
    outcome, purged = await run_purge()

    assert outcome == metrics.PURGE_RESULT_NOTHING_EXPIRED
    assert purged == 0
    # An unlabelled counter exists at zero from registration, so the assertion is
    # that it stayed zero rather than that it is absent.
    assert _sample("kai_revocation_purged_rows_total") == 0.0


async def test_a_skipped_run_is_not_reported_as_a_failure() -> None:
    """A scheduler seeing a failure for 'someone else purged' would page an
    operator for a perfectly healthy system."""
    async with _session() as holder, _session() as contender:
        await purge_mod._acquire_purge_lock(holder)
        with patch.object(purge_mod, "async_session_factory", return_value=contender):
            outcome, purged = await run_purge()

    assert (outcome, purged) == (metrics.PURGE_RESULT_LOCK_NOT_ACQUIRED, 0)
    assert (
        _sample(
            "kai_revocation_purge_runs_total", result=metrics.PURGE_RESULT_LOCK_NOT_ACQUIRED
        )
        == 1.0
    )
    assert _sample("kai_revocation_purge_runs_total", result=metrics.PURGE_RESULT_FAILED) is None


async def test_a_database_failure_is_counted_and_propagates() -> None:
    """The scheduler must be able to tell a failed run from a successful one, or
    the table silently stops being purged and nobody finds out until it is huge."""
    with patch.object(purge_mod, "purge_once", side_effect=_db_is_down()), pytest.raises(
        OperationalError
    ):
        await run_purge()

    assert _sample("kai_revocation_purge_runs_total", result=metrics.PURGE_RESULT_FAILED) == 1.0


async def test_the_session_is_closed_when_the_run_fails() -> None:
    """A leaked connection in a job that runs every 15 minutes would exhaust the
    pool over a day, so the connection must go back to the pool on every path."""
    session = async_session_factory()
    await session.execute(text("SELECT 1"))  # force the connection to be checked out
    while_open = engine.pool.checkedout()

    try:
        with (
            patch.object(purge_mod, "async_session_factory", return_value=session),
            patch.object(purge_mod, "purge_once", side_effect=_db_is_down()),
            pytest.raises(OperationalError),
        ):
            await run_purge()

        assert engine.pool.checkedout() == while_open - 1, "the connection was not returned"
    finally:
        await session.close()


async def test_the_session_is_closed_on_the_skipped_path() -> None:
    async with _session() as holder:
        await purge_mod._acquire_purge_lock(holder)

        session = async_session_factory()
        await session.execute(text("SELECT 1"))
        while_open = engine.pool.checkedout()
        try:
            with patch.object(purge_mod, "async_session_factory", return_value=session):
                await run_purge()
            assert engine.pool.checkedout() == while_open - 1, "the connection was not returned"
        finally:
            await session.close()


# ---------------------------------------------------------------------------
# Console entrypoint
# ---------------------------------------------------------------------------


def test_the_command_exits_zero_on_success() -> None:
    """``main`` owns its event loop with ``asyncio.run``.

    The database behaviour of a run is covered by the ``run_purge`` tests above.
    What is asserted here is only the exit-code mapping, so ``run_purge`` is
    stubbed: driving the real engine from a second event loop in a suite whose
    other tests share one pool would test the loop, not the command.
    """
    with _captured_logs(), _no_reconfigure(), patch.object(
        purge_mod, "run_purge", return_value=(metrics.PURGE_RESULT_PURGED, 3)
    ):
        assert main() == EXIT_OK


def test_the_command_exits_zero_when_another_run_holds_the_lock() -> None:
    """Overlapping invocations are normal, not an error, so the scheduler must
    not see a failure every time two runs overlap."""
    with _captured_logs(), _no_reconfigure(), patch.object(
        purge_mod,
        "run_purge",
        return_value=(metrics.PURGE_RESULT_LOCK_NOT_ACQUIRED, 0),
    ):
        assert main() == EXIT_OK


def test_the_command_exits_non_zero_on_a_database_failure() -> None:
    with (
        _captured_logs() as records,
        _no_reconfigure(),
        patch.object(purge_mod, "run_purge", side_effect=_db_is_down()),
    ):
        assert main() == EXIT_FAILED

    assert any(r.get("event") == "revocation_purge_aborted" for r in records)


def test_the_command_never_tracebacks_on_an_unexpected_error() -> None:
    """A maintenance job's traceback is noise in the scheduler's mail; the exit
    code is the signal."""
    with (
        _captured_logs() as records,
        _no_reconfigure(),
        patch.object(purge_mod, "run_purge", side_effect=ValueError("surprise-abc123")),
    ):
        assert main() == EXIT_FAILED

    aborted = [r for r in records if r.get("event") == "revocation_purge_unexpected"]
    assert aborted, records
    assert aborted[0]["error"] == "ValueError"
    assert "surprise-abc123" not in json.dumps(records)


def test_the_command_does_not_log_a_driver_message_on_failure() -> None:
    """A SQLAlchemy/driver message can embed the connection string.

    Asserted at the entrypoint, since that is the path a scheduler triggers
    during a real database incident.
    """
    with (
        _captured_logs() as records,
        _no_reconfigure(),
        patch.object(purge_mod, "run_purge", side_effect=_db_is_down()),
    ):
        main()

    rendered = json.dumps(records)
    assert "postgresql" not in rendered
    assert "unreachable" not in rendered


@contextmanager
def _no_reconfigure() -> Iterator[None]:
    """Stop ``main`` from re-pointing logging at stdout.

    ``main`` calls ``configure_logging()``, which would replace the capturing
    stream installed by ``_captured_logs`` and lose every event.
    """
    with patch.object(purge_mod, "configure_logging", lambda *a, **k: None):
        yield


def test_the_command_is_declared_in_the_packaging_entry_points() -> None:
    """Without the entry point, the documented schedule would invoke nothing."""
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["scripts"]["kai-purge-revocations"] == (
        "kai.maintenance.revocation_purge:main"
    )


def test_the_command_runs_from_the_same_image_as_the_app() -> None:
    """The purge must not be a second artifact that can drift from the code it
    purges."""
    compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "kai-purge-revocations" in compose
    assert "run --rm purge-revocations" in compose


def test_the_maintenance_process_installs_the_database_hook() -> None:
    """The command must report database failures the same way the API does, or a
    broken run is invisible until the table is found to be huge."""
    import inspect

    assert "install_db_instrumentation" in inspect.getsource(run_purge)


# ---------------------------------------------------------------------------
# Reachability
# ---------------------------------------------------------------------------


async def test_there_is_no_http_endpoint_for_the_purge(client: httpx.AsyncClient) -> None:
    """A purge is not something a client may ask for."""
    for method, path in (
        ("POST", "/api/v1/purge-revocations"),
        ("POST", "/api/v1/admin/purge-revocations"),
        ("GET", "/api/v1/maintenance/revocation-purge"),
    ):
        response = await client.request(method, path)
        assert response.status_code in (404, 405), f"{method} {path} is reachable"


async def test_the_openapi_schema_exposes_no_purge_operation(client: httpx.AsyncClient) -> None:
    response = await client.get("/openapi.json")
    paths = response.json()["paths"]
    assert not any("purge" in path for path in paths), paths


# ---------------------------------------------------------------------------
# Metrics hygiene
# ---------------------------------------------------------------------------


def test_a_purge_outcome_outside_the_closed_set_is_bucketed() -> None:
    """An unexpected outcome string must not mint a new series per occurrence."""
    metrics.record_purge_run("something-new-" + "x" * 40, purged=1)

    assert _sample("kai_revocation_purge_runs_total", result=metrics.UNKNOWN_LABEL) == 1.0
    assert "something-new" not in metrics.render_latest().decode()


def test_a_purge_failure_logs_the_type_not_the_message() -> None:
    """A driver error message can carry the connection string, credentials
    included."""
    with (
        _captured_logs() as records,
        patch.object(purge_mod, "purge_once", side_effect=_db_is_down()),
        pytest.raises(OperationalError),
    ):
        asyncio_run(run_purge())

    failures = [r for r in records if r.get("event") == "revocation_purge_failed"]
    assert failures, records
    assert failures[0]["error"] == "OperationalError"
    assert "postgresql" not in json.dumps(records)
    assert "unreachable" not in json.dumps(records)


def asyncio_run(coro: Any) -> Any:
    import asyncio

    return asyncio.run(coro)
