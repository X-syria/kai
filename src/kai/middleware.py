"""Per-request observability: correlation id, root span, HTTP metrics, request log.

This middleware is the outermost thing in the stack, which is the whole point.
It has to sit outside CORS, TrustedHost and the rate limiter so that a response
produced *by* those middlewares - a 429, a 400 for a bad Host - is measured and
logged exactly like a response produced by a route handler. See ``main.create_app``
for why it is therefore installed last.

What it guarantees:

* every request gets a ``request_id`` that appears in every log line for that
  request and in the ``X-Request-ID`` response header;
* an inbound id is honoured only when it is safe to do so
  (:mod:`kai.observability.context`);
* every request produces an HTTP counter, a latency sample, and a 5xx counter;
* every request produces one structured ``http_request`` log event, one
  ``http_request_failed`` event when it raises, and one ``http.request`` span
  that is the *parent* of every span opened downstream (auth, task, provider);
* nothing user-identifying, credential-shaped, or free-text is logged.

No secret, raw JWT, Authorization header, OAuth secret, API key, database
credential, or message body is ever bound into the log context here. The log
context carries exactly: request_id, method, route template, status, duration.
"""

from __future__ import annotations

import contextlib
import random
import time
from typing import Any

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from kai.config import get_settings
from kai.logging import get_logger
from kai.observability import metrics
from kai.observability.context import RouteTable, request_id_from_headers, route_label
from kai.observability.tracing import SpanContext, parse_traceparent, span

_log = get_logger("kai.http")


def _should_trace() -> bool:
    """Decide whether this request is traced, honouring the sample ratio."""
    ratio = get_settings().tracing_sample_ratio
    if ratio >= 1.0:
        return True
    if ratio <= 0.0:
        return False
    return random.random() < ratio


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Bind correlation + trace context, then measure the request it wrapped."""

    def __init__(self, app, *, route_table: RouteTable) -> None:
        super().__init__(app)
        self.route_table = route_table

    async def dispatch(self, request: Request, call_next) -> Response:
        settings = get_settings()
        request_id = request_id_from_headers(request.headers)

        # Clear first: this process serves many requests, and a stale id from an
        # earlier request on the same worker must never be inherited.
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        # The route is resolved by the router *during* call_next, so it is only
        # knowable afterwards. Reading scope["route"] up front would label every
        # request "unmatched" and quietly collapse the whole route dimension.
        # These facts are collected in a dict the span reads when it closes, so
        # the span still ends up carrying the route and the status.
        span_attributes: dict[str, Any] = {"method": request.method}
        span_context: SpanContext | None = None

        # ExitStack, not a bare ``with span(...)``: the span has to stay open
        # across call_next and close only once the outcome is known. Entering it
        # also makes it the *current* span for everything downstream, which is
        # what links auth/provider/task spans to this request's trace.
        with contextlib.ExitStack() as stack:
            if settings.tracing_enabled and _should_trace():
                span_context = stack.enter_context(
                    span(
                        "http.request",
                        context=_inbound_context(request, settings),
                        attributes=span_attributes,
                    )
                )

            started = time.perf_counter()
            try:
                response = await call_next(request)
            except Exception as exc:
                duration = time.perf_counter() - started
                route = route_label(request.scope, self.route_table)
                span_attributes.update(route=route, status_code=500)
                # The exception is about to be turned into a 500 by Starlette's
                # server-error handler, so record it as one. Only the type is
                # logged: a message can carry user data.
                _log.exception(
                    "http_request_failed",
                    method=request.method,
                    route=route,
                    status_code=500,
                    duration_seconds=round(duration, 6),
                    error_type=type(exc).__name__,
                    trace_id=span_context.trace_id if span_context else None,
                )
                _record(method=request.method, route=route, status_code=500, duration=duration)
                raise

            duration = time.perf_counter() - started
            status_code = response.status_code
            route = route_label(request.scope, self.route_table)
            span_attributes.update(route=route, status_code=status_code)
            response.headers["X-Request-ID"] = request_id
            if span_context is not None:
                response.headers["X-Trace-ID"] = span_context.trace_id

            _record(
                method=request.method, route=route, status_code=status_code, duration=duration
            )
            _log.info(
                "http_request",
                method=request.method,
                route=route,
                status_code=status_code,
                duration_seconds=round(duration, 6),
                trace_id=span_context.trace_id if span_context else None,
            )
        return response


def _record(*, method: str, route: str, status_code: int, duration: float) -> None:
    """Record the HTTP metrics, excluding the scrape endpoint itself.

    A Prometheus scrape every 15 seconds is ~5,760 requests a day of pure
    instrumentation. Counting them would let the scrape rate mask the request
    rate an operator is actually alerting on, so the exporter is excluded.
    """
    if route == get_settings().metrics_path:
        return
    metrics.record_http_request(
        method=method, route=route, status_code=status_code, duration_seconds=duration
    )


def _inbound_context(request: Request, settings) -> SpanContext | None:
    """The span this request should be a child of, or ``None`` to start fresh.

    An inbound ``traceparent`` is adopted only when the setting says a trusted
    upstream starts the trace *and* the header is well formed. A client-supplied
    header on a service that does not opt in is ignored entirely, so an
    unauthenticated caller cannot choose the trace id its failure is filed under.
    """
    if not settings.tracing_accept_inbound:
        return None
    return parse_traceparent(request.headers.get("traceparent"))


__all__ = ["RequestContextMiddleware"]