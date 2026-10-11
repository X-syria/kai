"""add alert outbox: delivery bookkeeping, claim lease, dispatcher state

``a1b2c3d4e5f7`` created the ``alerts`` table: one durable row per operational
alert, inserted in the same transaction as the task transition that raised it.
That row is the *record of intent*. This revision adds what a second process
needs to act on that intent without the raising transaction ever calling out to
HTTP, and the columns the outbound payload is rebuilt from so delivery no longer
depends on the task row still existing in the state it was in.

What the new columns are for (see ``src/kai/alerts/models.py`` and
``src/kai/alerts/service.py``):

* ``fulfillment_profile`` / ``task_attempts`` / ``task_max_retries`` /
  ``schema_version`` - the incident facts the outbound payload is derived from,
  frozen at enqueue time. A dispatcher rebuilds the payload from these and must
  not re-read the mutable ``tasks`` row, which may have moved on (or been
  deleted) by the time delivery runs. Nullable for rows written before this
  revision.
* ``claim_token`` / ``lease_until`` - an at-most-one-deliverer lease. A sweep
  marks a row ``DELIVERING`` with a per-claim token and a lease deadline, commits
  that before it opens a socket, and only finalises the row while it still holds
  the token. A process that dies mid-delivery leaves the lease to expire, and
  the next sweep reclaims it: at-least-once, never silently lost.

``alert_dispatcher_state`` is a single-row heartbeat. The dispatcher is a one-shot
command run by the operator's scheduler, so its own counters die with the
process; the freshness of this row is what the scraping app turns into
``kai_alerts_dispatcher_last_success_age_seconds`` so a scheduler that has
stopped running is visible on the exposition.

Upgrade: additive only. No existing column, index or default is touched, so a
running application is unaffected until it is re-deployed against the new head,
and the new columns are nullable so pre-existing alert rows stay valid.
Downgrade drops the table, the lease index and the six columns; it discards
in-flight delivery bookkeeping (any ``DELIVERING`` lease, and the frozen payload
facts) but keeps every alert row itself, so no operator-visibility record is
lost by a downgrade.

Revision ID: e6f7a8b9c0d1
Revises: a1b2c3d4e5f7
Create Date: 2026-10-11 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e6f7a8b9c0d1"
down_revision: str | None = "a1b2c3d4e5f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The incident facts the outbound payload is rebuilt from, frozen at enqueue
    # time. Nullable so rows written before this revision remain valid.
    op.add_column(
        "alerts", sa.Column("fulfillment_profile", sa.String(length=64), nullable=True)
    )
    op.add_column("alerts", sa.Column("task_attempts", sa.Integer(), nullable=True))
    op.add_column("alerts", sa.Column("task_max_retries", sa.Integer(), nullable=True))
    op.add_column(
        "alerts", sa.Column("schema_version", sa.String(length=64), nullable=True)
    )
    # The delivery lease. ``claim_token`` identifies the one process allowed to
    # finalise the row it set; ``lease_until`` is when a silent claim is assumed
    # dead and may be reclaimed.
    op.add_column("alerts", sa.Column("claim_token", sa.UUID(), nullable=True))
    op.add_column(
        "alerts", sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True)
    )
    # Reclaiming an expired lease scans exactly the rows that can be in flight,
    # so the predicate is partial on ``DELIVERING`` and the index stays tiny.
    op.create_index(
        "ix_alerts_lease",
        "alerts",
        ["lease_until"],
        unique=False,
        postgresql_where=sa.text("delivery_state = 'DELIVERING'"),
    )

    op.create_table(
        "alert_dispatcher_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "last_sweep_started_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "last_sweep_completed_at", sa.DateTime(timezone=True), nullable=True
        ),
        # A bounded exception class, never a message: the same rule the alert's
        # own ``last_error`` follows.
        sa.Column("last_error", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        comment="single-row heartbeat for the external alert dispatcher",
    )


def downgrade() -> None:
    op.drop_table("alert_dispatcher_state")
    op.drop_index("ix_alerts_lease", table_name="alerts")
    op.drop_column("alerts", "lease_until")
    op.drop_column("alerts", "claim_token")
    op.drop_column("alerts", "schema_version")
    op.drop_column("alerts", "task_max_retries")
    op.drop_column("alerts", "task_attempts")
    op.drop_column("alerts", "fulfillment_profile")
