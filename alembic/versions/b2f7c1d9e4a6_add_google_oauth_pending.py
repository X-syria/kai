"""add google_oauth_pending: deferred, user-confirmed Google account linking

The Google callback used to bind the account directly: a browser that returned
from Google with a valid ``code``/``state`` and the D33 binding cookie attached
the Google identity to the KAI user immediately. That is consent-linking risk -
control of a Google session was treated as consent to attach it to a KAI
account, with no explicit confirmation by the signed-in user.

This revision adds the durable intermediate state that splits the flow in two:

1. the callback exchanges the code, fetches userinfo, and writes the freshly
   issued credentials here as a *pending* row;
2. the signed-in user reviews the Google identity on a same-origin page and
   confirms, and only then is the row consumed and ``google_accounts`` written.

``confirmation_hash`` is the SHA-256 digest of the raw confirmation token that
travels to the browser in a ``__Host-`` cookie; ``state_hash`` is the digest of
the signed ``state`` the row was created for. Both are unique, so one callback
can create at most one pending row and a replay cannot mint a second. Only the
digests are stored: a database dump does not let anyone confirm a link.

``credentials_encrypted`` / ``token_expires_at`` are nullable because they are
cleared the moment the row becomes terminal. ``status`` and ``failure_reason``
are closed sets enforced by ``CHECK`` constraints: neither a library exception
message nor a provider string can ever be written into them.

Indexes: ``(tenant_id, user_id)`` answers "this user's transactions", and
``(status, expires_at)`` answers the two housekeeping predicates - resolving
expired pending rows and deleting terminal rows past their retention window.

Upgrade: additive only. No existing column, constraint, index or default is
touched, so ``google_accounts`` and every deployed row are unaffected.
Downgrade drops the table. In-flight pending transactions are discarded, which
is safe by construction: an unconfirmed transaction has never written an
account, and the credentials it held were a short-lived, unconfirmed grant.

Revision ID: b2f7c1d9e4a6
Revises: e6f7a8b9c0d1
Create Date: 2026-10-22 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b2f7c1d9e4a6"
down_revision: str | None = "e6f7a8b9c0d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_STATUSES = "'pending', 'consumed', 'cancelled', 'expired', 'failed'"
_FAILURE_REASONS = (
    "'ownership_conflict', 'refresh_token_required', 'reconnect_required',"
    " 'credential_error', 'integrity_error', 'expired'"
)


def upgrade() -> None:
    op.create_table(
        "google_oauth_pending",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("confirmation_hash", sa.String(length=64), nullable=False),
        sa.Column("state_hash", sa.String(length=64), nullable=False),
        sa.Column("google_sub", sa.String(length=255), nullable=False),
        sa.Column("google_email", sa.String(length=255), nullable=False),
        sa.Column("email_verified", sa.Boolean(), nullable=False),
        sa.Column("scopes", sa.String(length=255), nullable=False),
        sa.Column("scopes_from_provider", sa.Boolean(), nullable=False),
        # Nullable because both are cleared as soon as the row is terminal; a
        # consumed/cancelled/expired/failed row must not retain a bearer token.
        sa.Column("credentials_encrypted", sa.Text(), nullable=True),
        sa.Column(
            "token_expires_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_reason", sa.String(length=64), nullable=True),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            f"status IN ({_STATUSES})",
            name="ck_google_oauth_pending_status",
        ),
        sa.CheckConstraint(
            "failure_reason IS NULL OR failure_reason IN"
            f" ({_FAILURE_REASONS})",
            name="ck_google_oauth_pending_failure_reason",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "confirmation_hash",
            name="uq_google_oauth_pending_confirmation_hash",
        ),
        sa.UniqueConstraint("state_hash", name="uq_google_oauth_pending_state_hash"),
    )
    op.create_index(
        op.f("ix_google_oauth_pending_tenant_id"),
        "google_oauth_pending",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_google_oauth_pending_user_id"),
        "google_oauth_pending",
        ["user_id"],
        unique=False,
    )
    # "this user's transactions", without scanning the whole table.
    op.create_index(
        "ix_google_oauth_pending_tenant_user",
        "google_oauth_pending",
        ["tenant_id", "user_id"],
        unique=False,
    )
    # Both housekeeping predicates: expire pending rows, then delete terminal
    # rows past retention. Partial-free on purpose: it is one small index and
    # the two status filters are both served by it.
    op.create_index(
        "ix_google_oauth_pending_status_expires",
        "google_oauth_pending",
        ["status", "expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_google_oauth_pending_status_expires", table_name="google_oauth_pending"
    )
    op.drop_index(
        "ix_google_oauth_pending_tenant_user", table_name="google_oauth_pending"
    )
    op.drop_index(
        op.f("ix_google_oauth_pending_user_id"), table_name="google_oauth_pending"
    )
    op.drop_index(
        op.f("ix_google_oauth_pending_tenant_id"), table_name="google_oauth_pending"
    )
    op.drop_table("google_oauth_pending")
