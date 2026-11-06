"""One-shot housekeeping for abandoned Google pending-link transactions.

Why this exists
---------------
A pending transaction holds the encrypted access token (and sometimes a refresh
token) of a Google grant that was proven but never confirmed. It is only meant to
live for :data:`kai.google.pending.PENDING_TTL_SECONDS` while the user reviews the
confirmation page. If nobody ever opens that page - a closed tab, a crashed
browser, a link followed by somebody who is not signed in - the credentials would
otherwise sit there forever.

Mechanism
---------
* **External scheduler, one-shot command.** Same shape as
  ``kai-purge-revocations``: the API process owns no timer, and the operator runs
  ``python -m kai.maintenance.google_pending_purge`` from its scheduler. It does
  not change the Compose topology.
* **Same image, no second artifact,** so it cannot drift from the schema it
  purges.
* **Advisory lock.** :func:`kai.google.pending.purge_pending_transactions` takes
  a transaction-scoped ``pg_try_advisory_xact_lock`` under its own key, so
  overlapping runs collapse into one and a killed run cannot leak the lock.
* **Idempotent.** Expiring an already-expired row matches nothing; deleting a
  row that is gone returns 0.
* **No HTTP endpoint.** A client cannot ask for this.

What it changes
---------------
1. A ``pending`` row past ``expires_at`` becomes ``expired`` and loses its
   ``credentials_encrypted`` / ``token_expires_at``. Expiring is what removes the
   secret; deletion is only bookkeeping.
2. A terminal row (``consumed``/``cancelled``/``expired``/``failed``) older than
   :data:`kai.google.pending.TERMINAL_RETENTION` is deleted.

It never touches ``google_accounts``. An unconfirmed transaction has, by
construction, never written one.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Final

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from kai.db.engine import async_session_factory, engine
from kai.google.pending import purge_pending_transactions
from kai.logging import configure_logging, get_logger
from kai.observability.db import install_db_instrumentation

_log = get_logger("kai.maintenance.google_pending_purge")

#: Process exit codes: distinct so a scheduler can tell success from failure.
EXIT_OK: Final[int] = 0
EXIT_FAILED: Final[int] = 1


async def run_purge() -> tuple[int, int]:
    """Run the purge once. Return ``(expired, deleted)``.

    The session is closed in every case, including the lock-skipped one, so no
    connection is held after the command returns.
    """
    install_db_instrumentation(engine)
    session: AsyncSession = async_session_factory()
    try:
        return await purge_pending_transactions(session)
    finally:
        await session.close()


async def _amain() -> int:
    try:
        expired, deleted = await run_purge()
    except SQLAlchemyError as exc:
        # Type, never str(): a driver message can embed the connection string,
        # and an incident is exactly when logs are being read.
        _log.error("google_pending_purge_aborted", error=type(exc).__name__)
        return EXIT_FAILED
    except Exception as exc:  # noqa: BLE001 - a maintenance job must not traceback
        _log.error("google_pending_purge_unexpected", error=type(exc).__name__)
        return EXIT_FAILED
    _log.info("google_pending_purge_complete", expired=expired, deleted=deleted)
    return EXIT_OK


def main() -> int:
    """Console entrypoint. Returns the process exit code."""
    configure_logging()
    return asyncio.run(_amain())


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    sys.exit(main())
