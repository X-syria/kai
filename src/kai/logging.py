import logging
import sys

import structlog

from kai.config import get_settings


def configure_logging() -> None:
    settings = get_settings()
    logging.basicConfig(
        stream=sys.stdout,
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
        logger_factory=structlog.PrintLoggerFactory(sys.stdout),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = "kai") -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
