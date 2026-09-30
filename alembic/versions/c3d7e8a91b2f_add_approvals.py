"""add approvals and approval_rule_suggestions

Revision ID: c3d7e8a91b2f
Revises: 9ca1c4a9906e
Create Date: 2026-09-24 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'c3d7e8a91b2f'
down_revision: str | None = '9ca1c4a9906e'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'approvals',
        sa.Column('user_id', sa.UUID(), nullable=False),
        sa.Column('task_id', sa.UUID(), nullable=False),
        sa.Column('tool_name', sa.String(length=128), nullable=False),
        sa.Column('risk_level', sa.String(length=16), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column(
            'recipient_class', sa.String(length=64), server_default='',
            nullable=False
        ),
        sa.Column('intent_hash', sa.String(length=64), nullable=False),
        sa.Column('request_summary', sa.Text(), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('decided_by', sa.UUID(), nullable=True),
        sa.Column('decision_reason', sa.Text(), nullable=True),
        sa.Column('consumed_at', sa.DateTime(timezone=True), nullable=True),
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
        comment='tenant-scoped single-use tool approval request'
    )
    op.create_index(op.f('ix_approvals_tenant_id'), 'approvals', ['tenant_id'], unique=False)
    op.create_index(op.f('ix_approvals_user_id'), 'approvals', ['user_id'], unique=False)
    op.create_index(op.f('ix_approvals_task_id'), 'approvals', ['task_id'], unique=False)
    op.create_index(op.f('ix_approvals_status'), 'approvals', ['status'], unique=False)
    op.create_index(op.f('ix_approvals_intent_hash'), 'approvals', ['intent_hash'], unique=False)
    op.create_index(op.f('ix_approvals_expires_at'), 'approvals', ['expires_at'], unique=False)
    op.create_index(
        'ix_approvals_dedup', 'approvals',
        ['tenant_id', 'user_id', 'task_id', 'tool_name', 'status'],
        unique=False
    )
    op.create_index(
        'uq_approvals_pending_intent', 'approvals',
        ['tenant_id', 'user_id', 'task_id', 'tool_name', 'intent_hash'],
        unique=True,
        postgresql_where=sa.text("status = 'PENDING'")
    )

    op.create_table(
        'approval_rule_suggestions',
        sa.Column('user_id', sa.UUID(), nullable=False),
        sa.Column('tool_name', sa.String(length=128), nullable=False),
        sa.Column('risk_level', sa.String(length=16), nullable=False),
        sa.Column(
            'recipient_class', sa.String(length=64), server_default='',
            nullable=False
        ),
        sa.Column('allowed_tools', postgresql.JSONB(astext_type=sa.Text()),
                  nullable=False),
        sa.Column('allowed_recipients', postgresql.JSONB(astext_type=sa.Text()),
                  nullable=False),
        sa.Column('risk_ceiling', sa.String(length=16), nullable=False),
        sa.Column('expiration', sa.DateTime(timezone=True), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('rejected_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('reject_reason', sa.Text(), nullable=True),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('tenant_id', sa.UUID(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        comment='non-authorizing approval-fatigue rule proposal'
    )
    op.create_index(
        op.f('ix_approval_rule_suggestions_tenant_id'),
        'approval_rule_suggestions', ['tenant_id'], unique=False
    )
    op.create_index(
        op.f('ix_approval_rule_suggestions_user_id'),
        'approval_rule_suggestions', ['user_id'], unique=False
    )
    op.create_index(
        op.f('ix_approval_rule_suggestions_status'),
        'approval_rule_suggestions', ['status'], unique=False
    )
    op.create_index(
        'uq_approval_suggestions_offered', 'approval_rule_suggestions',
        ['tenant_id', 'user_id', 'tool_name', 'risk_level', 'recipient_class'],
        unique=True,
        postgresql_where=sa.text("status = 'OFFERED'")
    )


def downgrade() -> None:
    op.drop_index(
        'uq_approval_suggestions_offered', table_name='approval_rule_suggestions'
    )
    op.drop_index(
        op.f('ix_approval_rule_suggestions_status'),
        table_name='approval_rule_suggestions'
    )
    op.drop_index(
        op.f('ix_approval_rule_suggestions_user_id'),
        table_name='approval_rule_suggestions'
    )
    op.drop_index(
        op.f('ix_approval_rule_suggestions_tenant_id'),
        table_name='approval_rule_suggestions'
    )
    op.drop_table('approval_rule_suggestions')

    op.drop_index('uq_approvals_pending_intent', table_name='approvals')
    op.drop_index('ix_approvals_dedup', table_name='approvals')
    op.drop_index(op.f('ix_approvals_expires_at'), table_name='approvals')
    op.drop_index(op.f('ix_approvals_intent_hash'), table_name='approvals')
    op.drop_index(op.f('ix_approvals_status'), table_name='approvals')
    op.drop_index(op.f('ix_approvals_task_id'), table_name='approvals')
    op.drop_index(op.f('ix_approvals_user_id'), table_name='approvals')
    op.drop_index(op.f('ix_approvals_tenant_id'), table_name='approvals')
    op.drop_table('approvals')