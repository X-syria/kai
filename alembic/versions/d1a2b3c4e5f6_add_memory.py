"""add memory_entries and memory_preferences

Revision ID: d1a2b3c4e5f6
Revises: c3d7e8a91b2f
Create Date: 2026-09-24 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'd1a2b3c4e5f6'
down_revision: str | None = 'c3d7e8a91b2f'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'memory_entries',
        sa.Column('user_id', sa.UUID(), nullable=False),
        sa.Column('memory_type', sa.String(length=32), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('source', sa.String(length=32), nullable=False),
        sa.Column('confidence', sa.Float(), nullable=False),
        sa.Column('importance', sa.Float(), nullable=False),
        sa.Column('recurrence', sa.Float(), nullable=False),
        sa.Column('future_relevance', sa.Float(), nullable=False),
        sa.Column('ttl', sa.String(length=16), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('supersedes_id', sa.UUID(), nullable=True),
        sa.Column('superseded_by_id', sa.UUID(), nullable=True),
        sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('tenant_id', sa.UUID(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint(
            'confidence >= 0 AND confidence <= 1',
            name='ck_memory_entries_confidence_range'
        ),
        sa.CheckConstraint(
            'importance >= 0 AND importance <= 1',
            name='ck_memory_entries_importance_range'
        ),
        sa.CheckConstraint(
            'recurrence >= 0 AND recurrence <= 1',
            name='ck_memory_entries_recurrence_range'
        ),
        sa.CheckConstraint(
            'future_relevance >= 0 AND future_relevance <= 1',
            name='ck_memory_entries_future_relevance_range'
        ),
        sa.ForeignKeyConstraint(
            ['supersedes_id'], ['memory_entries.id'], ondelete='SET NULL'
        ),
        sa.ForeignKeyConstraint(
            ['superseded_by_id'], ['memory_entries.id'], ondelete='SET NULL'
        ),
        sa.ForeignKeyConstraint(
            ['tenant_id'], ['tenants.id'], ondelete='CASCADE'
        ),
        sa.ForeignKeyConstraint(
            ['user_id'], ['users.id'], ondelete='CASCADE'
        ),
        sa.PrimaryKeyConstraint('id'),
        comment='tenant+user-scoped deterministic memory'
    )
    op.create_index(
        op.f('ix_memory_entries_tenant_id'), 'memory_entries',
        ['tenant_id'], unique=False
    )
    op.create_index(op.f('ix_memory_entries_user_id'), 'memory_entries', ['user_id'], unique=False)
    op.create_index(
        op.f('ix_memory_entries_memory_type'), 'memory_entries',
        ['memory_type'], unique=False
    )
    op.create_index(
        op.f('ix_memory_entries_source'), 'memory_entries', ['source'], unique=False
    )
    op.create_index(
        op.f('ix_memory_entries_status'), 'memory_entries', ['status'], unique=False
    )
    op.create_index(
        op.f('ix_memory_entries_expires_at'), 'memory_entries',
        ['expires_at'], unique=False
    )

    op.create_table(
        'memory_preferences',
        sa.Column('user_id', sa.UUID(), nullable=False),
        sa.Column('disabled_types', postgresql.JSONB(astext_type=sa.Text()),
                  nullable=False),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('tenant_id', sa.UUID(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'tenant_id', 'user_id', name='uq_memory_preferences_tenant_user'
        ),
        comment='per-user memory category preferences (disabled retrieval set)'
    )
    op.create_index(
        op.f('ix_memory_preferences_tenant_id'), 'memory_preferences',
        ['tenant_id'], unique=False
    )
    op.create_index(
        op.f('ix_memory_preferences_user_id'), 'memory_preferences',
        ['user_id'], unique=False
    )


def downgrade() -> None:
    op.drop_index(
        op.f('ix_memory_preferences_user_id'), table_name='memory_preferences'
    )
    op.drop_index(
        op.f('ix_memory_preferences_tenant_id'), table_name='memory_preferences'
    )
    op.drop_table('memory_preferences')

    op.drop_index(op.f('ix_memory_entries_expires_at'), table_name='memory_entries')
    op.drop_index(op.f('ix_memory_entries_status'), table_name='memory_entries')
    op.drop_index(op.f('ix_memory_entries_source'), table_name='memory_entries')
    op.drop_index(
        op.f('ix_memory_entries_memory_type'), table_name='memory_entries'
    )
    op.drop_index(op.f('ix_memory_entries_user_id'), table_name='memory_entries')
    op.drop_index(op.f('ix_memory_entries_tenant_id'), table_name='memory_entries')
    op.drop_table('memory_entries')