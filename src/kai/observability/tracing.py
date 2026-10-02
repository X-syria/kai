"""W3C trace-context propagation and lightweight spans.

Scope, stated plainly: this is **correlation**, not a full distributed tracing
stack. KAI is one deployable process whose only outbound calls are to Google
and a planner model. There is no second service to correlate with, and shipping
an OpenTelemetry SDK plus a collector would add a dependency tree and an
operational component that nothing here consumes.

What it does instead:

* **Propagation.** Inbound ``traceparent`` (W3C Trace Context) is honoured when
  it is well formed, so a trace started by an upstream proxy or collector stays
  one trace across the process boundary. A malformed or absent header gets a
  fresh trace; a client cannot poison the trace space with arbitrary ids.
* **Request correlation.** ``request_id`` remains the operator-facing handle and
  is returned as ``X-Request-ID``; ``trace_id``/``span_id`` are the
  tracing-facing handles. Both ride on the existing structured-log channel.
* **Spans.** ``span()`` records a named, timed, parented unit of work for the
  operations that matter (request, auth, task, provider, DB) and emits one
  structured ``trace.span`` event.
* **Error status.** A span that raises records ``status="error"`` plus the
  exception *type* (never its message, which can carry user data) and re-raises.

Exporter: spans are exported through the JSON log stream the service already
writes to stdout, which is the telemetry channel every KAI deployment already
ships. There is no OTLP push and no collector dependency. An OTLP exporter is a
real remaining gap, recorded in the round report, and it is the first thing to
add once a collector exists to receive it.

Telemetry is never allowed to become an application outage: every emission path
here is exception-safe.
"""

from __future__ import annotations

import contextlib
import re
import secrets
import time
from collections.abc import Iterator
from contextlib import suppress
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Final

import structlog

from kai.logging import get_logger

_log = get_logger("kai.observability.tracing")

#: A W3C trace id is 16 random bytes rendered as 32 lowercase hex characters;
#: a span id is 8 bytes / 16 hex characters. Both must be non-zero.
_TRACE_ID_RE: Final = re.compile(r"^[0-9a-f]{32}$")
_SPAN_ID_RE: Final = re.compile(r"^[0-9a-f]{16}$")

#: Only version ``00`` is defined by the specification. An unknown future
#: version is accepted only when it still parses to the fixed field widths, per
#: the spec's forward-compatibility rule.
_SUPPORTED_VERSIONS: Final[frozenset[str]] = frozenset({"00"})

_TRACE_ID_LENGTH: Final[int] = 32
_SPAN_ID_LENGTH: Final[int] = 16
_ZERO_TRACE_ID: Final[str] = "0" * _TRACE_ID_LENGTH
_ZERO_SPAN_ID: Final[str] = "0" * _SPAN_ID_LENGTH


@dataclass(frozen=True)
class SpanContext:
    """The identifiers that tie a span to its trace and to its parent."""

    trace_id: str
    span_id: str
    parent_span_id: str | None = None


#: The currently active span, or ``None`` outside any span. A ContextVar rather
#: than a thread-local because the application is async: concurrent requests
#: each need their own trace, and a contextvar is per-task.
_current_span: ContextVar[SpanContext | None] = ContextVar("kai_current_span", default=None)


def _random_hex(length: int) -> str:
    return secrets.token_hex(length // 2)


def new_trace_id() -> str:
    return _random_hex(_TRACE_ID_LENGTH)


def new_span_id() -> str:
    return _random_hex(_SPAN_ID_LENGTH)


def parse_traceparent(header: str | None) -> SpanContext | None:
    """Return the context a valid ``traceparent`` names, or ``None``.

    The header is untrusted input. Anything that is not exactly the specified
    ``<version>-<trace-id>-<parent-id>-<flags>`` shape, or that carries an
    all-zero identifier (which the spec defines as invalid), yields ``None`` so
    the caller mints a fresh trace instead of adopting a client's.
    """
    if not header:
        return None
    parts = header.strip().split("-")
    if len(parts) != 4:
        return None
    version, trace_id, parent_id, flags = parts
    if len(version) != 2 or any(c not in "0123456789abcdef" for c in version.lower()):
        return None
    normalized_version = version.lower()
    if normalized_version not in _SUPPORTED_VERSIONS:
        # Forward compatibility: an unknown future version is still parsed,
        # because it keeps the same four fields at the same widths. The spec
        # also lets such a version append further fields; this parser does not,
        # and it refuses the header rather than guessing where the trace id
        # ends. Refusing is always safe - the caller mints a fresh trace.
        _log.debug("traceparent_future_version", version=normalized_version)
    if len(trace_id) != _TRACE_ID_LENGTH or not _TRACE_ID_RE.match(trace_id):
        return None
    if trace_id == _ZERO_TRACE_ID:
        return None
    if len(parent_id) != _SPAN_ID_LENGTH or not _SPAN_ID_RE.match(parent_id):
        return None
    if parent_id == _ZERO_SPAN_ID:
        return None
    if len(flags) != 2 or any(c not in "0123456789abcdefABCDEF" for c in flags):
        return None
    return SpanContext(trace_id=trace_id, span_id=parent_id, parent_span_id=None)


def format_traceparent(context: SpanContext) -> str:
    """Render ``context`` as a ``traceparent`` header value."""
    return f"00-{context.trace_id}-{context.span_id}-01"


def current_span() -> SpanContext | None:
    return _current_span.get()


def start_span(name: str, *, context: SpanContext | None = None) -> SpanContext:
    """Open a child of ``context`` (or the active span) and make it current.

    Returns the new span's context. The caller must ``end_span`` it; the
    :func:`span` context manager is the normal way to do that.
    """
    parent = context if context is not None else _current_span.get()
    child = SpanContext(
        trace_id=parent.trace_id if parent is not None else new_trace_id(),
        span_id=new_span_id(),
        parent_span_id=parent.span_id if parent is not None else None,
    )
    return child


def end_span(
    span: SpanContext,
    *,
    name: str,
    duration_seconds: float,
    error: str | None,
    attributes: dict[str, Any] | None = None,
) -> None:
    """Emit one finished span as a structured event.

    ``attributes`` are low-cardinality facts about the span (method, route
    template, status class). The caller may pass a dict it fills in *after* the
    work completes, which is how the request span ends up carrying the route and
    status that only exist once the response is known.

    Exception-safe: a telemetry defect must not become an application failure.
    """
    try:
        event: dict[str, Any] = {
            "span": name,
            "trace_id": span.trace_id,
            "span_id": span.span_id,
            "parent_span_id": span.parent_span_id,
            "duration_seconds": round(duration_seconds, 6),
        }
        if attributes:
            event.update(attributes)
        if error is None:
            _log.info("trace.span", status="ok", **event)
        else:
            _log.warning("trace.span", status="error", error_type=error, **event)
    except Exception:  # noqa: BLE001 - telemetry must never propagate
        pass


@contextlib.contextmanager
def span(
    name: str,
    *,
    context: SpanContext | None = None,
    attributes: dict[str, Any] | None = None,
) -> Iterator[SpanContext]:
    """Time a block of work as one span, recording an error status on raise.

    Usage::

        with span("auth.verify") as ctx:
            ...

    The block's exception propagates after the span is closed, so a failure is
    recorded *and* still handled by the caller's own error path.

    While the block runs, this span is the *current* one, so any span opened
    deeper - inside a route handler, an auth dependency, a provider call -
    automatically becomes its child. That is what makes request -> auth ->
    provider a single trace rather than three unrelated ones.
    """
    current = start_span(name, context=context)
    token = _current_span.set(current)
    started = time.perf_counter()
    error: str | None = None
    try:
        yield current
    except BaseException as exc:
        # Type only, never str(): an exception message can carry user data
        # (an email, a token fragment) and this goes to a log line.
        error = type(exc).__name__
        raise
    finally:
        _current_span.reset(token)
        # end_span already swallows its own failures; this guard is what makes
        # the contract hold for *any* emitter, so a future exporter cannot turn
        # a log problem into an application outage.
        with suppress(Exception):
            end_span(
                current,
                name=name,
                duration_seconds=time.perf_counter() - started,
                error=error,
                attributes=attributes,
            )


def bind_current_span(logger: structlog.stdlib.BoundLogger) -> structlog.stdlib.BoundLogger:
    """Return ``logger`` with the active trace identifiers bound.

    Bind once at the start of a request and every later log line carries the
    trace without each call site having to pass it.
    """
    current = _current_span.get()
    if current is None:
        return logger
    return logger.bind(
        trace_id=current.trace_id,
        span_id=current.span_id,
        parent_span_id=current.parent_span_id,
    )