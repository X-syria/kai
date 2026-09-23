from fastapi import FastAPI

from kai.api.router import api_router
from kai.config import get_settings
from kai.logging import configure_logging
from kai.middleware import RequestContextMiddleware


def create_app() -> FastAPI:
    configure_logging()
    app = FastAPI(
        title=get_settings().app_name,
        description="SaaS-ready multi-tenant personal AI agent",
        version="0.1.0",
    )
    app.add_middleware(RequestContextMiddleware)
    app.include_router(api_router)
    return app


app = create_app()
