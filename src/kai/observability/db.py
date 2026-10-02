"""Database failure accounting.

One SQLAlchemy ``handle_error`` hook on the shared engine is the whole seam.
Attaching it centrally means every database failure in the service is counted,
including failures inside the maintenance command, which never passes through
the request path.

The hook is strictly observational: it records and returns. It never swallows,
re-raises, or alters an exception, so adding it cannot change database
behaviour.
"""

from __future__ import annotations

from typing import Any, Final

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine

from kai.observability import metrics

#: Bounded operation classes. A raw SQL string or a table name would be an
#: unbounded label, and the operationally useful answer to "the database is
#: failing" is *which phase* failed, not which query.
DB_OP_EXECUTE: Final[str] = "execute"
DB_OP_TRANSACTION: Final[str] = "transaction"
DB_OP_DISCONNECT: Final[str] = "disconnect"

_installed_engines: set[int] = set()


def install_db_instrumentation(engine: AsyncEngine) -> None:
    """Attach the error hook to ``engine``. Idempotent per engine instance.

    Idempotent because it is called both when the app is built and when the
    maintenance command runs, and registering the same listener twice would
    double-count every failure.

    Only ``handle_error`` is used. SQLAlchemy's ``engine_disconnected`` is not
    registerable against an Engine, and a connection silently recycled by
    ``pool_pre_ping`` is not a failure the application saw - counting it would
    report pool churn as database errors. A disconnect that actually reaches a
    caller is classified by :func:`_classify` instead.
    """
    key = id(engine)
    if key in _installed_engines:
        return
    _installed_engines.add(key)

    @event.listens_for(engine.sync_engine, "handle_error")
    def _record(error_context: Any) -> None:
        metrics.record_db_error(_classify(error_context))


def _classify(error_context: Any) -> str:
    """Map a SQLAlchemy error context onto a bounded operation class."""
    original = getattr(error_context, "original_exception", None)
    if original is not None and _is_disconnect(original):
        return DB_OP_DISCONNECT
    # No statement means the failure was in the transaction machinery itself
    # (BEGIN / COMMIT / ROLLBACK), not inside a specific query.
    if getattr(error_context, "statement", None) is None:
        return DB_OP_TRANSACTION
    return DB_OP_EXECUTE


def _is_disconnect(exc: BaseException) -> bool:
    """True for a connection-level loss, without a hard driver dependency."""
    # SQLAlchemy wraps these as InterfaceError / OperationalError, and the
    # asyncpg class is ConnectionDoesNotExistError. Matching on the class name
    # covers asyncpg and psycopg alike, which matters because KAI supports
    # either driver through its URL.
    name = type(exc).__name__.lower()
    return any(
        marker in name
        for marker in ("interfaceerror", "operationalerror", "connectiondoesnotexist")
    )