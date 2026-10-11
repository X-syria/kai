"""The :class:`Alert` row: one logical operational alert, durably.

Why a row exists
----------------
Before this table, a task failure produced a webhook POST and nothing else. The
failure was durable; the *notification* was an in-process side effect that ran after
the commit, so a process that died between the two left a durably failed task that no
operator was ever told about, and nothing recorded that an alert had been owed. The
state machine then refuses the same edge again (``FAILED -> FAILED`` is not an edge),
so no retry could ever re-raise it: the loss was permanent.

One row closes that. It is inserted in the *same transaction* as the task state
change, so "the failure is durable" and "the intention to notify is durable" are the
same commit and neither can exist without the other. Delivery is a separate, retriable
act that reads and writes this row; it is never the source of truth.

What the row is not
-------------------
This is an *operational* record. It says a task entered a failure state and whether an
operator has since seen and dealt with it. It says nothing about whether any external
side effect happened: ``external_outcome`` and the fulfillment verdict live on the
execution row and are written only from evidence.

NULL semantics
--------------
Every nullable column here means "this did not happen yet", never "this is unknown" and
never a backfilled guess:

* ``execution_id`` NULL - the task failed with no runtime execution row (a client
  declared the failure, or the execution row was deleted). It is not a placeholder id.
* ``next_attempt_at`` NULL - no retry is scheduled: never attempted, the attempt budget
  is spent, or the delivery reached a final state.
* ``delivered_at`` NULL - no receiver has ever accepted this alert.
* ``last_error`` NULL - no delivery failure has been recorded. It is never a success
  marker; that is ``delivery_state``.
* ``claim_token`` / ``lease_until`` NULL - no delivery sweep holds a lease on this row.
  A row in ``DELIVERING`` always has both; every other state has neither.
* ``fulfillment_profile`` / ``task_attempts`` / ``task_max_retries`` /
  ``schema_version`` NULL - the row predates the outbox revision, which is when
  these frozen payload facts began to be written. It never means "the task had
  no profile"; the payload builder refuses to reconstruct a payload from a row
  whose frozen facts are absent rather than inventing defaults.
* ``acknowledged_at`` / ``resolved_at`` NULL - the operator has not done that yet.
* ``resolution_note`` NULL - the operator recorded no note. It never means "there was
  no reason".

The one ambiguity this table cannot remove is deliberate: a process that dies between
the receiver accepting a POST and the row being marked ``DELIVERED`` leaves no way to
know which happened. The row says ``DELIVERING``, the sweep re-sends, and the receiver
can recognise the resend because ``id`` is stable. That is at-least-once delivery with
best-effort deduplication; it is not exactly-once.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from kai.db.base import Base, PKMixin, TenantMixin, TimestampMixin

#: Operator lifecycle. Strictly monotonic: OPEN -> ACKNOWLEDGED -> RESOLVED, and
#: nothing goes backwards. Refusing to resolve an alert nobody acknowledged is what
#: makes the acknowledgement a record of a human rather than a formality.
ALERT_STATE_OPEN: Final[str] = "OPEN"
ALERT_STATE_ACKNOWLEDGED: Final[str] = "ACKNOWLEDGED"
ALERT_STATE_RESOLVED: Final[str] = "RESOLVED"

ALERT_STATES: Final[frozenset[str]] = frozenset(
    {ALERT_STATE_OPEN, ALERT_STATE_ACKNOWLEDGED, ALERT_STATE_RESOLVED}
)

#: Delivery lifecycle, orthogonal to the operator lifecycle above.
#:
#: ``PENDING``    - durable intent, no attempt has been made.
#: ``DELIVERING`` - an attempt is in flight, or one was and the process died before
#:                  recording the result. Never read as delivered.
#: ``DELIVERED``  - a receiver accepted the alert. Terminal for delivery; the row stays
#:                  forever as the record that it was sent.
#: ``FAILED``     - the last attempt did not reach a receiver. Retryable while
#:                  ``next_attempt_at`` is set; permanently failed once it is NULL.
DELIVERY_PENDING: Final[str] = "PENDING"
DELIVERY_DELIVERING: Final[str] = "DELIVERING"
DELIVERY_DELIVERED: Final[str] = "DELIVERED"
DELIVERY_FAILED: Final[str] = "FAILED"

DELIVERY_STATES: Final[frozenset[str]] = frozenset(
    {DELIVERY_PENDING, DELIVERY_DELIVERING, DELIVERY_DELIVERED, DELIVERY_FAILED}
)


class Alert(PKMixin, TenantMixin, TimestampMixin, Base):
    """One logical alert about one task, from one incident to its operator closure.

    ``id`` is *not* a random UUID even though :class:`PKMixin` would generate one: it is
    a uuid5 derived from the incident's own facts (see
    :func:`kai.alerts.service.alert_identity`). That is the deduplication
    mechanism and it is the primary key, so the database enforces it - a second
    insertion of the same logical alert conflicts instead of paging an operator twice.
    Every redelivery of the same incident reuses this id, which is what lets a receiver
    recognise a retry as the same alert rather than a new one.

    The row carries the incident's identity and nothing else. There is no ``goal``, no
    ``last_error`` and no provider message here, and the outbound payload is derived from
    these columns rather than copied, so "no secrets leave the process" stays a
    structural property of the column set instead of a redaction list somebody has to
    keep complete.

    Tenancy is a hard FK like every other domain row: an alert belongs to exactly one
    tenant, and every read of it in :mod:`kai.alerts.service` filters on that column, so
    an alert id belonging to another tenant does not resolve.
    """

    __tablename__ = "alerts"
    # The text matches ``a1b2c3d4e5f7_add_alerts`` exactly. A mismatch here is not
    # cosmetic: ``alembic check`` would propose a ``modify_comment`` that strips
    # the description the migration applied.
    #
    # The two composite indexes are declared here because the migration creates
    # them. Leaving them out does not merely lose an optimisation: ``alembic
    # check`` reports each as a *removed* index, and accepting that proposal
    # would make the next autogenerate emit ``drop_index`` for both. The
    # ``index=True`` columns above cover the single-column cases; a composite
    # cannot be expressed that way.
    __table_args__ = (
        # The dispatch sweep's predicate, in the order it is written. Partial on
        # the two states that can still be attempted, so the index stays small no
        # matter how many delivered alerts accumulate, and so a scan never has to
        # skip the delivered ones.
        Index(
            "ix_alerts_due",
            "next_attempt_at",
            "created_at",
            unique=False,
            postgresql_where=text("delivery_state in ('PENDING', 'FAILED')"),
        ),
        # Reclaiming an expired delivery lease scans exactly the rows that can be
        # in flight. Partial on ``DELIVERING`` for the same reason as above: the
        # index only ever covers the handful of rows currently being delivered.
        Index(
            "ix_alerts_lease",
            "lease_until",
            unique=False,
            postgresql_where=text("delivery_state = 'DELIVERING'"),
        ),
        Index("ix_alerts_tenant_state", "tenant_id", "state", unique=False),
        {"comment": "durable operational alert and its operator lifecycle"},
    )

    task_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    #: Nullable on purpose. A task can fail with no runtime execution row at all - a
    #: client declaring its own task failed is the ordinary case - and a deleted
    #: execution must not delete the record that it failed.
    execution_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("runtime_executions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    #: A closed set from :mod:`kai.observability.alerting`, not caller text.
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    from_state: Mapped[str] = mapped_column(String(64), nullable=False)
    to_state: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The incident's own timestamp: for a failure this is ``tasks.failed_at``,
    #: for a resolution the transition time. It is carried on the payload but it
    #: is deliberately *not* part of the identity (see
    #: :func:`kai.alerts.service.alert_identity`): the identity is closed over the
    #: state machine's own counters, and a wall-clock instant inside the key would
    #: make two writes of the same incident two rows whenever the clock moved.
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Frozen incident facts, written once at enqueue. The outbound payload is
    # rebuilt from these by the dispatcher, so delivery never re-reads the
    # mutable ``tasks`` row - which may have changed state, or been deleted,
    # since the incident was recorded. Nullable only for rows that predate the
    # outbox revision; the payload builder refuses such a row rather than
    # inventing values.
    fulfillment_profile: Mapped[str | None] = mapped_column(String(64), nullable=True)
    task_attempts: Mapped[int | None] = mapped_column(Integer, nullable=True)
    task_max_retries: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: The payload discriminator in force when the incident happened, so a later
    #: schema bump does not silently relabel an already-recorded incident.
    schema_version: Mapped[str | None] = mapped_column(String(64), nullable=True)

    state: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=ALERT_STATE_OPEN,
        server_default=ALERT_STATE_OPEN,
    )
    delivery_state: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=DELIVERY_PENDING,
        server_default=DELIVERY_PENDING,
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: The delivery lease. ``claim_token`` is rewritten on every claim and is the
    #: proof a finaliser still owns the attempt it is completing; ``lease_until``
    #: is when a claim that never finalised is assumed dead and reclaimed. Both
    #: are NULL whenever no sweep is holding the row.
    claim_token: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    lease_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: The failing exception's *type name* and nothing more. A driver message can embed
    #: the connection string and an HTTP error body can echo the payload, so the type
    #: is the most this column will ever hold. It exists because "receiver refused" and
    #: "receiver unreachable" call for different operator actions, and a bare FAILED
    #: cannot tell them apart.
    last_error: Mapped[str | None] = mapped_column(Text)

    #: The operator who acknowledged, resolved by row. ``SET NULL`` because deleting a
    #: user must not erase the fact that somebody handled an incident.
    acknowledged_by: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resolved_by: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: Operator-authored, bounded, whitespace-normalised. Stored in KAI's own database
    #: beside ``tasks.goal`` and ``tasks.last_error`` and returned only inside the owning
    #: tenant - it is never part of the outbound alert payload.
    resolution_note: Mapped[str | None] = mapped_column(Text)


#: The singleton row identity. A one-row table is what makes "the dispatcher is
#: alive" a single readable fact rather than a label to aggregate over, and the
#: fixed key lets the service upsert without a race.
DISPATCHER_STATE_ID: Final[int] = 1


class AlertDispatcherState(TimestampMixin, Base):
    """One row: the freshness of the external alert dispatcher.

    The dispatcher is a one-shot command the operator's scheduler invokes, so it
    owns no long-lived metrics registry - its own counters would die with the
    process that recorded them, exactly the trap the revocation purge documents
    (see ``deploy/alerting/kai-alerts.scheduler-pending.yml``). Instead it stamps
    this row, and the scraping application process turns the age of those stamps
    into ``kai_alerts_dispatcher_last_success_age_seconds``. A scheduler that has
    stopped running then shows up on the exposition the app already serves.

    There is deliberately no ``alerts`` FK here: this is bookkeeping about the
    dispatcher, not about any one alert.
    """

    __tablename__ = "alert_dispatcher_state"
    __table_args__ = (
        {"comment": "single-row heartbeat for the external alert dispatcher"},
    )

    #: Fixed at :data:`DISPATCHER_STATE_ID`; never generated.
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: When the most recent sweep began. Advanced before any delivery work, so a
    #: wedge *during* a sweep is visible rather than indistinguishable from a
    #: scheduler that never fired at all.
    last_sweep_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: When the most recent sweep finished cleanly. This is the alerting signal:
    #: its age is what the stale-worker rule watches.
    last_sweep_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: A bounded exception class from the last failed sweep, never a message: the
    #: same rule the alert's own ``last_error`` follows, because a driver message
    #: can embed the connection string.
    last_error: Mapped[str | None] = mapped_column(String(64), nullable=True)


__all__ = [
    "ALERT_STATE_ACKNOWLEDGED",
    "ALERT_STATE_OPEN",
    "ALERT_STATE_RESOLVED",
    "ALERT_STATES",
    "DELIVERY_DELIVERED",
    "DELIVERY_DELIVERING",
    "DELIVERY_FAILED",
    "DELIVERY_PENDING",
    "DELIVERY_STATES",
    "DISPATCHER_STATE_ID",
    "Alert",
    "AlertDispatcherState",
]
