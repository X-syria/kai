from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from kai.auth.jwt import InvalidTokenError, decode_access_token
from kai.auth.revocation import is_token_revoked
from kai.db.engine import get_session
from kai.db.models import User
from kai.db.scoping import get_user_for_tenant

_bearer = HTTPBearer(auto_error=False)

_UNAUTHENTICATED: dict[str, str] = {
    "missing": "missing bearer token",
    "invalid": "token is invalid or expired",
    "revoked": "token is invalid or expired",
    "inactive": "token is invalid or expired",
}


def _reject(reason: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=_UNAUTHENTICATED[reason],
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: AsyncSession = Depends(get_session),
) -> User:
    """Authenticate a request to exactly one principal.

    Four independent reasons a caller may fail, and they all resolve to the
    same opaque 401 body on purpose. A client cannot use the error text to
    learn whether a token was forged, expired, revoked, or simply belonged to
    an account that no longer exists — that distinction is useful to an attacker
    probing for valid material and is not useful to a legitimate client.

    The order below is deliberate: signature and expiry are checked first
    because they are the cheapest and they do not touch the database. Revocation
    is checked next, and only then is the user row loaded, so an unauthenticated
    request never becomes a database query.
    """
    if credentials is None:
        raise _reject("missing")
    try:
        claims = decode_access_token(credentials.credentials)
    except InvalidTokenError as exc:
        raise _reject("invalid") from exc

    # Revocation is checked before anything is done on the caller's behalf, and
    # before the user row is trusted for authorization decisions below.
    if await is_token_revoked(session, jti=claims.jti):
        raise _reject("revoked")

    user = await get_user_for_tenant(
        session,
        tenant_id=claims.tenant_id,
        user_id=claims.user_id,
    )
    # A token that names a user which no longer exists, or that has been
    # deactivated, is as unusable as one that was never signed. Previously
    # ``is_active`` was only ever reported by /auth/me and never enforced, so
    # deactivating an account did not actually stop anyone already holding a
    # live token.
    if user is None or not user.is_active:
        raise _reject("inactive")
    return user
