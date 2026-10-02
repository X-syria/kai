"""Timing and failure accounting for outbound provider calls.

Every outbound provider call in KAI goes through one of the client modules
(Google OAuth / Calendar / Gmail / Contacts, or the planner model). This helper
is the single place those calls are measured, so the metric set cannot drift
away from the set of real call sites and the label values stay a closed set of
``(provider, operation)`` pairs.

The context manager records what happened and re-raises the original exception
untouched. It never swallows an error and never changes a return value, so
wrapping a call site cannot alter what that call does.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager

from kai.observability import metrics
from kai.observability.tracing import span


@contextmanager
def provider_call(*, provider: str, operation: str) -> Iterator[None]:
    """Record one outbound provider call: a span, a latency sample, a failure count.

    Usage::

        with provider_call(provider=metrics.PROVIDER_GOOGLE, operation="list_events"):
            resp = await transport.get(url)

    On success the duration goes into ``kai_provider_latency_seconds``. On
    failure it goes into ``kai_provider_errors_total`` and the exception is
    re-raised unchanged. The failing duration is not added to the latency
    histogram, which must describe healthy calls only; the span still carries
    it, so "how long did Google take when it broke" is answerable from the log
    channel.
    """
    started = time.perf_counter()
    try:
        with span(f"provider.{operation}"):
            yield
    except BaseException as exc:
        metrics.record_provider_call(
            provider=provider,
            operation=operation,
            duration_seconds=time.perf_counter() - started,
            error=type(exc).__name__,
        )
        raise
    metrics.record_provider_call(
        provider=provider,
        operation=operation,
        duration_seconds=time.perf_counter() - started,
        error=None,
    )