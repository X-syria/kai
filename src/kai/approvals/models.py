"""Approval Manager ORM models.

Tenancy is a hard FK into ``tenants.id``; every query in
:mod:`kai.approvals.service` filters on ``tenant_id`` (plus ``user_id``) so an
approval can never be read or decided through another tenant's / user's context.

No secrets are ever stored here: ``intent_hash`` is a SHA-256 of the canonical
tool intent (tool name + normalized arguments + account), and
``request_summary`` is a human-readable, safe preview. Raw message bodies,
tokens, and authorization headers never reach this table.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
)
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from kai.db.base import Base, PKMixin, TenantMixin, TimestampMixin

#: At most one PENDING approval may exist for any exact intent
#: ``(tenant, user, task, tool, intent_hash)``; settled rows (APPROVED/consumed,
#: DENIED, EXPIRED) do not block a brand-new request for the same intent.
_PENDING_INTENT_INDEX = Index(
    "uq_approvals_pending_intent",
    "tenant_id",
    "user_id",
    "task_id",
    "tool_name",
    "intent_hash",
    unique=True,
    postgresql_where=sa_text("status = 'PENDING'"),
)

#: Exactly one OFFERED suggestion per scope; rejection frees the slot so the
#: cooldown-window logic can eventually offer a fresh one.
_OFFERED_SUGGESTION_INDEX = Index(
    "uq_approval_suggestions_offered",
    "tenant_id",
    "user_id",
    "tool_name",
    "risk_level",
    "recipient_class",
    unique=True,
    postgresql_where=sa_text("status = 'OFFERED'"),
)


class ApprovalStatus(StrEnum):
    """Lifecycle of one approval request. Terminal states are immutable.

    ``EXPIRED`` is a lazy, deterministic terminal state: a PENDING or APPROVED
    approval whose ``expires_at`` has passed is never usable and is rolled to
    EXPIRED on the next read or check. There is deliberately no "standing" or
    repeated-use status — an approval authorizes one execution of one exact
    intent.
    """

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    DENIED = "DENIED"
    EXPIRED = "EXPIRED"


class Approval(PKMixin, TenantMixin, TimestampMixin, Base):
    """A tenant/user/task-scoped request for one exact tool intent."""

    __tablename__ = "approvals"
    __table_args__ = (
        Index(
            "ix_approvals_dedup",
            "tenant_id",
            "user_id",
            "task_id",
            "tool_name",
            "status",
        ),
        _PENDING_INTENT_INDEX,
        {"comment": "tenant-scoped single-use tool approval request"},
    )

    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    task_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    risk_level: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    #: Classification label of the intent's recipient (e.g. "internal"/"external")
    #: used by the non-authorizing fatigue suggestion; never a secret or an email.
    recipient_class: Mapped[str] = mapped_column(
        String(64), nullable=False, default=""
    )
    intent_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )
    request_summary: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # ``decided_by`` is audit metadata only (the authenticated principal that
    # made the decision) — deliberately a plain UUID, not an FK, so a decision
    # survives the subject being removed.
    decided_by: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    decision_reason: Mapped[str | None] = mapped_column(Text)
    # Single-use marker: set when an APPROVED approval authorizes its one
    # execution. A consumed approval can never authorize again.
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def is_terminal(self) -> bool:
        return self.status != ApprovalStatus.PENDING.value


class ApprovalRuleSuggestion(PKMixin, TenantMixin, TimestampMixin, Base):
    """An explicit *non-authorizing* proposal produced by approval fatigue.

    A suggestion is never a permission: it holds no grant, is never consulted
    by the gateway, and cannot override Policy. It is recorded so a future
    Policy-rules slice can turn an accepted suggestion into a real rule, and
    so a rejected suggestion can start the cooldown. ``status`` owns the
    OFFERED -> REJECTED lifecycle only; acceptance is out of scope here.
    """

    __tablename__ = "approval_rule_suggestions"
    __table_args__ = (
        _OFFERED_SUGGESTION_INDEX,
        {"comment": "non-authorizing approval-fatigue rule proposal"},
    )

    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    risk_level: Mapped[str] = mapped_column(String(16), nullable=False)
    recipient_class: Mapped[str] = mapped_column(
        String(64), nullable=False, default=""
    )
    allowed_tools: Mapped[list[str]] = mapped_column(
        JSONB, default=list, nullable=False
    )
    allowed_recipients: Mapped[list[str]] = mapped_column(
        JSONB, default=list, nullable=False
    )
    risk_ceiling: Mapped[str] = mapped_column(String(16), nullable=False)
    expiration: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reject_reason: Mapped[str | None] = mapped_column(Text)


__all__ = ["Approval", "ApprovalRuleSuggestion", "ApprovalStatus"]