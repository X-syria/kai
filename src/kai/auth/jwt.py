from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import jwt

from kai.config import SUPPORTED_JWT_ALGORITHMS, get_settings

TOKEN_TYPE_ACCESS = "access"


class InvalidTokenError(ValueError):
    """Raised when a supplied token is malformed, expired, or unparseable."""


@dataclass(frozen=True)
class TokenClaims:
    """Verified identity carried by an access token (MVP scope).

    ``user_id`` + ``tenant_id`` together form the *only* principal a request
    may ever act as; every downstream query is scoped by ``tenant_id``.

    ``jti`` is the token's own id. It exists so a token can be revoked before
    its natural expiry; without it, a leaked bearer token is unstoppable until
    the clock runs out.
    """

    user_id: UUID
    tenant_id: UUID
    token_type: str
    exp: datetime
    jti: str


def create_access_token(*, user_id: UUID, tenant_id: UUID) -> str:
    settings = get_settings()
    if settings.jwt_algorithm not in SUPPORTED_JWT_ALGORITHMS:
        raise ValueError(f"unsupported JWT algorithm: {settings.jwt_algorithm!r}")
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "tenant_id": str(tenant_id),
        "type": TOKEN_TYPE_ACCESS,
        "jti": str(uuid4()),
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": int(now.timestamp()),
        "exp": int(
            (now + timedelta(minutes=settings.jwt_access_token_minutes)).timestamp()
        ),
    }
    return jwt.encode(
        payload, settings.jwt_secret, algorithm=settings.jwt_algorithm
    )


def decode_access_token(token: str) -> TokenClaims:
    """Validate ``token`` and return its claims.

    Raises :class:`InvalidTokenError` for any malformed, expired, or
    wrong-typed token. This is the single trusted decode path in the app.

    Verification is not optional here: ``exp`` must be present and unexpired,
    ``iss`` and ``aud`` must match this service, and the signature must verify
    under an algorithm from the closed allowlist. A token missing any required
    claim is rejected rather than defaulted, so a token that omits ``exp`` can
    never be treated as non-expiring.
    """
    settings = get_settings()
    if settings.jwt_algorithm not in SUPPORTED_JWT_ALGORITHMS:
        raise InvalidTokenError("token is invalid or expired")
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
            leeway=settings.jwt_leeway_seconds,
            options={"require": ["exp", "iat", "sub", "jti", "iss", "aud"]},
        )
    except jwt.PyJWTError as exc:
        raise InvalidTokenError("token is invalid or expired") from exc

    try:
        user_id = UUID(payload["sub"])
        tenant_id = UUID(payload["tenant_id"])
        token_type = str(payload["type"])
        exp = datetime.fromtimestamp(int(payload["exp"]), tz=UTC)
        jti = str(payload["jti"])
    except (KeyError, ValueError, TypeError) as exc:
        raise InvalidTokenError("token is missing required claims") from exc

    if token_type != TOKEN_TYPE_ACCESS:
        raise InvalidTokenError("token is not an access token")

    return TokenClaims(
        user_id=user_id,
        tenant_id=tenant_id,
        token_type=token_type,
        exp=exp,
        jti=jti,
    )
