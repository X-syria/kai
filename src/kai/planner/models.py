"""The :class:`TaskPlan` ORM model — auditable, versioned plan proposals.

A task owns an ordered history of ``TaskPlan`` rows, each carrying the
validated structured plan as ``plan_json`` plus the fixed ``model_id`` that
produced it. The latest accepted version is ``SUPERSEDED`` the moment a newer
one lands, so no plan is ever silently overwritten. Tenancy is a real FK like
every other row in the schema, and all lookups are tenant-scoped.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from kai.db.base import Base, PKMixin, TenantMixin, TimestampMixin

PLAN_STATUS_ACCEPTED = "ACCEPTED"
PLAN_STATUS_SUPERSEDED = "SUPERSEDED"


class TaskPlan(PKMixin, TenantMixin, TimestampMixin, Base):
    """One persisted plan proposal for exactly one task."""

    __tablename__ = "task_plans"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "task_id", "version", name="uq_task_plans_tenant_task_version"
        ),
    )

    task_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    plan_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    model_id: Mapped[str] = mapped_column(String(128), nullable=False)