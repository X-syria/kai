from fastapi import APIRouter

from kai.db.engine import (  # noqa: F401  (kept importable API)
    CheckDatabaseError,
    check_database_reachable,
)

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict:
    try:
        await check_database_reachable()
    except CheckDatabaseError as exc:
        return {"status": "degraded", "database": "unreachable", "detail": str(exc)}
    return {"status": "ok", "database": "reachable"}
