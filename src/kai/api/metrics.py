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

from datetime import UTC, datetime

from fastapi import APIRouter, Response

from kai.observability import metrics


async def _refresh_alert_queue() -> None:
    """Fold the durable outbox's depth into the registry before rendering.

    The outbox is written by the application but drained by a separate one-shot
    dispatcher, so its depth is not a counter this process can increment. It is
    read here instead, on the scrape, because a database read per scrape is
    bounded work that cannot fall behind the way a background refresh loop can.

    A failure is recorded as a database error and swallowed: the metrics route
    must still answer, and a monitoring system being unable to read the queue is
    exactly the kind of thing ``kai_db_errors_total`` exists to surface.
    """
    from kai.alerts import service as alert_service
    from kai.db.engine import async_session_factory
    from kai.logging import get_logger

    try:
        async with async_session_factory() as session:
            snapshot = await alert_service.snapshot_queue(
                session, now=datetime.now(UTC)
            )
    except Exception as exc:  # noqa: BLE001 - a scrape must never 500 on this
        metrics.record_db_error("execute")
        get_logger("kai.api.metrics").warning(
            "alert_queue_snapshot_failed", error=type(exc).__name__
        )
        return
    metrics.set_alert_queue(
        pending=snapshot.pending,
        retryable=snapshot.retryable,
        in_flight=snapshot.in_flight,
        permanently_failed=snapshot.permanently_failed,
        oldest_pending_age_seconds=snapshot.oldest_pending_age_seconds,
    )
    metrics.set_alert_dispatcher_freshness(
        last_success_age_seconds=snapshot.dispatcher_last_success_age_seconds,
        last_start_age_seconds=snapshot.dispatcher_last_start_age_seconds,
    )


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
        that can fail or go stale. The outbox depth is pulled from the database
        first, because it is maintained by a different process.
        """
        await _refresh_alert_queue()
        return Response(
            content=metrics.render_latest(),
            media_type=metrics.content_type(),
        )

    return router