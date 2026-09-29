"""Google OAuth endpoints: connect, callback, and account listing.

``/connect`` is behind auth and returns the Google authorization URL carrying a
signed ``state`` bound to the caller's ``(user_id, tenant_id)``. ``/callback``
restores that binding from the ``state`` alone (the browser that returns from
Google has no Bearer header), so a callback can only ever attach the Google
identity to the user/tenant that started it. ``/accounts`` never surfaces
tokens or encrypted blobs.
"""

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kai.auth.deps import get_current_user
from kai.config import get_settings
from kai.db.engine import get_session
from kai.db.models import GoogleAccount, User
from kai.db.scoping import get_user_for_tenant
from kai.google import client
from kai.google.client import GoogleOAuthError, build_authorization_url
from kai.google.deps import get_google_http_client
from kai.google.service import (
    GoogleAccountConflictError,
    bind_or_reconnect_google_account,
)
from kai.google.state import InvalidStateError, create_state, validate_state

router = APIRouter(prefix="/auth/google", tags=["google-oauth"])


@router.get("/connect")
async def google_connect(current: User = Depends(get_current_user)) -> dict:
    settings = get_settings()
    state = create_state(user_id=current.id, tenant_id=current.tenant_id)
    scopes = [
        *settings.google_oauth_scopes.split(),
        settings.google_contacts_scope,
        settings.google_calendar_scope,
        settings.google_gmail_read_scope,
        settings.google_gmail_send_scope,
    ]
    authorization_url = build_authorization_url(
        scopes=scopes,
        redirect_uri=settings.google_oauth_redirect_uri,
        state=state,
    )
    return {"authorization_url": authorization_url}


@router.get("/callback")
async def google_callback(
    state: str | None = None,
    code: str | None = None,
    error: str | None = None,
    session: AsyncSession = Depends(get_session),
    transport: httpx.AsyncClient = Depends(get_google_http_client),
) -> dict:
    if error is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"google authorization failed: {error}",
        )
    if state is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="missing oauth state"
        )
    try:
        claims = validate_state(state)
    except InvalidStateError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid or expired oauth state",
        ) from exc

    user = await get_user_for_tenant(
        session, tenant_id=claims.tenant_id, user_id=claims.user_id
    )
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="account not found for this tenant",
        )
    if code is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="missing authorization code",
        )

    settings = get_settings()
    try:
        token = await client.exchange_code(
            transport, code=code, redirect_uri=settings.google_oauth_redirect_uri
        )
        info = await client.fetch_userinfo(transport, access_token=token.access_token)
    except GoogleOAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)
        ) from exc

    if not info.email_verified:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="google email is not verified",
        )

    try:
        account = await bind_or_reconnect_google_account(
            session,
            tenant_id=user.tenant_id,
            user_id=user.id,
            google_sub=info.sub,
            email=info.email,
            scopes=settings.google_oauth_scopes,
            access_token=token.access_token,
            refresh_token=token.refresh_token,
            expires_in=token.expires_in,
        )
    except GoogleAccountConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc

    return {
        "connected": True,
        "email": info.email,
        "google_sub": info.sub,
        "account_id": str(account.id),
    }


@router.get("/accounts")
async def google_accounts(
    current: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    result = await session.execute(
        select(GoogleAccount)
        .where(
            GoogleAccount.tenant_id == current.tenant_id,
            GoogleAccount.user_id == current.id,
        )
        .order_by(GoogleAccount.created_at, GoogleAccount.id)
    )
    accounts = result.scalars().all()
    return {
        "accounts": [
            {
                "account_id": str(account.id),
                "email": account.email,
                "google_sub": account.google_sub,
                "scopes": account.scopes,
                "token_expires_at": (
                    account.token_expires_at.isoformat()
                    if account.token_expires_at is not None
                    else None
                ),
            }
            for account in accounts
        ]
    }