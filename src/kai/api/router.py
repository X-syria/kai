from fastapi import APIRouter

from kai.api.auth import router as auth_router
from kai.api.calendar import router as calendar_router
from kai.api.contacts import router as contacts_router
from kai.api.gmail import router as gmail_router
from kai.api.google import router as google_router
from kai.api.health import router as health_router
from kai.api.planner import router as planner_router
from kai.api.tasks import router as tasks_router

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health_router)
api_router.include_router(auth_router)
api_router.include_router(google_router)
api_router.include_router(contacts_router)
api_router.include_router(calendar_router)
api_router.include_router(gmail_router)
api_router.include_router(tasks_router)
api_router.include_router(planner_router)
