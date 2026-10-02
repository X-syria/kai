from fastapi import FastAPI
from starlette.middleware.trustedhost import TrustedHostMiddleware

from kai.api.router import api_router
from kai.config import get_settings
from kai.logging import configure_logging
from kai.middleware import RequestContextMiddleware
from kai.security.cors import build_cors_middleware
from kai.security.ratelimit import RateLimitMiddleware

#: Unauthenticated, password-consuming routes. These are the only endpoints a
#: client can reach without a token, so they are the only ones worth limiting.
RATE_LIMITED_PATHS: tuple[str, ...] = ("/auth/login", "/auth/register")


def create_app() -> FastAPI:
    configure_logging()
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        description="SaaS-ready multi-tenant personal AI agent",
        version="0.1.0",
    )
    app.add_middleware(RequestContextMiddleware)
    # Read the limit through the cached settings object at request time rather
    # than closing over a snapshot taken at import, so an operator (or a test)
    # that changes the limit does not need the process restarted to see it.
    app.add_middleware(
        RateLimitMiddleware,
        paths=RATE_LIMITED_PATHS,
        limit=lambda: get_settings().auth_rate_limit_requests,
        window=lambda: float(get_settings().auth_rate_limit_window_seconds),
    )
    # TrustedHost is installed only when hosts are configured, because an empty
    # allowlist in Starlette rejects every request. It is a Host-header guard:
    # it stops a poisoned Host from being reflected into generated URLs and
    # keeps password-reset style links pointed at this service.
    if settings.trusted_hosts:
        app.add_middleware(
            TrustedHostMiddleware, allowed_hosts=list(settings.trusted_hosts)
        )
    cors = build_cors_middleware(settings.cors_allowed_origins)
    if cors is not None:
        # Returned as (class, kwargs) rather than a built instance: Starlette
        # needs the class so it can bind the app in the right position.
        middleware_cls, middleware_kwargs = cors
        # Starlette's _MiddlewareFactory protocol types the first positional as
        # its own ASGIApp alias, which is structurally identical to (but not
        # spelled identically as) the alias CORSMiddleware.__init__ accepts.
        # The ignore is that library variance gap, not a loose call here.
        app.add_middleware(middleware_cls, **middleware_kwargs)  # type: ignore[arg-type]
    app.include_router(api_router)
    return app


app = create_app()
