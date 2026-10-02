from fastapi import APIRouter

from kai.db.engine import (  # noqa: F401  (kept importable API)
    CheckDatabaseError,
    check_database_reachable,
)
from kai.observability import metrics

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict:
    """Report readiness, and record it so "has it ever been ready" is visible.

    The endpoint returns HTTP 200 either way and lets the body carry the
    verdict, which is right for a probe but wrong for a metric: a monitoring
    system that only saw status codes could not tell a healthy replica from a
    degraded one. So the outcome is counted here as well.
    """
    try:
        await check_database_reachable()
    except CheckDatabaseError as exc:
        metrics.record_db_error("health")
        return {"status": "degraded", "database": "unreachable", "detail": str(exc)}
    return {"status": "ok", "database": "reachable"}