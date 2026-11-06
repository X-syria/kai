import contextlib
from collections.abc import AsyncIterator

from fastapi import FastAPI
from starlette.middleware.trustedhost import TrustedHostMiddleware

from kai.api.metrics import build_metrics_router
from kai.api.router import api_router
from kai.config import get_settings
from kai.db.engine import engine
from kai.demo import router as demo_router
from kai.logging import configure_logging, get_logger
from kai.middleware import RequestContextMiddleware
from kai.observability import metrics
from kai.observability.context import RouteTable, build_route_table
from kai.observability.db import install_db_instrumentation
from kai.security.cors import build_cors_middleware
from kai.security.ratelimit import RateLimitMiddleware

_log = get_logger("kai.main")

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
        lifespan=_lifespan,
    )
    # Starlette builds the stack so that the LAST middleware added is the
    # OUTERMOST one. The order below is therefore the reverse of the call order
    # and is deliberate:
    #
    #   RequestContext -> CORS -> TrustedHost -> RateLimit -> router
    #
    # RequestContext has to be outermost, or it would sit *inside* the rate
    # limiter and never see the 429s, CORS preflights, or TrustedHost 400s that
    # the middlewares produce without reaching a route. Those rejections are
    # precisely the events an operator needs counted, so the observability
    # envelope wraps everything.
    #
    # The relative order of the three security middlewares is unchanged from the
    # security round - CORS outermost, then the Host guard, then the limiter.
    # Only RequestContext moved, and it moved outwards past all three. Swapping
    # CORS and TrustedHost while making the envelope outermost would have been
    # an unrequested change to the Host-check/response-precedence behaviour.
    #
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
    _mount_demo_ui(app)
    _mount_metrics(app)
    # Installed last of all, so it is the outermost layer, and built after every
    # router is mounted so the route table it labels against is complete.
    app.add_middleware(RequestContextMiddleware, route_table=_route_table(app))
    # One hook on the shared engine covers every database failure in the
    # service, including those in the maintenance command.
    install_db_instrumentation(engine)
    return app


def _route_table(app: FastAPI) -> RouteTable:
    """Collect every route template the app serves, for bounded route labels.

    Sourced from the generated OpenAPI document, which is the public and exact
    description of the mounted path table - including the prefixes that
    FastAPI's lazily-included routers do not put on ``scope["route"].path``.

    Routes absent from the schema are added explicitly, so an endpoint hidden
    from documentation still gets its own template instead of collapsing into
    ``unmatched`` alongside every 404.
    """
    templates: set[str] = set(app.openapi().get("paths", {}))
    # ``BaseRoute`` has no ``path``; the attribute is what Starlette adds on
    # ``Route`` and ``Mount``, so it is read dynamically rather than asserted.
    declared = (getattr(route, "path", None) for route in app.routes)
    templates.update(path for path in declared if isinstance(path, str) and path)
    if get_settings().metrics_enabled:
        templates.add(get_settings().metrics_path)
    return build_route_table(templates)


def _mount_demo_ui(app: FastAPI) -> None:
    """Serve the approval demo page only when the operator opted in.

    The page is unauthenticated HTML that drives the ordinary authenticated API
    from a browser. Mounting it by default would put an evaluation surface in
    every deployment, so the router is registered only when
    ``DEMO_UI_ENABLED`` is true. When it is off the path does not exist at all
    (404), which is stronger than returning an error from a mounted route: it
    keeps ``/demo`` out of the route table entirely.
    """
    settings = get_settings()
    if not settings.demo_ui_enabled:
        _log.info("demo_ui_disabled", reason="DEMO_UI_ENABLED is false")
        return
    app.include_router(demo_router)
    _log.info("demo_ui_enabled", path="/demo")


def _mount_metrics(app: FastAPI) -> None:
    """Serve metrics only when the operator asked for them.

    The route is unauthenticated (Prometheus cannot present a KAI token), so
    enabling it is an exposure decision and stays opt-in. When it is on it must
    be reachable from a private network or a scrape proxy only.
    """
    settings = get_settings()
    if not settings.metrics_enabled:
        _log.info("metrics_export_disabled", reason="METRICS_ENABLED is false")
        return
    app.include_router(build_metrics_router(settings.metrics_path))
    _log.info("metrics_export_enabled", path=settings.metrics_path)


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Record process lifecycle so a restart is visible in telemetry."""
    settings = get_settings()
    _log.info(
        "startup",
        environment=settings.environment,
        metrics_path=settings.metrics_path,
        metrics_enabled=settings.metrics_enabled,
        tracing_enabled=settings.tracing_enabled,
    )
    metrics.set_process_up(True)
    try:
        yield
    finally:
        # Set before logging so a scrape taken during shutdown already reports 0.
        metrics.set_process_up(False)
        _log.info("shutdown")
        with contextlib.suppress(Exception):
            await engine.dispose()


app = create_app()