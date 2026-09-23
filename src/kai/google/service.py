"""Persistence and refresh logic for tenant-scoped Google accounts."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from kai.db.models import GoogleAccount
from kai.google import client
from kai.google.client import GoogleOAuthError
from kai.google.crypto import decrypt_credentials, encrypt_credentials


class GoogleAccountConflictError(RuntimeError):
    """Raised when a Google identity cannot be bound to the requested user."""


async def bind_or_reconnect_google_account(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    user_id: UUID,
    google_sub: str,
    email: str,
    scopes: str,
    credentials_encrypted: str,
    token_expires_at: datetime,
) -> GoogleAccount:
    """Bind a Google identity to ``(tenant_id, user_id)`` or refresh an existing one.

    The DB ``(tenant_id, google_sub)`` unique constraint makes a duplicate
    identity a reconnect into the same row rather than a second row. If the
    identity already exists under the tenant but owned by a different user,
    binding is refused instead of silently re-assigning it.
    """
    result = await session.execute(
        select(GoogleAccount).where(
            GoogleAccount.tenant_id == tenant_id,
            GoogleAccount.google_sub == google_sub,
        )
    )
    existing = result.scalar_one_or_none()
    if existing is not None:
        if existing.user_id != user_id:
            raise GoogleAccountConflictError(
                "google account is already bound to another user in this tenant"
            )
        existing.email = email
        existing.scopes = scopes
        existing.credentials_encrypted = credentials_encrypted
        existing.token_expires_at = token_expires_at
        account = existing
    else:
        account = GoogleAccount(
            tenant_id=tenant_id,
            user_id=user_id,
            google_sub=google_sub,
            email=email,
            scopes=scopes,
            credentials_encrypted=credentials_encrypted,
            token_expires_at=token_expires_at,
        )
        session.add(account)
    try:
        await session.commit()
    except IntegrityError as exc:
        # Lost a concurrent bind race against the unique constraint.
        await session.rollback()
        winner = (
            await session.execute(
                select(GoogleAccount).where(
                    GoogleAccount.tenant_id == tenant_id,
                    GoogleAccount.google_sub == google_sub,
                )
            )
        ).scalar_one_or_none()
        if winner is None or winner.user_id != user_id:
            raise GoogleAccountConflictError("google account could not be bound") from exc
        account = winner
    await session.refresh(account)
    return account


async def refresh_google_account(
    session: AsyncSession,
    transport: httpx.AsyncClient,
    *,
    account_id: UUID,
    tenant_id: UUID,
) -> GoogleAccount:
    """Refresh a stored account's access token, scoped to ``tenant_id``.

    The new access token is re-encrypted before persisting; a Google rejection
    (revoked / invalid refresh token) surfaces as :class:`GoogleOAuthError`
    before any stored credential is touched.
    """
    result = await session.execute(
        select(GoogleAccount).where(
            GoogleAccount.id == account_id,
            GoogleAccount.tenant_id == tenant_id,
        )
    )
    account = result.scalar_one_or_none()
    if account is None:
        raise GoogleAccountConflictError("google account not found for tenant")

    credentials = decrypt_credentials(account.credentials_encrypted)
    refresh_token = credentials.get("refresh_token")
    if not refresh_token:
        raise GoogleOAuthError("google account has no stored refresh token")

    token = await client.refresh_access_token(transport, refresh_token=refresh_token)
    credentials["access_token"] = token.access_token
    if token.refresh_token:
        credentials["refresh_token"] = token.refresh_token

    account.credentials_encrypted = encrypt_credentials(credentials)
    account.token_expires_at = datetime.now(UTC) + timedelta(seconds=token.expires_in)
    await session.commit()
    await session.refresh(account)
    return account