from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kai.db.models import User


async def get_user_for_tenant(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    user_id: UUID,
) -> User | None:
    """Fetch a user only if it belongs to ``tenant_id``.

    This is the central tenant-isolation lookup: the ``WHERE`` clause includes
    both the tenant and the record id, so a ``user_id`` that exists under a
    different tenant simply does not resolve here. Returns ``None`` on miss.
    """
    result = await session.execute(
        select(User).where(User.tenant_id == tenant_id, User.id == user_id)
    )
    return result.scalar_one_or_none()


async def get_for_tenant(
    session: AsyncSession,
    model: type[User],
    *,
    tenant_id: UUID,
    record_id: UUID,
) -> User | None:
    """Generic tenant-scoped fetch. MVP only supports ``User`` records."""
    if model is not User:
        raise TypeError(f"tenant scoping is not implemented for {model.__name__}")
    return await get_user_for_tenant(
        session, tenant_id=tenant_id, user_id=record_id
    )
