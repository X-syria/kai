"""merge intent attempts and google oauth pending

Revision ID: 7573ac048c34
Revises: a7c9e1f3b5d2, b2f7c1d9e4a6
Create Date: 2026-11-05 16:28:44.427208

"""
from collections.abc import Sequence

revision: str = '7573ac048c34'
down_revision: str | None = ('a7c9e1f3b5d2', 'b2f7c1d9e4a6')
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
