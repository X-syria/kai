"""add revoked_tokens for access-token revocation

Revision ID: d4e5f6a7b8c9
Revises: c8e1f3a5b7d9
Create Date: 2026-09-30 12:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4e5f6a7b8c9"
down_revision: str | None = "c8e1f3a5b7d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "revoked_tokens",
        sa.Column("jti", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "revoked_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("jti"),
    )
    op.create_index(
        op.f("ix_revoked_tokens_user_id"), "revoked_tokens", ["user_id"], unique=False
    )
    op.create_index(
        op.f("ix_revoked_tokens_tenant_id"),
        "revoked_tokens",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_revoked_tokens_expires_at"),
        "revoked_tokens",
        ["expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_revoked_tokens_expires_at"), table_name="revoked_tokens")
    op.drop_index(op.f("ix_revoked_tokens_tenant_id"), table_name="revoked_tokens")
    op.drop_index(op.f("ix_revoked_tokens_user_id"), table_name="revoked_tokens")
    op.drop_table("revoked_tokens")
