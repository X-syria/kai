"""The :class:`Task` ORM model plus its failure/retry envelope.

Tenancy is a real FK into ``tenants.id``; every query against this table is
scoped by ``WHERE tenant_id = ?`` so a task can never be read or mutated
through another tenant's context (see :mod:`kai.tasks.service`).
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from kai.db.base import Base, PKMixin, TenantMixin, TimestampMixin


class Task(PKMixin, TenantMixin, TimestampMixin, Base):
    """A domain task owned by exactly one ``(tenant, user)`` pair.

    The Task Contract (``goal``, ``success_criteria``, ``allowed_tools``,
    ``risk_policy``, ``deadline``, ``max_cost``, ``max_steps``,
    ``verification_method``, ``fallback``) plus failure/retry metadata live on
    the row itself, so the Planner can consume it later without any schema
    change. ``is_archived`` is the only data path removed from the default
    list queries; it is not a soft-delete tombstone.
    """

    __tablename__ = "tasks"
    __table_args__ = ({"comment": "tenant-scoped domain task state"},)

    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    state: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    is_archived: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default="false"
    )

    # --- Task Contract -----------------------------------------------------
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    success_criteria: Mapped[list[str]] = mapped_column(
        JSONB, default=list, nullable=False
    )
    allowed_tools: Mapped[list[str]] = mapped_column(
        JSONB, default=list, nullable=False
    )
    risk_policy: Mapped[str] = mapped_column(String(64), nullable=False)
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    max_cost: Mapped[float] = mapped_column(
        Float, default=0.0, nullable=False, server_default="0"
    )
    max_steps: Mapped[int] = mapped_column(
        Integer, default=20, nullable=False, server_default="20"
    )
    verification_method: Mapped[str] = mapped_column(String(64), nullable=False)
    fallback: Mapped[str] = mapped_column(Text, nullable=False)

    # --- failure / retry envelope ------------------------------------------
    attempts: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False, server_default="0"
    )
    max_retries: Mapped[int] = mapped_column(
        Integer, default=2, nullable=False, server_default="2"
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def contract_dict(self) -> dict[str, Any]:
        """The Task Contract as a plain dict (serialization surface)."""
        return {
            "goal": self.goal,
            "success_criteria": list(self.success_criteria),
            "allowed_tools": list(self.allowed_tools),
            "risk_policy": self.risk_policy,
            "deadline": self.deadline,
            "max_cost": self.max_cost,
            "max_steps": self.max_steps,
            "verification_method": self.verification_method,
            "fallback": self.fallback,
        }