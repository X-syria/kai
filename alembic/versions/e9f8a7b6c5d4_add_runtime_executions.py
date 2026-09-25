"""add runtime_executions

Revision ID: e9f8a7b6c5d4
Revises: d1a2b3c4e5f6
Create Date: 2026-09-25 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'e9f8a7b6c5d4'
down_revision: str | None = 'd1a2b3c4e5f6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'runtime_executions',
        sa.Column('task_id', sa.UUID(), nullable=False),
        sa.Column('user_id', sa.UUID(), nullable=False),
        sa.Column('timezone', sa.String(length=64), nullable=False),
        sa.Column('scenario', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            'identities',
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            'evidence',
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column('cursor', sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column(
            'steps_used', sa.Integer(), nullable=False, server_default=sa.text('0')
        ),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('tenant_id', sa.UUID(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        comment='agent-runtime execution state for one task'
    )
    op.create_index(
        op.f('ix_runtime_executions_tenant_id'), 'runtime_executions',
        ['tenant_id'], unique=False
    )
    op.create_index(
        op.f('ix_runtime_executions_user_id'), 'runtime_executions',
        ['user_id'], unique=False
    )
    op.create_index(
        op.f('ix_runtime_executions_task_id'), 'runtime_executions',
        ['task_id'], unique=True
    )


def downgrade() -> None:
    op.drop_index(op.f('ix_runtime_executions_task_id'), table_name='runtime_executions')
    op.drop_index(op.f('ix_runtime_executions_user_id'), table_name='runtime_executions')
    op.drop_index(op.f('ix_runtime_executions_tenant_id'), table_name='runtime_executions')
    op.drop_table('runtime_executions')