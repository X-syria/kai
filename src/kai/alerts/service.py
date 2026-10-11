"""The durable alert outbox: atomic enqueue, lease-based delivery, retry.

Why this module exists
----------------------
``kai.observability.alerting`` turns a task transition into a payload and POSTs
it. Doing that inline, after the commit, leaves a crash window the alerting docs
name plainly: a process that dies between "the failure committed" and "the
webhook was sent" leaves a durable failure nobody was told about, and the state
machine refuses ``FAILED -> FAILED``, so nothing can ever re-raise it. The
notification is not recoverable because there is no record that one was owed.

This module closes that window with an outbox. The *intent to notify* is written
in the same transaction as the task state change, so "the failure is durable" and
"an alert is owed" are one commit and neither can exist without the other. A
separate process - :mod:`kai.maintenance.alert_dispatch` - reads those rows and
delivers them, with retries and a lease, and the task state change never waits on
a socket.

Identity
--------
``Alert.id`` is a uuid5 over the incident's own facts: ``(tenant_id, task_id,
kind, from_state, to_state, task_attempts)`` joined by an ASCII unit separator
(U+001F), which cannot occur in any component. The wall-clock ``occurred_at`` is
deliberately *excluded*: it is carried on the payload, but inside the key a clock
that moved between two writes of one incident would manufacture a second row.
The tuple above is unique per incident because ``RETRY`` is the only edge from a
failure state back into the lifecycle and entering it increments ``attempts``
(``kai.tasks.service._transition_task_locked``); the only other re-entry,
``VERIFYING -> EXECUTING``, is not a failure edge and cannot produce two failure
alerts without passing through ``RETRY`` first. So the key stays collision-free
without a timestamp, and the primary key *is* the deduplication mechanism.

``occurred_at`` - for a failure ``tasks.failed_at``, for a resolution the
transition time - is frozen into the payload so a retry re-sends the same bytes.

Guarantees, stated exactly
--------------------------
* **Atomicity.** Enqueue uses ``INSERT ... ON CONFLICT (id) DO NOTHING`` on the
  caller's session. A conflict is a duplicate of an already-recorded incident;
  the existing row is read back and its identity fields checked. Matching
  contents mean the write is a no-op; *differing* contents mean the key collided
  with a different incident, which is a defect, and it raises so the whole
  transaction (including the task state change) rolls back.
* **At-most-one deliverer.** A sweep claims a row with a conditional
  ``UPDATE ... FROM (SELECT ... FOR UPDATE SKIP LOCKED)`` that sets a per-claim
  ``claim_token`` and a ``lease_until``, and it commits that before opening a
  socket. The lock is never held across HTTP. The finaliser updates only
  ``WHERE id = :id AND claim_token = :token``, so a sweeper that lost its lease
  to a slower peer cannot overwrite the peer's result.
* **At-least-once, not exactly-once.** A process that dies after the receiver
  accepted the POST but before finalising leaves the row ``DELIVERING``; the next
  sweep reclaims it and re-sends. The receiver can recognise the resend because
  ``id`` is stable. This is the same trade ``a1b2c3d4e5f7`` documented; it is not
  solved here and is not claimed to be.
* **Bounded failure data.** ``last_error`` holds a bounded exception class or a
  fixed ``HTTPStatusError`` marker, never a URL, a body, or a driver message.

Retry policy
------------
Eight attempts including the first, exponential backoff ``30s * 2**(n-1)`` capped
at ``900s``, full jitter (a uniform draw in ``[0, ceiling]``). Transient is a
connection error, a timeout, HTTP 408/429, or any 5xx; everything else that is
not 2xx is permanent. Exhaustion parks the row at ``FAILED`` with
``next_attempt_at = NULL`` - invisible to the due-event query but still countable
on the exposition, because a permanently undelivered alert is a fact an operator
must be able to see.

Alerting is optional and off by default. When it is disabled or unconfigured a
sweep claims nothing, so queued alerts stay ``PENDING`` and deliver once an
operator arms a destination. That is a deliberate choice: discarding a durable
notification because a webhook is temporarily unset would spend the very
guarantee this table was built to provide.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import UUID, uuid4, uuid5

from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from kai.alerts.models import (
    DELIVERY_DELIVERED,
    DELIVERY_DELIVERING,
    DELIVERY_FAILED,
    DELIVERY_PENDING,
    DISPATCHER_STATE_ID,
    Alert,
    AlertDispatcherState,
)

#: Namespace for every alert identity. A fixed, committed constant, not a
#: per-process ``uuid4``: the identity has to be reproducible across restarts and
#: across replicas or the primary key stops being a deduplication key.
ALERT_IDENTITY_NAMESPACE: Final[UUID] = UUID("a4540a2c-9058-464d-82d5-1be2d327ac83")

#: Component separator for the identity pre-image. U+001F cannot appear in a UUID
#: string, a state name, a kind, or a decimal integer, so the join is unambiguous
#: without per-field escaping.
_IDENTITY_SEPARATOR: Final[str] = "\x1f"

#: Total delivery attempts, the first one included.
MAX_ATTEMPTS: Final[int] = 8

#: First retry ceiling and the cap it doubles towards. With eight attempts the
#: delays are 30, 60, 120, 240, 480, 900, 900 seconds (each jittered), which
#: spans a receiver restart and a short provider outage without holding a queue
#: row for days.
INITIAL_BACKOFF_SECONDS: Final[float] = 30.0
MAX_BACKOFF_SECONDS: Final[float] = 900.0

#: How long a claim is assumed live. Comfortably longer than any delivery
#: timeout (``alert_timeout_seconds`` defaults to 5s) so a healthy, slow receiver
#: is never reclaimed underneath itself, short enough that a crashed sweeper's
#: rows come back within one scheduler interval.
LEASE_SECONDS: Final[float] = 60.0


class AlertIntegrityError(RuntimeError):
    """A deterministic alert id collided with a row describing a different incident.

    This is not a duplicate-delivery case; it means the identity key is no longer
    a function of the fields it is supposed to be. It is raised inside the
    caller's transaction so the task state change that produced it rolls back
    rather than committing beside a corrupted alert row.
    """


def alert_identity(
    *,
    tenant_id: UUID,
    task_id: UUID,
    kind: str,
    from_state: str,
    to_state: str,
    task_attempts: int,
) -> UUID:
    """Return the deterministic id for one incident.

    Pure and total: same inputs, same id, in any process. The field set is the
    incident's identity and nothing else - see the module docstring for why
    ``occurred_at`` is not in it.
    """
    preimage = _IDENTITY_SEPARATOR.join(
        (
            str(tenant_id),
            str(task_id),
            kind,
            from_state,
            to_state,
            str(task_attempts),
        )
    )
    return uuid5(ALERT_IDENTITY_NAMESPACE, preimage)


def backoff_ceiling(attempt_number: int) -> float:
    """The exponential ceiling for the delay after ``attempt_number``.

    ``attempt_number`` is one-based and is the count of attempts already made, so
    the first failure backs off ``INITIAL_BACKOFF_SECONDS`` and each later one
    doubles until ``MAX_BACKOFF_SECONDS``.
    """
    exponent = max(attempt_number - 1, 0)
    return min(INITIAL_BACKOFF_SECONDS * (2**exponent), MAX_BACKOFF_SECONDS)


def next_backoff(attempt_number: int) -> float:
    """A full-jitter delay in ``[0, backoff_ceiling(attempt_number)]`` seconds.

    Full jitter (a uniform draw across the whole interval, not a fraction around
    a fixed point) because a receiver that was down comes back to every queued
    alert at once; spreading the retries is what keeps the recovery from being a
    thundering herd of its own.
    """
    return random.random() * backoff_ceiling(attempt_number)


@dataclass(frozen=True, slots=True)
class QueuedAlert:
    """A claimed alert, with everything needed to rebuild its payload.

    The dispatcher works from this and never re-reads the ``tasks`` row: by the
    time delivery runs the task may have moved on, and an alert must describe the
    incident that was recorded, not the task's current mood.
    """

    id: UUID
    kind: str
    task_id: UUID
    tenant_id: UUID
    from_state: str
    to_state: str
    fulfillment_profile: str | None
    task_attempts: int | None
    task_max_retries: int | None
    schema_version: str | None
    occurred_at: datetime
    #: The attempt just made (the row's post-increment count), so the finaliser
    #: does not have to re-read the row to size the next backoff.
    attempts: int
    claim_token: UUID


@dataclass(frozen=True, slots=True)
class QueueSnapshot:
    """A point-in-time read of the outbox, for the metrics endpoint.

    Ages are seconds and are ``math.inf`` when there is no evidence yet - no
    successful sweep has ever been recorded - because "unknown freshness" must
    read as maximally stale, not as fresh.
    """

    pending: int
    retryable: int
    in_flight: int
    permanently_failed: int
    oldest_pending_age_seconds: float
    dispatcher_last_success_age_seconds: float
    dispatcher_last_start_age_seconds: float
    dispatcher_last_error: str | None


def _identity_fields(
    *,
    tenant_id: UUID,
    task_id: UUID,
    kind: str,
    from_state: str,
    to_state: str,
    task_attempts: int | None,
) -> tuple[Any, ...]:
    return (tenant_id, task_id, kind, from_state, to_state, task_attempts)


async def enqueue_transition_alert(
    session: AsyncSession,
    *,
    task: Any,
    kind: str,
    from_state: str,
    to_state: str,
    occurred_at: datetime,
    schema_version: str,
) -> UUID:
    """Write the durable intent to alert, inside the caller's transaction.

    ``task`` is read for identifiers and counters only, and is typed ``Any`` for
    the same reason :func:`kai.observability.alerting.build_alert` is: this module
    must not import :mod:`kai.tasks.models` at module scope, because
    ``tasks.service`` imports it.

    Returns the alert id. A second enqueue of the same incident is a no-op: the
    conflicting row is read back and checked against the fields that produced the
    key, and only a genuine mismatch raises.
    """
    alert_id = alert_identity(
        tenant_id=task.tenant_id,
        task_id=task.id,
        kind=kind,
        from_state=from_state,
        to_state=to_state,
        task_attempts=int(task.attempts),
    )
    values = {
        "id": alert_id,
        "tenant_id": task.tenant_id,
        "task_id": task.id,
        # A task can fail with no runtime execution row; the row does not carry a
        # link here and the column is nullable for exactly that case.
        "execution_id": None,
        "kind": kind,
        "from_state": from_state,
        "to_state": to_state,
        "occurred_at": occurred_at,
        "fulfillment_profile": task.fulfillment_profile,
        "task_attempts": int(task.attempts),
        "task_max_retries": int(task.max_retries),
        "schema_version": schema_version,
        # PENDING with a due time of "now": the row is immediately eligible, and
        # the retry query's ``next_attempt_at IS NOT NULL`` naturally excludes a
        # row that has permanently failed.
        "delivery_state": DELIVERY_PENDING,
        "next_attempt_at": occurred_at,
    }
    statement = (
        pg_insert(Alert)
        .values(**values)
        .on_conflict_do_nothing(index_elements=[Alert.id])
        .returning(Alert.id)
    )
    inserted = (await session.execute(statement)).scalar_one_or_none()
    if inserted is None:
        existing = await session.get(Alert, alert_id)
        _assert_consistent(existing, values)
    return alert_id


def _assert_consistent(existing: Alert | None, values: dict[str, Any]) -> None:
    """Fail loudly when a deterministic id resolves to a different incident.

    Reached only on a conflict. Identical fields are the ordinary retry and are
    accepted; anything else means the id is not the function of the incident it
    claims to be, and continuing would hide a real incident behind a collision.
    """
    if existing is None:
        # The conflicting row vanished between the insert and the read, which
        # only happens under concurrency that also deleted it; treat as a defect
        # rather than guessing.
        raise AlertIntegrityError("alert id conflict resolved to no row")
    expected = _identity_fields(
        tenant_id=values["tenant_id"],
        task_id=values["task_id"],
        kind=values["kind"],
        from_state=values["from_state"],
        to_state=values["to_state"],
        task_attempts=values["task_attempts"],
    )
    actual = _identity_fields(
        tenant_id=existing.tenant_id,
        task_id=existing.task_id,
        kind=existing.kind,
        from_state=existing.from_state,
        to_state=existing.to_state,
        task_attempts=existing.task_attempts,
    )
    if expected != actual:
        raise AlertIntegrityError(
            "alert id collided with a different incident: "
            f"held {actual!r}, enqueueing {expected!r}"
        )


async def claim_due_event(
    session: AsyncSession,
    *,
    now: datetime,
    due_before: datetime | None = None,
    lease_seconds: float = LEASE_SECONDS,
    max_attempts: int = MAX_ATTEMPTS,
) -> QueuedAlert | None:
    """Claim the oldest alert that was due by ``due_before``, or ``None``.

    ``due_before`` bounds a sweep to the work that was already waiting when it
    began. Without it a sweep that retried a row with a jittered, near-zero
    backoff would immediately re-claim it and burn every attempt of its budget in
    a single run; with it, a row rescheduled *during* a sweep (its ``next_attempt_at``
    is now in the future) is left for the next one. It defaults to ``now`` for a
    caller that genuinely wants everything due this instant.

    The row is locked with ``SKIP LOCKED`` so two sweeps over the same queue step
    past each other rather than queueing, and the claim is written and committed
    by the caller *before* any network call - the lock is held only for the
    bookkeeping, never across HTTP.
    """
    cutoff = due_before if due_before is not None else now
    candidate = (
        select(Alert.id)
        .where(
            Alert.delivery_state.in_((DELIVERY_PENDING, DELIVERY_FAILED)),
            Alert.next_attempt_at.is_not(None),
            Alert.next_attempt_at <= cutoff,
            Alert.attempts < max_attempts,
        )
        .order_by(Alert.next_attempt_at.asc(), Alert.created_at.asc())
        .with_for_update(skip_locked=True)
        .limit(1)
        .subquery()
    )
    token = uuid4()
    statement = (
        update(Alert)
        .where(Alert.id == candidate.c.id)
        .values(
            delivery_state=DELIVERY_DELIVERING,
            claim_token=token,
            lease_until=now + timedelta(seconds=lease_seconds),
            attempts=Alert.attempts + 1,
            # A claimed row is not due again until the sweeper finalises it. The
            # finaliser sets the *next* due time on a retryable failure.
            next_attempt_at=None,
            updated_at=func.now(),
        )
        .returning(*Alert.__table__.c)
    )
    row = (await session.execute(statement)).mappings().one_or_none()
    if row is None:
        return None
    return QueuedAlert(
        id=row["id"],
        kind=row["kind"],
        task_id=row["task_id"],
        tenant_id=row["tenant_id"],
        from_state=row["from_state"],
        to_state=row["to_state"],
        fulfillment_profile=row["fulfillment_profile"],
        task_attempts=row["task_attempts"],
        task_max_retries=row["task_max_retries"],
        schema_version=row["schema_version"],
        occurred_at=row["occurred_at"],
        attempts=row["attempts"],
        claim_token=token,
    )


async def finalize_claimed(
    session: AsyncSession,
    *,
    claim: QueuedAlert,
    delivered: bool,
    retryable: bool,
    error: str | None,
    now: datetime,
    max_attempts: int = MAX_ATTEMPTS,
) -> bool:
    """Record the outcome of a claimed delivery, under the claim's own token.

    Returns whether the row was still the caller's to finalise. ``False`` means a
    lease expired and another sweep reclaimed the row first; the caller must not
    retry the write, because fighting over it is how two sweeps would overwrite
    each other's results.
    """
    values: dict[str, Any] = {
        "claim_token": None,
        "lease_until": None,
        "updated_at": func.now(),
    }
    if delivered:
        values.update(
            delivery_state=DELIVERY_DELIVERED,
            delivered_at=now,
            next_attempt_at=None,
            last_error=None,
        )
    else:
        values.update(
            delivery_state=DELIVERY_FAILED,
            last_error=error or "unknown",
        )
        if retryable and claim.attempts < max_attempts:
            delay = next_backoff(claim.attempts)
            values["next_attempt_at"] = now + timedelta(seconds=delay)
        else:
            # Exhausted, or a permanent refusal: park it. The row is still
            # counted as permanently failed on the exposition.
            values["next_attempt_at"] = None

    statement = (
        update(Alert)
        .where(Alert.id == claim.id, Alert.claim_token == claim.claim_token)
        .values(**values)
    )
    result = await session.execute(statement)
    # ``rowcount`` is only on the DBAPI-returning result; the async session type
    # does not declare it, so it is read defensively (see
    # ``kai.auth.revocation``). A zero means a lease was lost to a faster sweep.
    return bool(getattr(result, "rowcount", 0))


async def recover_expired_leases(
    session: AsyncSession,
    *,
    now: datetime,
    max_attempts: int = MAX_ATTEMPTS,
) -> int:
    """Reclaim rows whose ``DELIVERING`` lease has expired. Return how many.

    A row is ``DELIVERING`` with an expired lease only because the sweeper that
    claimed it died before finalising. It is set back to ``FAILED``; if the
    attempt budget still allows, it is made due immediately so the *next* sweep
    retries it, otherwise it is parked. The row's ``attempts`` was already
    incremented at claim time, so a crash costs one attempt, not an infinite
    loop.
    """
    statement = (
        update(Alert)
        .where(
            Alert.delivery_state == DELIVERY_DELIVERING,
            Alert.lease_until.is_not(None),
            Alert.lease_until <= now,
        )
        .values(
            delivery_state=DELIVERY_FAILED,
            claim_token=None,
            lease_until=None,
            next_attempt_at=case(
                (Alert.attempts < max_attempts, now),
                else_=None,
            ),
            updated_at=func.now(),
        )
    )
    result = await session.execute(statement)
    return int(getattr(result, "rowcount", 0) or 0)


def _age_seconds(now: datetime, moment: datetime | None) -> float:
    """Seconds from ``moment`` to ``now``; ``inf`` when ``moment`` is unknown."""
    if moment is None:
        return math.inf
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return max((now - moment).total_seconds(), 0.0)


async def snapshot_queue(session: AsyncSession, *, now: datetime) -> QueueSnapshot:
    """Read the queue and dispatcher freshness in bounded aggregates.

    Deliberately no per-row read: this runs on every scrape, and the counters it
    feeds are unindexed gauges, so a single aggregate query is what keeps it
    from becoming the heaviest thing the metrics endpoint does.
    """
    pending = func.count().filter(Alert.delivery_state == DELIVERY_PENDING)
    retryable = func.count().filter(
        and_(
            Alert.delivery_state == DELIVERY_FAILED,
            Alert.next_attempt_at.is_not(None),
        )
    )
    in_flight = func.count().filter(Alert.delivery_state == DELIVERY_DELIVERING)
    permanently_failed = func.count().filter(
        and_(
            Alert.delivery_state == DELIVERY_FAILED,
            Alert.next_attempt_at.is_(None),
        )
    )
    oldest_pending = func.min(Alert.created_at).filter(
        or_(
            Alert.delivery_state == DELIVERY_PENDING,
            and_(
                Alert.delivery_state == DELIVERY_FAILED,
                Alert.next_attempt_at.is_not(None),
            ),
        )
    )
    row = (
        await session.execute(
            select(pending, retryable, in_flight, permanently_failed, oldest_pending)
        )
    ).one()

    dispatcher = await session.get(AlertDispatcherState, DISPATCHER_STATE_ID)
    last_success = dispatcher.last_sweep_completed_at if dispatcher else None
    last_start = dispatcher.last_sweep_started_at if dispatcher else None
    last_error = dispatcher.last_error if dispatcher else None

    return QueueSnapshot(
        pending=int(row[0] or 0),
        retryable=int(row[1] or 0),
        in_flight=int(row[2] or 0),
        permanently_failed=int(row[3] or 0),
        # An empty queue has an age of zero, not unknown: there is nothing
        # waiting, which is the healthiest reading.
        oldest_pending_age_seconds=(
            _age_seconds(now, row[4]) if row[4] is not None else 0.0
        ),
        dispatcher_last_success_age_seconds=_age_seconds(now, last_success),
        dispatcher_last_start_age_seconds=_age_seconds(now, last_start),
        dispatcher_last_error=last_error,
    )


async def record_sweep_start(session: AsyncSession, *, now: datetime) -> None:
    """Stamp the sweep's start. Committed before any delivery work."""
    await _upsert_dispatcher_state(session, values={"last_sweep_started_at": now})


async def record_sweep_complete(session: AsyncSession, *, now: datetime) -> None:
    """Stamp a clean sweep completion and clear the last error."""
    await _upsert_dispatcher_state(
        session,
        values={"last_sweep_completed_at": now, "last_error": None},
    )


async def record_sweep_error(session: AsyncSession, *, error: str) -> None:
    """Record a bounded exception class for a failed sweep."""
    await _upsert_dispatcher_state(session, values={"last_error": error})


async def _upsert_dispatcher_state(
    session: AsyncSession, *, values: dict[str, Any]
) -> None:
    """Write the singleton dispatcher row, creating it on first use.

    An upsert rather than a read-then-write so two sweeps starting at once cannot
    both decide the row is missing and race the insert.
    """
    statement = (
        pg_insert(AlertDispatcherState)
        .values(id=DISPATCHER_STATE_ID, **values)
        .on_conflict_do_update(
            index_elements=[AlertDispatcherState.id],
            set_={**values, "updated_at": func.now()},
        )
    )
    await session.execute(statement)
