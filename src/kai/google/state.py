"""Stateless, authenticated CSRF protection for the Google OAuth flow.

The ``state`` value is an HMAC-signed payload binding a callback to the exact
``(user_id, tenant_id)`` that started it *and* to the browser that started it.
Those are two different guarantees and they fail differently:

* the signature proves the payload is intact and was minted by this service;
* the embedded ``binding_hash`` proves the browser presenting the callback is
  the one that called ``/connect``.

The second one cannot live in ``state`` alone. ``state`` travels through
Google's servers and the user's address bar, so anyone who can read it can
replay it; by itself it is a bearer token for "bind this Google account".
``/connect`` therefore also sets an ``HttpOnly`` cookie holding a fresh random
secret and stores only its SHA-256 digest inside the signed payload. The
``/callback`` request must present both: a state whose digest matches the
cookie's secret. A link forwarded to another browser carries the state but not
the cookie, so the exchange never happens for it.

There is no server-side session store: the signature, the digest comparison,
and the embedded expiry make a forged, replayed-from-elsewhere, or stale state
fail validation before any Google token is exchanged.
"""

import base64
import hashlib
import hmac
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from kai.config import get_settings

STATE_TTL_SECONDS = 600

#: SHA-256 hex digest. Lowercase only, so the payload has exactly one spelling
#: for a given digest and the length is fixed at 64 characters with no ``:``.
_BINDING_HASH_RE = re.compile(r"[0-9a-f]{64}")


class InvalidStateError(ValueError):
    """Raised when a supplied ``state`` is malformed, forged, or expired."""


@dataclass(frozen=True)
class StateClaims:
    """Identity and browser secret digest bound into a signed ``state``."""

    user_id: UUID
    tenant_id: UUID
    scopes: tuple[str, ...]
    binding_hash: str


def _sign(payload: str) -> str:
    secret = get_settings().jwt_secret.encode("utf-8")
    digest = hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii")


def _encode_body(payload: str) -> str:
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_body(body: str) -> str:
    padded = body + "=" * (-len(body) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8")


def hash_binding(secret: str) -> str:
    """Return the digest of a browser-binding secret, for embedding in ``state``.

    Only the digest ever leaves the process: the raw secret lives in an
    ``HttpOnly`` cookie and must not appear in a URL, a log, or the signed
    payload itself.
    """
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def verify_binding(claims: StateClaims, secret: str | None) -> bool:
    """Return whether ``secret`` is the browser secret ``claims`` was minted for.

    An absent or empty secret never matches: a browser that presents no cookie
    cannot prove it started the transaction, so it must be refused rather than
    compared against an empty digest. The comparison is constant-time so it
    does not leak how much of a guessed secret was correct.
    """
    if not secret:
        return False
    return hmac.compare_digest(claims.binding_hash, hash_binding(secret))


def create_state(
    *,
    user_id: UUID,
    tenant_id: UUID,
    scopes: Sequence[str],
    binding_hash: str,
    expires_at: datetime | None = None,
) -> str:
    """Create a signed state token bound to identity, scopes, and a browser.

    ``binding_hash`` is the SHA-256 hex digest of a per-transaction secret that
    is delivered to the browser as a cookie, never in the URL. It is placed
    *before* the scope list so the payload splits into exactly five fields with
    ``split(":", 4)``: scopes contain ``://`` and must therefore be the last,
    unsplit field.
    """
    if _BINDING_HASH_RE.fullmatch(binding_hash) is None:
        raise ValueError("binding_hash must be a lowercase sha256 hex digest")
    exp = expires_at or (datetime.now(UTC) + timedelta(seconds=STATE_TTL_SECONDS))
    payload = (
        f"{user_id}:{tenant_id}:{int(exp.timestamp())}:{binding_hash}:"
        f"{' '.join(scopes)}"
    )
    body = _encode_body(payload)
    return f"{body}.{_sign(payload)}"


def validate_state(state: str, *, now: datetime | None = None) -> StateClaims:
    """Validate a signed state token and return its claims.

    Raises :class:`InvalidStateError` for malformed, tampered, or expired
    tokens. ``now`` exists only for deterministic expiry tests.

    Payloads with fewer than five fields - the three-field pre-scope form and
    the four-field identity+scopes form - are rejected: they carry no browser
    binding, so accepting them would reopen the replay this field closes. The
    check is fail-closed by construction.
    """
    try:
        body, signature = state.split(".", 1)
        payload = _decode_body(body)
    except (ValueError, TypeError, UnicodeDecodeError) as exc:
        raise InvalidStateError("oauth state is malformed") from exc

    if not hmac.compare_digest(signature, _sign(payload)):
        raise InvalidStateError("oauth state signature is invalid")

    try:
        (
            user_id_str,
            tenant_id_str,
            exp_str,
            binding_hash,
            scope_str,
        ) = payload.split(":", 4)
        user_id = UUID(user_id_str)
        tenant_id = UUID(tenant_id_str)
        exp = datetime.fromtimestamp(int(exp_str), tz=UTC)
        if _BINDING_HASH_RE.fullmatch(binding_hash) is None:
            raise ValueError("binding hash field is malformed")
        tokens = scope_str.split(" ")
        if not tokens or any(not token for token in tokens):
            raise ValueError("scope field is malformed")
    except (ValueError, TypeError) as exc:
        raise InvalidStateError("oauth state payload is malformed") from exc

    check_time = now if now is not None else datetime.now(UTC)
    if exp <= check_time:
        raise InvalidStateError("oauth state has expired")

    return StateClaims(
        user_id=user_id,
        tenant_id=tenant_id,
        scopes=tuple(tokens),
        binding_hash=binding_hash,
    )