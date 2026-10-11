"""Scheduled delivery for the durable alert outbox.

Why this is a separate one-shot command
---------------------------------------
A task transition writes an alert row in its own transaction
(:func:`kai.alerts.service.enqueue_transition_alert`) and never opens a socket.
Something then has to read those rows and POST them, and that something must not
be the process that raises alerts, for the same reason the revocation purge is
external: the application must not own a schedule, and a scheduler that lives in
the app either ties the cadence to the replica count or needs leader election of
its own. An operator's scheduler invokes ``kai-dispatch-alerts`` every minute and
the process exits.

What one sweep does
-------------------
1. Stamp the sweep start in ``alert_dispatcher_state`` and commit it, so a sweep
   that wedges *during* delivery is distinguishable from a scheduler that never
   ran at all.
2. Reclaim expired leases: any row still ``DELIVERING`` past its ``lease_until``
   was claimed by a process that died before finalising. It goes back to
   ``FAILED`` and, if its attempt budget remains, is made due again.
3. Claim and deliver, one alert at a time, only rows that were already due when
   the sweep began (``due_before``). Each claim is committed *before* the POST -
   the row lock is never held across a network call - and the outcome is written
   back under the claim's own token, so a slower peer that reclaimed the row can
   never be overwritten.
4. Stamp the sweep completion.

At-least-once, never silent
---------------------------
A crash between "the receiver accepted it" and "the row was marked delivered"
leaves the row ``DELIVERING``; the next sweep reclaims and re-sends it. The
receiver recognises the resend by the stable ``id``. This is the delivery
contract the ``alerts`` table documents, and it is the reason a per-claim lease
exists rather than a simple flag.

Alerting disabled or unconfigured
---------------------------------
A sweep claims nothing when ``ALERTING_ENABLED`` is false or no URL is set, so
queued alerts stay ``PENDING`` and deliver once a destination exists. Discarding
a durable notification because a webhook is temporarily unset would spend the
guarantee the outbox was built to provide. The sweep still stamps its start and
completion, so a deployment that has simply not armed a destination does not make
``kai_alerts_dispatcher_last_success_age_seconds`` go stale - "alerting is off"
and "the worker is dead" stay distinguishable. Alerts that are owed but not
moving are surfaced by ``kai_alerts_permanently_failed`` instead.

Why the metrics this records are process-local
----------------------------------------------
``metrics.record_alert_delivery`` increments a counter in *this* process's
registry, which dies when the process exits - exactly like the purge's counters.
It is recorded because a live run's log and a debugging session benefit from it,
but it is not the scraped signal. The scraped signal is the outbox depth and the
dispatcher freshness, which the application process reads from the database
(:mod:`kai.api.metrics`) and exposes on the endpoint Prometheus already scrapes.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from sqlalchemy.exc import SQLAlchemyError

from kai.alerts import service as alert_service
from kai.alerts.service import MAX_ATTEMPTS, AlertIntegrityError, QueuedAlert
from kai.config import get_settings
from kai.db.engine import async_session_factory, engine
from kai.logging import configure_logging, get_logger
from kai.observability import alerting, metrics
from kai.observability.db import install_db_instrumentation

_log = get_logger("kai.maintenance.alert_dispatch")

#: Process exit codes for the scheduler.
EXIT_OK: Final[int] = 0
EXIT_FAILED: Final[int] = 1

#: Ceiling on one sweep, so a backlog cannot turn a one-minute job into one that
#: runs for an hour and overlaps its successors. A queue larger than this drains
#: over successive sweeps rather than in one.
MAX_EVENTS_PER_SWEEP: Final[int] = 1000

#: Recorded in ``last_error`` when a durable row cannot be turned back into a
#: payload (a pre-outbox row with no frozen facts). It is a permanent failure:
#: no number of retries will add the missing columns.
_MALFORMED: Final[str] = "MalformedAlertRow"


@dataclass(frozen=True, slots=True)
class SweepSummary:
    """What one sweep did, for the log line and the process exit decision."""

    delivered: int
    retried: int
    failed: int
    recovered: int
    outcome: str


def _payload_from_claim(claim: QueuedAlert) -> alerting.TaskAlert:
    """Rebuild the wire alert from the frozen incident facts on the row.

    A pre-outbox row (any frozen field NULL) cannot be reconstructed and is
    refused rather than completed with invented defaults; the caller parks it as
    a permanent failure.
    """
    if (
        claim.fulfillment_profile is None
        or claim.task_attempts is None
        or claim.task_max_retries is None
        or claim.schema_version is None
    ):
        raise AlertIntegrityError("alert row is missing its frozen payload facts")
    occurred = claim.occurred_at
    if occurred.tzinfo is None:
        occurred = occurred.replace(tzinfo=UTC)
    return alerting.TaskAlert(
        kind=claim.kind,
        task_id=str(claim.task_id),
        tenant_id=str(claim.tenant_id),
        from_state=claim.from_state,
        to_state=claim.to_state,
        fulfillment_profile=claim.fulfillment_profile,
        attempts=claim.task_attempts,
        max_retries=claim.task_max_retries,
        occurred_at=occurred.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        schema_version=claim.schema_version,
    )


async def _deliver_claimed(
    claim: QueuedAlert, *, url: str, timeout: float
) -> alerting.DeliveryAttempt:
    """Build the payload and attempt one delivery, classifying any failure."""
    try:
        alert = _payload_from_claim(claim)
    except AlertIntegrityError:
        return alerting.DeliveryAttempt(
            delivered=False, retryable=False, error=_MALFORMED
        )
    attempt = await alerting.attempt_delivery(alert, url=url, timeout=timeout)
    if not attempt.delivered:
        # Same event name and shape the inline path used, so existing log-based
        # alerting keeps working. Destination is redacted to scheme and host.
        _log.warning(
            "alert_delivery_failed",
            kind=claim.kind,
            task_id=str(claim.task_id),
            destination=alerting._redact(url),
            error=attempt.error,
            attempt=claim.attempts,
        )
    return attempt


async def run_dispatch() -> SweepSummary:
    """Run one sweep. Returns what it did. Raises only on a database-level fault.

    The caller maps a raised fault to a non-zero exit so the scheduler notices;
    an ordinary delivery failure is an outcome of the sweep, not a fault of it.
    """
    install_db_instrumentation(engine)
    settings = get_settings()
    url = settings.alert_webhook_url.strip()

    sweep_started = datetime.now(UTC)
    async with async_session_factory() as session:
        await alert_service.record_sweep_start(session, now=sweep_started)
        await session.commit()
        # Lease recovery is pure database bookkeeping and runs even when alerting
        # is off: a row left DELIVERING by a sweep that died while alerting *was*
        # armed must not stay claimed forever.
        recovered = await alert_service.recover_expired_leases(
            session, now=sweep_started
        )
        await session.commit()

    if not settings.alerting_enabled or not url:
        # The sweep still ran and is still healthy, so it stamps a completion:
        # "alerting is off" must not read as "the worker is dead". Queued alerts
        # stay PENDING and deliver once a destination exists; the permanent-
        # failure gauge is what surfaces alerts that are owed and not moving.
        async with async_session_factory() as session:
            await alert_service.record_sweep_complete(session, now=datetime.now(UTC))
            await session.commit()
        _log.warning(
            "alert_dispatch_disabled",
            reason="alerting disabled or no webhook configured",
        )
        return SweepSummary(0, 0, 0, recovered, "disabled")

    delivered = retried = failed = 0
    for _ in range(MAX_EVENTS_PER_SWEEP):
        async with async_session_factory() as session:
            claim = await alert_service.claim_due_event(
                session, now=datetime.now(UTC), due_before=sweep_started
            )
            await session.commit()
        if claim is None:
            break

        attempt = await _deliver_claimed(
            claim, url=url, timeout=settings.alert_timeout_seconds
        )

        now = datetime.now(UTC)
        async with async_session_factory() as session:
            applied = await alert_service.finalize_claimed(
                session,
                claim=claim,
                delivered=attempt.delivered,
                retryable=attempt.retryable,
                error=attempt.error,
                now=now,
                max_attempts=MAX_ATTEMPTS,
            )
            await session.commit()

        if attempt.delivered:
            metrics.record_alert_delivery(claim.kind, metrics.ALERT_OUTCOME_SENT)
            delivered += 1
        else:
            metrics.record_alert_delivery(claim.kind, metrics.ALERT_OUTCOME_FAILED)
            if applied and attempt.retryable and claim.attempts < MAX_ATTEMPTS:
                retried += 1
            else:
                # Exhausted the budget, permanently refused, orphaned by a lost
                # lease, or malformed. In every case it will not be attempted
                # again without operator action, which the permanent-failure
                # gauge makes visible.
                failed += 1

    async with async_session_factory() as session:
        await alert_service.record_sweep_complete(session, now=datetime.now(UTC))
        await session.commit()

    _log.info(
        "alert_dispatch_complete",
        delivered=delivered,
        retried=retried,
        failed=failed,
        recovered=recovered,
    )
    return SweepSummary(delivered, retried, failed, recovered, "completed")


async def _record_error(error: str) -> None:
    """Best-effort record of a failed sweep. Never raises."""
    try:
        async with async_session_factory() as session:
            await alert_service.record_sweep_error(session, error=error)
            await session.commit()
    except Exception as exc:  # noqa: BLE001 - the original error is what matters
        _log.warning("alert_dispatch_error_record_failed", error=type(exc).__name__)


async def _amain() -> int:
    try:
        await run_dispatch()
    except SQLAlchemyError as exc:
        # Type, never str(): a driver message can embed the connection string.
        await _record_error(type(exc).__name__)
        _log.error("alert_dispatch_aborted", error=type(exc).__name__)
        return EXIT_FAILED
    except Exception as exc:  # noqa: BLE001 - a maintenance job must not traceback
        await _record_error(type(exc).__name__)
        _log.error("alert_dispatch_unexpected", error=type(exc).__name__)
        return EXIT_FAILED
    return EXIT_OK


def main() -> int:
    """Console entrypoint. Returns the process exit code."""
    configure_logging()
    return asyncio.run(_amain())


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    sys.exit(main())
