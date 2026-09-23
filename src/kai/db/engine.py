from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from kai.config import get_settings


def _build_engine() -> AsyncEngine:
    return create_async_engine(
        get_settings().database_url,
        echo=False,
        pool_pre_ping=True,
    )


engine: AsyncEngine = _build_engine()


async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class CheckDatabaseError(RuntimeError):
    """Raised when the database cannot be reached (health-check plumbing)."""


async def check_database_reachable() -> None:
    """Run ``SELECT 1``; raise :class:`CheckDatabaseError` on any failure."""
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - normalise connectivity failures
        raise CheckDatabaseError(f"database unreachable: {exc}") from exc


async def get_session() -> AsyncIterator[AsyncSession]:
    """Yield one request-scoped :class:`AsyncSession`."""
    async with async_session_factory() as session:
        yield session
