"""Persistence and the access-token lifecycle for tenant-scoped Google accounts.

This module is the single credential authority for Google:

* every write of ``credentials_encrypted`` / ``token_expires_at`` goes through
  :func:`_store_credentials`;
* every merge of an OAuth token response into stored credentials goes through
  :func:`merge_credentials`;
* every "give me a usable access token" question is answered by
  :func:`usable_access_token`, which Calendar, Gmail and Contacts all call, so
  the three verticals cannot drift into three different refresh behaviours.

The lifecycle: ``token_expires_at`` is written from Google's own ``expires_in``
on every bind and every refresh, so it is the authoritative lifetime of the
stored access token. While that expiry is still outside :data:`REFRESH_SKEW`
the stored token is used as-is; once it is not, the stored ``refresh_token`` is
exchanged for a new access token under a row lock and the result is written
back. There is deliberately no 401 retry: a 401 on a freshly issued token means
consent was revoked (where a retry fails identically), and the mutating
endpoints are not safely replayable.
"""

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

#: A stored access token expiring within this window is exchanged for a new one
#: before it is handed to a provider. Without a skew a token judged "still
#: valid" can expire while the request carrying it is in flight, and the only
#: recovery would be the 401 retry this design does not perform.
REFRESH_SKEW = timedelta(seconds=60)


class GoogleAccountConflictError(RuntimeError):
    """Raised when a Google identity cannot be bound to the requested user."""


def merge_credentials(
    existing: dict | None, *, access_token: str, refresh_token: str | None
) -> dict:
    """Merge one OAuth token response into stored credentials.

    The new access token always wins. A stored refresh token is replaced only
    when Google actually issued one: Google omits ``refresh_token`` whenever a
    re-authorization does not change consent, and writing that omission back as
    ``None`` would destroy the account's only long-lived credential, making
    every later refresh impossible until the user connected the account again.
    """
    merged = dict(existing or {})
    merged["access_token"] = access_token
    if refresh_token:
        merged["refresh_token"] = refresh_token
    return merged


def _store_credentials(
    account: GoogleAccount, credentials: dict, *, expires_in: int, now: datetime
) -> None:
    """The one write path for a Google account's stored credentials."""
    account.credentials_encrypted = encrypt_credentials(credentials)
    account.token_expires_at = now + timedelta(seconds=expires_in)


def _stored_access_token(credentials: dict) -> str:
    token = credentials.get("access_token")
    if not isinstance(token, str) or not token:
        raise GoogleOAuthError("google account has no stored access token")
    return token


def _is_fresh(account: GoogleAccount, now: datetime) -> bool:
    """True when the stored access token can still be used at ``now``.

    ``token_expires_at`` is written from Google's own ``expires_in`` on every
    bind and every refresh, so it is the authoritative lifetime. A missing
    expiry certifies nothing and is therefore never fresh.
    """
    expires_at = account.token_expires_at
    return expires_at is not None and expires_at > now + REFRESH_SKEW


async def _lock_account(
    session: AsyncSession, account: GoogleAccount
) -> GoogleAccount:
    """Lock the account row and read it as it is right now.

    ``populate_existing`` because the caller already loaded this account in the
    same session: without it the lock would serialise refreshes while still
    letting the loser exchange a token against a credential image that no
    longer exists. Same primitive as ``tasks.service._load_task_for_update``.
    """
    result = await session.execute(
        select(GoogleAccount)
        .where(
            GoogleAccount.id == account.id,
            GoogleAccount.tenant_id == account.tenant_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    locked = result.scalar_one_or_none()
    if locked is None:
        raise GoogleAccountConflictError("google account not found for tenant")
    return locked


async def _exchange_refresh_token(
    session: AsyncSession,
    transport: httpx.AsyncClient,
    *,
    account: GoogleAccount,
    credentials: dict,
    now: datetime,
) -> dict:
    """Exchange the stored refresh token and stage the new credentials.

    A Google rejection (revoked / invalid refresh token) surfaces as
    :class:`GoogleOAuthError` before any stored credential is touched. The
    session is rolled back on those paths so a row lock taken by the caller is
    released immediately instead of being held until the request unwinds.
    """
    refresh_token = credentials.get("refresh_token")
    if not refresh_token:
        await session.rollback()
        raise GoogleOAuthError("google account has no stored refresh token")
    try:
        token = await client.refresh_access_token(
            transport, refresh_token=str(refresh_token)
        )
    except GoogleOAuthError:
        await session.rollback()
        raise
    merged = merge_credentials(
        credentials,
        access_token=token.access_token,
        refresh_token=token.refresh_token,
    )
    _store_credentials(account, merged, expires_in=token.expires_in, now=now)
    return merged


async def usable_access_token(
    session: AsyncSession,
    transport: httpx.AsyncClient,
    *,
    account: GoogleAccount,
    now: datetime | None = None,
) -> str:
    """Return a Google access token that can be sent to a provider right now.

    A fresh stored token is returned as-is. Otherwise the row is locked,
    re-read, and re-checked, so callers that all found the token expired
    serialise into a single exchange instead of a refresh storm: the first one
    refreshes and commits, and the ones waiting on the lock wake up on the new
    expiry and reuse the token it persisted.
    """
    current = now or datetime.now(UTC)
    credentials = decrypt_credentials(account.credentials_encrypted)
    if _is_fresh(account, current):
        return _stored_access_token(credentials)

    locked = await _lock_account(session, account)
    credentials = decrypt_credentials(locked.credentials_encrypted)
    if _is_fresh(locked, current):
        # Somebody else refreshed while this caller waited for the lock; the
        # commit releases it without writing anything.
        await session.commit()
        return _stored_access_token(credentials)

    merged = await _exchange_refresh_token(
        session, transport, account=locked, credentials=credentials, now=current
    )
    await session.commit()
    return _stored_access_token(merged)


async def bind_or_reconnect_google_account(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    user_id: UUID,
    google_sub: str,
    email: str,
    scopes: str,
    access_token: str,
    refresh_token: str | None,
    expires_in: int,
    now: datetime | None = None,
) -> GoogleAccount:
    """Bind a Google identity to ``(tenant_id, user_id)`` or refresh an existing one.

    The DB ``(tenant_id, google_sub)`` unique constraint makes a duplicate
    identity a reconnect into the same row rather than a second row. If the
    identity already exists under the tenant but owned by a different user,
    binding is refused instead of silently re-assigning it.

    A reconnect replaces the access token but keeps the stored refresh token
    unless Google issued a new one; see :func:`merge_credentials`.
    """
    current = now or datetime.now(UTC)
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
        _store_credentials(
            existing,
            merge_credentials(
                decrypt_credentials(existing.credentials_encrypted),
                access_token=access_token,
                refresh_token=refresh_token,
            ),
            expires_in=expires_in,
            now=current,
        )
        account = existing
    else:
        account = GoogleAccount(
            tenant_id=tenant_id,
            user_id=user_id,
            google_sub=google_sub,
            email=email,
            scopes=scopes,
        )
        _store_credentials(
            account,
            merge_credentials(
                None, access_token=access_token, refresh_token=refresh_token
            ),
            expires_in=expires_in,
            now=current,
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

    Unconditional: the refresh token is exchanged even when the stored access
    token has not expired. The new access token is re-encrypted before
    persisting; a Google rejection (revoked / invalid refresh token) surfaces
    as :class:`GoogleOAuthError` before any stored credential is touched.
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

    await _exchange_refresh_token(
        session,
        transport,
        account=account,
        credentials=decrypt_credentials(account.credentials_encrypted),
        now=datetime.now(UTC),
    )
    await session.commit()
    await session.refresh(account)
    return account
