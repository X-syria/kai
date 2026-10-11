"""add alerts: durable notification intent and operator lifecycle

Closes the crash window between a committed task failure and the webhook POST that
was supposed to tell an operator about it. See ``src/kai/alerts/models.py`` for what
each column means and why it is nullable; the short version:

* ``id`` is a uuid5 derived from the incident's own facts, not a random UUID, so the
  primary key is the deduplication mechanism. Retries reuse it.
* ``tenant_id`` is a hard FK and every service query filters on it, so an alert cannot
  be read or acted on across tenants.
* ``occurred_at`` is a fact of the incident, not part of its identity: the identity
  is closed over the task, the transition and the attempt counter, so two genuinely
  separate failures of one task are two incidents (the attempt counter moved) while a
  re-write of one incident is one. See ``kai.alerts.service.alert_identity``.
* nullable columns all mean "this did not happen yet" and never "unknown".

Retention: alerts are the durable answer to "did anyone know about this, and did they
deal with it", so they are not purged on any schedule in this revision. A later round
may add one; nothing here depends on them existing forever beyond that.

Upgrade: additive only - one new table and its indexes. No existing table, column,
index or default is touched, so the running application is unaffected until it is
re-deployed against the new head. Downgrade drops the table and its indexes, which
discards undelivered notification intents: a downgrade is an operator accepting that
an alert may never be delivered.

Revision ID: a1b2c3d4e5f7
Revises: d4e5f6a7b8c9
Create Date: 2026-10-03 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1b2c3d4e5f7"
down_revision: str | None = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "alerts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("task_id", sa.UUID(), nullable=False),
        sa.Column("execution_id", sa.UUID(), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("from_state", sa.String(length=64), nullable=False),
        sa.Column("to_state", sa.String(length=64), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "state", sa.String(length=32), server_default="OPEN", nullable=False
        ),
        sa.Column(
            "delivery_state",
            sa.String(length=16),
            server_default="PENDING",
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("acknowledged_by", sa.UUID(), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.UUID(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution_note", sa.Text(), nullable=True),
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
        # SET NULL, not CASCADE: deleting a user must not erase the fact that somebody
        # handled an incident, and deleting an execution must not delete the record that
        # the execution failed.
        sa.ForeignKeyConstraint(["acknowledged_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["execution_id"], ["runtime_executions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["resolved_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        comment="durable operational alert and its operator lifecycle",
    )
    op.create_index(op.f("ix_alerts_tenant_id"), "alerts", ["tenant_id"], unique=False)
    op.create_index(op.f("ix_alerts_task_id"), "alerts", ["task_id"], unique=False)
    op.create_index(
        op.f("ix_alerts_execution_id"), "alerts", ["execution_id"], unique=False
    )
    # The dispatch sweep's predicate, in the order it is written. Partial on the two
    # states that can still be attempted, so the index stays small no matter how many
    # delivered alerts accumulate, and so a scan never has to skip the delivered ones.
    op.create_index(
        "ix_alerts_due",
        "alerts",
        ["next_attempt_at", "created_at"],
        unique=False,
        postgresql_where=sa.text("delivery_state in ('PENDING', 'FAILED')"),
    )
    op.create_index(
        "ix_alerts_tenant_state", "alerts", ["tenant_id", "state"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_alerts_tenant_state", table_name="alerts")
    op.drop_index("ix_alerts_due", table_name="alerts")
    op.drop_index(op.f("ix_alerts_execution_id"), table_name="alerts")
    op.drop_index(op.f("ix_alerts_task_id"), table_name="alerts")
    op.drop_index(op.f("ix_alerts_tenant_id"), table_name="alerts")
    op.drop_table("alerts")
