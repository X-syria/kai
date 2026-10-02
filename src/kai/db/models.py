
from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from kai.db.base import Base, PKMixin, TenantMixin, TimestampMixin


class Tenant(PKMixin, TimestampMixin, Base):
    """A tenant is the unit of ownership; MVP rule is 1 tenant == 1 user.

    Every business model carries a :attr:`tenant_id` (via ``TenantMixin``) that
    points back to this table, which lets all queries in
    :mod:`kai.db.scoping` enforce isolation at the statement level.
    """

    __tablename__ = "tenants"

    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)

    users: Mapped[list["User"]] = relationship(
        "User",
        back_populates="tenant",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class User(PKMixin, TenantMixin, TimestampMixin, Base):
    """An authenticated principal. Always scoped to exactly one tenant."""

    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("email", name="uq_users_email"),)

    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(512), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    tenant: Mapped["Tenant"] = relationship("Tenant", back_populates="users")


class RevokedToken(Base):
    """A single access token that was explicitly invalidated before expiry.

    Keyed by the token's ``jti`` (JWT id). Signing out cannot un-sign a token
    that is already in someone's hands, so revocation works by recording the
    id and refusing it on every subsequent decode. ``expires_at`` mirrors the
    token's own ``exp`` so a revoked id is only meaningful until the token
    would have expired anyway, which lets the row be purged without ever
    revoking a token that is already invalid.
    """

    __tablename__ = "revoked_tokens"

    jti: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    revoked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class GoogleAccount(PKMixin, TenantMixin, TimestampMixin, Base):
    """A Google identity connected by a user, scoped to exactly one tenant.

    A ``(tenant_id, google_sub)`` identity can exist at most once per tenant,
    so reconnecting refreshes the same row instead of duplicating it, and an
    account can never be silently re-bound to a different ``user_id`` inside
    the tenant. ``google_sub`` is Google's stable subject id, not the email.
    """

    __tablename__ = "google_accounts"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "google_sub", name="uq_google_accounts_tenant_google_sub"
        ),
    )

    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    google_sub: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    scopes: Mapped[str] = mapped_column(String(255), nullable=False)
    credentials_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
