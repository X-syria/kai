"""add intent_attempts: durable INTENT attempt record (O1-B)

The gateway already claims an approval's single use atomically, but nothing
durable recorded that a claim was attempted. This revision adds the table that
one gateway invocation writes immediately *before* ``consume``: one row per
attempt, carrying the exact intent binding (``intent_hash``) and the approval it
was about to use.

What this table deliberately is not:

* It is not a permission, a lease, a dispatch permit, or a retry token. Nothing
  reads it on the execution path and nothing consults it to authorize anything.
* It does not prove an external effect happened, or that one did not.
* It does not make the attempt durable before consumption starts. The gateway
  adds the row to the caller's session; durability comes from the commit that
  ``ApprovalService.consume`` performs (``approvals/service.py``). On a
  compare-and-set failure that commit still happens *before* the raise, so a row
  here can be durable while the approval was not consumed -- there is no "both
  or nothing" atomicity. A crash before the commit loses the row entirely.

``authorization_id`` / ``authorized_at_utc`` are the shape an L2 (AUTHORIZED)
writer will fill. **No writer exists in this revision**, so both stay NULL for
every row the current code writes; they exist only so a read-only reader can
answer "no AUTHORIZED reference is recorded" without inventing a verdict.

Indexes: ``(tenant_id, user_id, created_at)`` answers the one query the reader
makes -- one tenant/user's attempts newest-first. ``task_id`` is indexed because
the FK cascades on task deletion. ``tenant_id`` is indexed by the tenancy mixin,
as on every other tenant-scoped table.

Upgrade: additive only; no existing table, column, or constraint is touched.
Downgrade drops the table, which discards attempt records only -- no approval,
task, or account state depends on it.

Parent: ``e6f7a8b9c0d1`` (``add_alert_outbox``), the last revision on the
tracked chain. This table has no dependency on any later revision -- its only
foreign keys are ``tenants``/``users``/``tasks`` -- so it deliberately does not
chain off the untracked, unapproved ``b2f7c1d9e4a6`` (deferred Google linking)
that happens to be present in some working trees. Chaining off it would make
this migration undeliverable wherever that local work is absent.

Revision ID: a7c9e1f3b5d2
Revises: e6f7a8b9c0d1
Create Date: 2026-10-24 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7c9e1f3b5d2"
down_revision: str | None = "e6f7a8b9c0d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "intent_attempts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("task_id", sa.UUID(), nullable=False),
        # Correlator, not an FK: an attempt record must not be cascade-deleted
        # with (or block deletion of) the approval row.
        sa.Column("approval_id", sa.UUID(), nullable=False),
        sa.Column("tool_name", sa.String(length=128), nullable=False),
        sa.Column("intent_hash", sa.String(length=64), nullable=False),
        sa.Column("account_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # L2 (AUTHORIZED) substrate. No writer exists yet: both are NULL for
        # every row this revision can produce.
        sa.Column("authorization_id", sa.UUID(), nullable=True),
        sa.Column("authorized_at_utc", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        comment="durable INTENT attempt record; not evidence of dispatch",
    )
    op.create_index(
        op.f("ix_intent_attempts_tenant_id"),
        "intent_attempts",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_intent_attempts_task_id"),
        "intent_attempts",
        ["task_id"],
        unique=False,
    )
    # The reader's only predicate + ordering: one tenant/user, newest first.
    op.create_index(
        "ix_intent_attempts_tenant_user_created",
        "intent_attempts",
        ["tenant_id", "user_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_intent_attempts_tenant_user_created", table_name="intent_attempts")
    op.drop_index(op.f("ix_intent_attempts_task_id"), table_name="intent_attempts")
    op.drop_index(op.f("ix_intent_attempts_tenant_id"), table_name="intent_attempts")
    op.drop_table("intent_attempts")
