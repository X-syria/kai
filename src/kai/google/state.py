"""Stateless, authenticated CSRF protection for the Google OAuth flow.

The ``state`` value is an HMAC-signed payload binding a callback to the exact
``(user_id, tenant_id)`` that started it. There is no server-side session
store: the signature and embedded expiry make a forged or replayed-from-elsewhere
state fail validation before any Google token is exchanged.
"""

import base64
import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from kai.config import get_settings

STATE_TTL_SECONDS = 600


class InvalidStateError(ValueError):
    """Raised when a supplied ``state`` is malformed, forged, or expired."""


@dataclass(frozen=True)
class StateClaims:
    """Identity bound into a signed ``state`` payload."""

    user_id: UUID
    tenant_id: UUID


def _sign(payload: str) -> str:
    secret = get_settings().jwt_secret.encode("utf-8")
    digest = hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii")


def _encode_body(payload: str) -> str:
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_body(body: str) -> str:
    padded = body + "=" * (-len(body) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8")


def create_state(*, user_id: UUID, tenant_id: UUID, expires_at: datetime | None = None) -> str:
    """Create a signed state token bound to ``user_id`` and ``tenant_id``."""
    exp = expires_at or (datetime.now(UTC) + timedelta(seconds=STATE_TTL_SECONDS))
    payload = f"{user_id}:{tenant_id}:{int(exp.timestamp())}"
    body = _encode_body(payload)
    return f"{body}.{_sign(payload)}"


def validate_state(state: str, *, now: datetime | None = None) -> StateClaims:
    """Validate a signed state token and return its claims.

    Raises :class:`InvalidStateError` for malformed, tampered, or expired
    tokens. ``now`` exists only for deterministic expiry tests.
    """
    try:
        body, signature = state.split(".", 1)
        payload = _decode_body(body)
    except (ValueError, TypeError, UnicodeDecodeError) as exc:
        raise InvalidStateError("oauth state is malformed") from exc

    if not hmac.compare_digest(signature, _sign(payload)):
        raise InvalidStateError("oauth state signature is invalid")

    try:
        user_id_str, tenant_id_str, exp_str = payload.split(":")
        user_id = UUID(user_id_str)
        tenant_id = UUID(tenant_id_str)
        exp = datetime.fromtimestamp(int(exp_str), tz=UTC)
    except (ValueError, TypeError) as exc:
        raise InvalidStateError("oauth state payload is malformed") from exc

    check_time = now if now is not None else datetime.now(UTC)
    if exp <= check_time:
        raise InvalidStateError("oauth state has expired")

    return StateClaims(user_id=user_id, tenant_id=tenant_id)