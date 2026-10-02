import logging
import sys
from typing import TextIO

import structlog

from kai.config import get_settings


def configure_logging(stream: TextIO | None = None) -> None:
    """Route every structured event to ``stream`` as one JSON object per line.

    ``cache_logger_on_first_use`` is off on purpose. structlog permanently
    replaces ``bind`` on a cached proxy the first time it is used, which pins
    that proxy to whatever stream and processors were configured at import
    time; with caching on, a later ``configure_logging()`` is silently ignored
    by every logger that already logged once. Keeping it off makes this
    function authoritative for its whole process lifetime, which is what the
    observability contract and the log-capture tests both depend on.
    """
    settings = get_settings()
    sink = stream if stream is not None else sys.stdout
    logging.basicConfig(
        stream=sink,
        level=settings.log_level,
        format="%(message)s",
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, settings.log_level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(sink),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str = "kai") -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
