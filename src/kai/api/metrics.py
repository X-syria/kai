"""The metrics export route.

One route, no push, no collector. A monitoring system scrapes it; KAI renders
its own registry and returns it. That shape is deliberate: it means a monitoring
backend being unreachable, slow, or down cannot affect request handling, and
there is no exporter process to keep alive.

Exposure is controlled by ``METRICS_ENABLED`` (default off). When it is on, the
route is unauthenticated by design - Prometheus cannot present a KAI token - so
it must be reachable only from a private network or a scrape proxy. Nothing it
returns contains a user identifier, a token, a request body, or a route's
concrete path; the ``route`` label is the route *template*.
"""

from __future__ import annotations

from fastapi import APIRouter, Response

from kai.observability import metrics


def build_metrics_router(path: str) -> APIRouter:
    """Build a router serving exactly ``path``.

    The path is taken from configuration rather than hardcoded, so an operator
    can move the endpoint without this module owning a second definition of it.
    """
    router = APIRouter(tags=["observability"])

    @router.get(
        path,
        include_in_schema=False,
        response_class=Response,
        responses={
            200: {
                "content": {metrics.content_type(): {}},
                "description": "Prometheus text exposition of KAI's metrics.",
            }
        },
    )
    async def metrics_endpoint() -> Response:
        """Render the current registry in the Prometheus text format.

        Rendered per request rather than cached, so a scrape always reflects
        the counters as of that moment and there is no background refresh loop
        that can fail or go stale.
        """
        return Response(
            content=metrics.render_latest(),
            media_type=metrics.content_type(),
        )

    return router