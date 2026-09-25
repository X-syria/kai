"""bind runtime cursor to plan version

Revision ID: b3d5f7a9c1e2
Revises: f1e2d3c4b5a6
Create Date: 2026-09-25 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'b3d5f7a9c1e2'
down_revision: str | None = 'f1e2d3c4b5a6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The cursor counts steps of exactly one plan. These two columns record
    # which plan version it is counting, and the content hash that pins that
    # version's steps; a re-plan can no longer be silently resumed against the
    # old cursor. Both are nullable: a row that has not planned yet binds
    # nothing, and the runtime adopts the plan on its first trigger.
    op.add_column(
        'runtime_executions',
        sa.Column(
            'plan_version',
            sa.Integer(),
            nullable=True,
            comment='task plan version this cursor is bound to',
        ),
    )
    op.add_column(
        'runtime_executions',
        sa.Column(
            'plan_hash',
            sa.String(length=64),
            nullable=True,
            comment='sha256 of the bound plan version steps',
        ),
    )


def downgrade() -> None:
    op.drop_column('runtime_executions', 'plan_hash')
    op.drop_column('runtime_executions', 'plan_version')
