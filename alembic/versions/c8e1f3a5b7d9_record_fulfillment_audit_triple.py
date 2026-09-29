"""record the fulfillment audit triple on runtime executions

Revision ID: c8e1f3a5b7d9
Revises: a4c7e2b9d1f3
Create Date: 2026-09-27 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'c8e1f3a5b7d9'
down_revision: str | None = 'a4c7e2b9d1f3'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Which sealed profile decided fulfillment, which version of that profile's
    # evaluator semantics ran, and what it concluded. An audit trail, so a reader
    # can ask "what was decided, and by what" without re-deriving it.
    #
    # Every column is nullable and none is backfilled, on purpose. A run that
    # never reached verification has no verdict to record, and a row written
    # before this revision has no recorded evaluator identity to recover — the
    # only honest value for it is NULL. A backfilled 'calendar_meeting:v1' would
    # assert a provenance that was never written down, and could be wrong: the
    # semantics that ran back then were the pre-rename, first-witness ones, which
    # is precisely the version identity is meant to expose. NULL therefore reads
    # as "not recorded" and never as "no evaluator ran".
    op.add_column(
        'runtime_executions',
        sa.Column(
            'fulfillment_profile',
            sa.String(length=64),
            nullable=True,
            comment='sealed contract profile whose evaluator decided fulfillment',
        ),
    )
    op.add_column(
        'runtime_executions',
        sa.Column(
            'evaluator_version',
            sa.String(length=64),
            nullable=True,
            comment='semantic version of the fulfillment evaluator that ran',
        ),
    )
    op.add_column(
        'runtime_executions',
        sa.Column(
            'fulfillment_verdict',
            sa.String(length=32),
            nullable=True,
            comment='verdict the evaluator returned: audit record, not authority',
        ),
    )


def downgrade() -> None:
    op.drop_column('runtime_executions', 'fulfillment_verdict')
    op.drop_column('runtime_executions', 'evaluator_version')
    op.drop_column('runtime_executions', 'fulfillment_profile')
