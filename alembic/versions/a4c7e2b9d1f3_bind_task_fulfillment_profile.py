"""bind task contract to a fulfillment profile

Revision ID: a4c7e2b9d1f3
Revises: b3d5f7a9c1e2
Create Date: 2026-09-27 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'a4c7e2b9d1f3'
down_revision: str | None = 'b3d5f7a9c1e2'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The Task Contract now names which deterministic evaluator answers its
    # success_criteria. Sealing it on the task row is what lets a resumed run
    # re-derive the same evaluator from persisted state instead of from a
    # mutable runtime or model value.
    #
    # The server default is migration scaffolding, not the semantic path: every
    # code path that seals a contract sets the column explicitly, and the only
    # profile in the closed set is 'calendar_meeting'. It exists so rows written
    # before this column keep loading.
    op.add_column(
        'tasks',
        sa.Column(
            'fulfillment_profile',
            sa.String(length=64),
            nullable=False,
            server_default='calendar_meeting',
            comment='which deterministic fulfillment evaluator answers this contract',
        ),
    )


def downgrade() -> None:
    op.drop_column('tasks', 'fulfillment_profile')
