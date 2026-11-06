
from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
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


#: The closed status set of a :class:`GoogleOAuthPending` row.
GOOGLE_PENDING_STATUSES: tuple[str, ...] = (
    "pending",
    "consumed",
    "cancelled",
    "expired",
    "failed",
)

#: The closed failure-reason set. Bounded identifiers, never provider text or an
#: exception message: these values are stored and may be shown to a client.
GOOGLE_PENDING_FAILURE_REASONS: tuple[str, ...] = (
    "ownership_conflict",
    "refresh_token_required",
    "reconnect_required",
    "credential_error",
    "integrity_error",
    "expired",
)


def _sql_string_list(values: tuple[str, ...]) -> str:
    """Render ``values`` as a SQL ``IN`` list of quoted literals.

    The values are module constants, never user input, so interpolation is the
    same thing a hand-written constraint does - but keeping one spelling means
    the ORM constraint and the service cannot drift.
    """
    return ", ".join(f"'{value}'" for value in values)


class GoogleOAuthPending(PKMixin, TenantMixin, TimestampMixin, Base):
    """A Google link authorization that was proven but not yet confirmed.

    The callback proves control of the Google identity and of the browser, then
    stops: it stores the freshly issued (encrypted) credentials here instead of
    writing ``google_accounts``. The account is only linked after the signed-in
    KAI user explicitly confirms, which is what closes the consent-linking gap
    D41 addresses - a Google login that returns a code for somebody else's
    Google identity can no longer attach that identity on its own.

    Only digests are stored for the two bearer values the browser holds: the
    confirmation token (a ``__Host-`` cookie) and the ``state``. Neither raw
    value can be recovered from a database dump, and neither is logged.

    ``credentials_encrypted`` / ``token_expires_at`` are nullable because both
    are cleared the moment the row leaves ``pending``: a consumed, cancelled,
    expired, or failed row must not retain a bearer token. ``status`` and
    ``failure_reason`` are closed sets enforced by ``CHECK`` constraints, so a
    provider or library message can never be stored in either.
    """

    __tablename__ = "google_oauth_pending"
    __table_args__ = (
        UniqueConstraint(
            "confirmation_hash",
            name="uq_google_oauth_pending_confirmation_hash",
        ),
        UniqueConstraint("state_hash", name="uq_google_oauth_pending_state_hash"),
        CheckConstraint(
            f"status IN ({_sql_string_list(GOOGLE_PENDING_STATUSES)})",
            name="ck_google_oauth_pending_status",
        ),
        CheckConstraint(
            "failure_reason IS NULL OR failure_reason IN"
            f" ({_sql_string_list(GOOGLE_PENDING_FAILURE_REASONS)})",
            name="ck_google_oauth_pending_failure_reason",
        ),
        Index(
            "ix_google_oauth_pending_tenant_user", "tenant_id", "user_id"
        ),
        Index(
            "ix_google_oauth_pending_status_expires", "status", "expires_at"
        ),
    )

    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    #: SHA-256 hex digest of the raw confirmation token. The raw value lives
    #: only in the browser cookie, so the database alone cannot confirm a link.
    confirmation_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    #: SHA-256 hex digest of the signed ``state`` this row was created for, so a
    #: replayed callback cannot mint a second transaction.
    state_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    google_sub: Mapped[str] = mapped_column(String(255), nullable=False)
    google_email: Mapped[str] = mapped_column(String(255), nullable=False)
    email_verified: Mapped[bool] = mapped_column(Boolean, nullable=False)
    scopes: Mapped[str] = mapped_column(String(255), nullable=False)
    #: False when the token response omitted ``scope`` and the stored list is
    #: the defensive fallback (the requested scopes), so nothing downstream can
    #: claim Google confirmed a grant it never reported.
    scopes_from_provider: Mapped[bool] = mapped_column(Boolean, nullable=False)
    credentials_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    failure_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)


class IntentAttempt(PKMixin, TenantMixin, Base):
    """A durable INTENT attempt: the gateway reached the single-use claim.

    One row is written by the Tool Gateway immediately *before* it asks the
    Approval Manager to consume an APPROVED approval, using the caller's own
    session (:mod:`kai.tools.gateway`). It is the L1 (INTENT) link of the audit
    chain: it records that one exact intent was about to be dispatched under one
    approval. ``authorization_id`` / ``authorized_at_utc`` are the shape an L2
    (AUTHORIZED) writer will fill; **no writer exists today**, so every row
    written by the current code has both NULL. Nothing here is a permission, a
    claim, a lease, or a retry token.

    Accepted limits -- kept next to the data because they bound every reading
    of it:

    1. This row is only ``session.add``-ed. It is **not** guaranteed to be
       durable before consumption starts; it becomes durable at the next commit
       of that session, which today is the commit inside
       ``ApprovalService.consume``.
    2. On a compare-and-set failure ``consume`` commits the caller's session
       *before* raising (``approvals/service.py``), so this attempt row can be
       durable while **this attempt** did not consume the approval. That is not
       the same as "the approval is unconsumed": a concurrent attempt may have
       consumed it (limit 4). Do not read a durable row here as "the approval
       was used", and do not claim "both or nothing" -- there is no atomicity
       between the record and the single-use claim.
    3. A crash (or any error) before that commit loses the row entirely. This is
       not a write-ahead log and cannot be replayed to recover a lost attempt.
    4. After a CAS failure the approval must not be assumed intact: a
       concurrent attempt may already have consumed it. Re-read its state.
    5. A row proves nothing about whether an external effect occurred, whether
       one is permitted, or whether a retry is safe.

    Retention -- this is **not** a permanent audit record. ``tenant_id``,
    ``user_id``, and ``task_id`` are ``ON DELETE CASCADE`` foreign keys, so
    deleting the owning tenant, user, or task deletes its attempt rows with it.
    ``approval_id`` and ``account_id`` are deliberately *not* foreign keys, so an
    attempt outlives deletion of the approval or account it names -- but only
    until its owning tenant/user/task is deleted. There is no archive, tombstone,
    soft delete, or retention job: a deleted attempt is gone, and its absence is
    therefore not evidence that no attempt was ever made.
    """

    __tablename__ = "intent_attempts"
    __table_args__ = (
        # The reader's only query: one tenant/user's newest attempts first.
        Index(
            "ix_intent_attempts_tenant_user_created",
            "tenant_id",
            "user_id",
            "created_at",
        ),
        {"comment": "durable INTENT attempt record; not evidence of dispatch"},
    )

    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    task_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    #: Correlator to the approval whose single use was about to be claimed.
    #: Deliberately a plain UUID, not an FK (the same reasoning as
    #: ``Approval.decided_by``): an attempt record is audit substrate and must
    #: not be cascade-deleted with, or block deletion of, the approval row.
    approval_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    #: The same SHA-256 intent binding the approval stores, recomputed by the
    #: gateway from the validated arguments and the context's account.
    intent_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The account the intent would run through; plain UUID for the same reason
    #: as ``approval_id`` (an account can be disconnected or re-bound).
    account_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    #: Database time, not application time: the record must not depend on the
    #: writer's clock.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    #: L2 (AUTHORIZED) substrate only. NO WRITER EXISTS YET; every row the
    #: current code writes leaves both NULL. They exist so a read-only reader can
    #: answer "no AUTHORIZED reference is recorded for this attempt" without
    #: inventing a verdict. They grant nothing and are not consulted by any
    #: authorization path.
    authorization_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    authorized_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
