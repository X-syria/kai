"""The :class:`RuntimeExecution` ORM row (one per agent-runtime task).

The row keeps the agent runtime's *resume contract*: the validated scenario,
the resolved attendee identities and their evidence, the list of tool-run
records (evidence), and the progress cursor. It exists so a task can stop at a
human approval checkpoint and resume later without re-running the model or
re-executing completed steps, and so a re-trigger can never create a duplicate
calendar event.

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

    claim_token: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True, index=True
    )
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


__all__ = ["RuntimeExecution"]