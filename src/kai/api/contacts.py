"""Contacts vertical slice endpoints.

Every operation resolves the user's Google account through
``(current.tenant_id, current.user_id)`` — an ``account_id`` belonging to a
different tenant simply does not resolve. Google People API failures are mapped
to explicit HTTP statuses and no token or credential is ever echoed.
"""

from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kai.auth.deps import get_current_user
from kai.contacts.client import (
    GoogleApiError,
    GoogleForbiddenError,
    GoogleInvalidTokenError,
    GoogleRateLimitError,
)
from kai.contacts.models import Contact
from kai.contacts.service import AccountNotFoundError, list_contacts, match_contacts
from kai.db.engine import get_session
from kai.db.models import GoogleAccount, User
from kai.google.client import GoogleOAuthError
from kai.google.deps import get_google_http_client
from kai.google.service import GOOGLE_RECONNECT_DETAIL, GoogleReconnectRequiredError

router = APIRouter(prefix="/contacts", tags=["contacts"])


def _serialize(contact: Contact) -> dict:
    return {
        "resource_name": contact.resource_name,
        "display_name": contact.display_name,
        "emails": list(contact.emails),
        "phone_numbers": list(contact.phone_numbers),
    }


async def _resolve_account(
    session: AsyncSession, current: User, account_id: UUID | None
) -> GoogleAccount:
    query = select(GoogleAccount).where(
        GoogleAccount.tenant_id == current.tenant_id,
        GoogleAccount.user_id == current.id,
    )
    if account_id is not None:
        query = query.where(GoogleAccount.id == account_id)
    else:
        query = query.order_by(GoogleAccount.created_at, GoogleAccount.id).limit(1)
    account = (await session.execute(query)).scalar_one_or_none()
    if account is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no connected google account",
        )
    return account


@router.get("")
async def google_contacts(
    account_id: UUID | None = None,
    q: str | None = None,
    current: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    transport: httpx.AsyncClient = Depends(get_google_http_client),
) -> dict:
    account = await _resolve_account(session, current, account_id)
    try:
        contacts = await list_contacts(
            session,
            transport,
            account_id=account.id,
            tenant_id=current.tenant_id,
        )
    except AccountNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except GoogleReconnectRequiredError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=GOOGLE_RECONNECT_DETAIL,
        ) from exc
    except GoogleOAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="google provider temporarily unavailable",
        ) from exc
    except GoogleInvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)
        ) from exc
    except GoogleRateLimitError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc)
        ) from exc
    except GoogleForbiddenError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)
        ) from exc
    except GoogleApiError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        ) from exc

    matched = match_contacts(contacts, q) if q is not None else contacts
    return {"contacts": [_serialize(contact) for contact in matched]}