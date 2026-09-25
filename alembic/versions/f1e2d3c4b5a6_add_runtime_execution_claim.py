"""add runtime execution claim

Revision ID: f1e2d3c4b5a6
Revises: e9f8a7b6c5d4
Create Date: 2026-09-25 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'f1e2d3c4b5a6'
down_revision: str | None = 'e9f8a7b6c5d4'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'runtime_executions', sa.Column('claim_token', sa.UUID(), nullable=True)
    )
    op.add_column(
        'runtime_executions',
        sa.Column('claimed_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        op.f('ix_runtime_executions_claim_token'),
        'runtime_executions',
        ['claim_token'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f('ix_runtime_executions_claim_token'), table_name='runtime_executions'
    )
    op.drop_column('runtime_executions', 'claimed_at')
    op.drop_column('runtime_executions', 'claim_token')
