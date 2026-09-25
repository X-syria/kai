"""The :class:`RuntimeExecution` ORM row (one per agent-runtime task).

The row keeps the agent runtime's *resume contract*: the validated scenario,
the resolved attendee identities and their evidence, the list of tool-run
records (evidence), and the progress cursor. It exists so a task can stop at a
human approval checkpoint and resume later without re-running the model or
re-executing completed steps, and so a re-trigger can never create a duplicate
calendar event.

``plan_version``/``plan_hash`` are what the ``cursor`` is bound to. The cursor
counts the steps of exactly one plan, so the row records that plan's version and
a content hash of its steps: a re-plan after execution started can never be
resumed against the old cursor (which would silently skip the new plan's steps
or re-run finished ones). Both stay ``None`` until the first trigger adopts a
plan, and are only re-bound when no step has run yet.

``claim_token``/``claimed_at`` are the runtime's per-task execution claim: the
single-trigger lease that makes a concurrent or duplicate resume a no-op instead
of a second writer racing the owner into a different final state. It is a
committed reservation, never a held transaction or row lock, so no database
lock is ever kept across an external provider call. A claim older than the lease
is treated as abandoned by a crashed worker and may be taken over.

Explicit tenants-and-users FK scope mirrors the Task row; ``task_id`` is
unique so at most one execution exists per task. No secrets are stored here:
no tokens, credentials, or message bodies.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from kai.db.base import Base, PKMixin, TenantMixin, TimestampMixin


class RuntimeExecution(PKMixin, TenantMixin, TimestampMixin, Base):
    """Agent-runtime execution state, 1:1 with a ``tasks.id`` row."""

    __tablename__ = "runtime_executions"
    __table_args__ = (
        {"comment": "agent-runtime execution state for one task"},
    )

    task_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)

    scenario: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    identities: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    evidence: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    cursor: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    steps_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: What the cursor above is counting: one plan version plus the hash of its
    #: steps. Nullable until the first trigger binds it.
    plan_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    plan_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    claim_token: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True, index=True
    )
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


__all__ = ["RuntimeExecution"]