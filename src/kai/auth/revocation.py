"""Revocation of access tokens that are still inside their expiry window.

A signed token stays cryptographically valid until ``exp``. Expiry alone is
therefore not a revocation mechanism: a token stolen today is usable for the
rest of its window. This module is the compensating control — a token id that
has been recorded here stops authenticating requests immediately.

The store is a table keyed by ``jti`` rather than a cache, so revocation
survives a restart and is shared by every replica, which is the same property
that makes it correct in a multi-instance deployment.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from kai.auth.jwt import TokenClaims
from kai.db.models import RevokedToken


async def is_token_revoked(session: AsyncSession, *, jti: str) -> bool:
    """Return whether ``jti`` has been revoked.

    A miss is a normal, fast path: most requests carry a live token, so the
    common case is one indexed primary-key lookup returning nothing.
    """
    result = await session.execute(
        select(RevokedToken.jti).where(RevokedToken.jti == jti)
    )
    return result.scalar_one_or_none() is not None


async def revoke_token(session: AsyncSession, *, claims: TokenClaims) -> None:
    """Record ``claims.jti`` as revoked until the token would expire anyway.

    Idempotent: signing out twice, or signing out a token another request
    already revoked, is a no-op rather than an error. That matters because
    logout is reachable concurrently by retries and by two devices at once.
    """
    session.add(
        RevokedToken(
            jti=claims.jti,
            user_id=claims.user_id,
            tenant_id=claims.tenant_id,
            expires_at=claims.exp,
        )
    )
    try:
        await session.commit()
    except IntegrityError:
        # The id was already revoked. The row we want is the one that is
        # already there, so there is nothing left to do.
        await session.rollback()


async def purge_expired_revocations(session: AsyncSession) -> int:
    """Delete revocations whose token has expired anyway, returning the count.

    Safe to run at any time: a record is only removed once its token's own
    ``exp`` has passed, which means the token is already rejected by the
    signature/expiry check and the record can no longer change any outcome.
    """
    result = await session.execute(
        delete(RevokedToken).where(RevokedToken.expires_at <= datetime.now(UTC))
    )
    await session.commit()
    # ``rowcount`` is only present on the DBAPI-returning result; a session
    # execute that hit the identity map instead of a cursor has none, which is
    # the same as "nothing was deleted".
    rowcount = getattr(result, "rowcount", 0)
    return int(rowcount or 0)
