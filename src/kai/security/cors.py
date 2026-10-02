from collections.abc import Sequence
from typing import Any

from starlette.middleware.cors import CORSMiddleware

CORSConfig = tuple[type[CORSMiddleware], dict[str, Any]]


def build_cors_middleware(
    allowed_origins: Sequence[str],
) -> CORSConfig | None:
    """Describe a CORS middleware for ``allowed_origins``, or ``None`` to skip it.

    Returned as a ``(class, kwargs)`` pair rather than a constructed instance
    because ``add_middleware`` needs the class with the app supplied separately;
    passing an already-built instance is the thing that silently drops the
    ``app`` argument.

    ``None`` when the allowlist is empty is deliberate. Installing Starlette's
    middleware with an empty list and ``allow_credentials=True`` looks
    configured but rejects every cross-origin call, which reads in production as
    a mysteriously broken frontend. Not installing it is the same effective
    policy with an honest code path.

    ``*`` is refused outright. It is incompatible with credentialed requests per
    the CORS spec, and because Starlette mirrors ``Access-Control-Allow-Origin:
    *`` back together with ``Allow-Credentials: true``, permitting it would hand
    every origin on the internet a credentialed cross-origin channel to this API.
    """
    if not allowed_origins:
        return None
    if "*" in allowed_origins:
        raise ValueError(
            "cors_allowed_origins must list explicit origins; '*' is not allowed"
        )
    return (
        CORSMiddleware,
        {
            "allow_origins": list(allowed_origins),
            "allow_credentials": True,
            "allow_methods": ["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
            "allow_headers": ["Authorization", "Content-Type"],
            "max_age": 600,
        },
    )
