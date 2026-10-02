"""Scheduled housekeeping for ``revoked_tokens``.

Why this exists
---------------
``revoke_token`` records a signed token's ``jti`` so that logout takes effect
immediately rather than at ``exp``. Those rows are pure garbage once the token
would have expired anyway: the signature/expiry check already rejects it, so
the record can no longer change any outcome. Without a purge the table grows
without bound, one row per logout, forever.

Mechanism (decided, not incidental)
----------------------------------
* **External scheduler, one-shot command.** The application process does not own
  a timer and does not run a loop. An operator's scheduler (cron, systemd
  timer, Kubernetes CronJob) invokes ``kai-purge-revocations`` every 15
  minutes UTC and the process exits. A single-process scheduler would be
  simpler but would either tie the schedule to replica count or need its own
  leader election; an external one is one line of operator configuration and
  survives any change in how many replicas run.
* **Same image, no second artifact.** It is the KAI image running a different
  entrypoint, so it cannot drift from the code it purges.
* **Concurrency: a PostgreSQL transaction-scoped advisory lock.** Every run
  takes the same lock key. Exactly one run purges; the others observe the lock
  held and exit 0 without doing anything. The lock is transaction-scoped, so
  PostgreSQL releases it on commit *and* on rollback *and* on connection loss -
  there is no lock to leak and no recovery step if a purge is killed
  mid-transaction.
* **Idempotent.** Deleting rows that are already gone returns 0. A second run
  immediately after the first is a no-op.
* **Catch-up is free.** A run deletes everything expired *as of that moment*, so
  missed windows are corrected by the next one. There is no watermark and
  nothing to backfill.
* **No HTTP endpoint.** A purge is not a request a client may ask for.
* **Purge failure must not take down the API.** It is a separate process; a
  failure here exits non-zero for the scheduler to notice and leaves the
  running service untouched.

Safety
------
The delete predicate is unchanged: ``expires_at <= now()``. A revocation whose
token has not yet expired is never removed, so logout cannot silently stop
working. That is the one invariant this command exists to preserve.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Final

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from kai.auth.revocation import purge_expired_revocations
from kai.db.engine import async_session_factory, engine
from kai.logging import configure_logging, get_logger
from kai.observability import metrics
from kai.observability.db import install_db_instrumentation

_log = get_logger("kai.maintenance.revocation_purge")

#: Advisory-lock key for "the expired-revocation purge".
#:
#: PostgreSQL advisory locks are keyed by two int4s or one int8. This is one
#: int8 in the PostgreSQL user-defined-lock-key range, and it is KAI-specific so
#: it cannot collide with a lock some other component on the same database
#: happens to use. Changing it would let two differently-keyed purges run
#: concurrently, so it is a constant rather than a setting.
PURGE_LOCK_KEY: Final[int] = 0x4B41_495F_5055_5245

#: Process exit codes. Distinct so a scheduler can tell "someone else purged"
#: from "nothing to purge" from "this run failed".
EXIT_OK: Final[int] = 0
EXIT_FAILED: Final[int] = 1


class PurgeSkipped(Exception):
    """Another run holds the advisory lock; this one has nothing to do."""


async def _acquire_purge_lock(session: AsyncSession) -> None:
    """Take the purge's advisory lock for the life of the current transaction.

    ``pg_try_advisory_xact_lock`` returns immediately rather than waiting, which
    is what makes overlapping replicas collapse into one purge instead of
    queueing: the losers should exit immediately, not hold a database
    connection open waiting for a lock that may take a while.

    The lock is taken on the same session - and therefore inside the same
    transaction - as the delete, because :func:`purge_expired_revocations`
    commits that transaction. Holding it on a second connection would leave a
    window where the lock is held but the delete is not inside it.
    """
    acquired = await session.scalar(
        text("SELECT pg_try_advisory_xact_lock(CAST(:key AS BIGINT))"),
        {"key": PURGE_LOCK_KEY},
    )
    if not acquired:
        raise PurgeSkipped("another purge holds the advisory lock")


async def purge_once(session: AsyncSession) -> int:
    """Purge expired revocations under the advisory lock; return rows deleted.

    Raises :class:`PurgeSkipped` when another run already holds the lock. Any
    database error propagates so the caller can report a failed run rather than
    a successful one that deleted nothing.
    """
    await _acquire_purge_lock(session)
    return await purge_expired_revocations(session)


async def run_purge() -> tuple[str, int]:
    """Run the purge once. Return ``(outcome, rows_deleted)``.

    ``outcome`` is one of the bounded ``PURGE_RESULT_*`` values. The session is
    closed before returning in every case, including the skipped one, so a
    skipped run releases its connection immediately.
    """
    install_db_instrumentation(engine)
    session = async_session_factory()
    try:
        purged = await purge_once(session)
    except PurgeSkipped as exc:
        metrics.record_purge_run(metrics.PURGE_RESULT_LOCK_NOT_ACQUIRED)
        # The reason is this module's own fixed string, not caller text, so it is
        # safe to log; it is still the exception *type* that identifies it.
        _log.info("revocation_purge_skipped", reason=type(exc).__name__)
        return metrics.PURGE_RESULT_LOCK_NOT_ACQUIRED, 0
    except SQLAlchemyError as exc:
        # A purge failure must not become an application failure: it is logged,
        # counted, and reported to the scheduler as a failed run. The API
        # service is a different process and is not affected.
        metrics.record_purge_run(metrics.PURGE_RESULT_FAILED)
        _log.error("revocation_purge_failed", error=type(exc).__name__)
        raise
    finally:
        await session.close()

    result = metrics.PURGE_RESULT_PURGED if purged else metrics.PURGE_RESULT_NOTHING_EXPIRED
    metrics.record_purge_run(result, purged=purged)
    _log.info("revocation_purge_complete", purged=purged)
    return result, purged


async def _amain() -> int:
    try:
        await run_purge()
    except SQLAlchemyError as exc:
        # Type, never str(): a SQLAlchemy/driver message can embed the connection
        # string, and this is the one path that would put a credential in a log
        # line during a database incident - exactly when logs are being read.
        _log.error("revocation_purge_aborted", error=type(exc).__name__)
        return EXIT_FAILED
    except Exception as exc:  # noqa: BLE001 - a maintenance job must not traceback
        _log.error("revocation_purge_unexpected", error=type(exc).__name__)
        return EXIT_FAILED
    return EXIT_OK


def main() -> int:
    """Console entrypoint. Returns the process exit code."""
    configure_logging()
    return asyncio.run(_amain())


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    sys.exit(main())