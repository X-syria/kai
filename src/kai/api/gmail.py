"""Gmail read endpoints.

Every operation resolves the user's Google account through
``(current.tenant_id, current.user_id)`` — an ``account_id`` from another
tenant simply does not resolve. Message bodies arrive as T2 untrusted external
data and are returned as data, never executed or allowed to change policy.
Gmail exposes no read receipts, so read acknowledgement is never claimed.

**This module performs no external side effect.** Sending was removed from the
public API because it reached ``send_and_verify`` directly, skipping the
ToolGateway — and with it policy, approval, identity resolution, durable
evidence, audit and alerting. ``POST /gmail/send`` now answers ``410 Gone`` and
names the governed path, where
the sender is taken from the stored account, never from the request.
"""

from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kai.auth.deps import get_current_user
from kai.db.engine import get_session
from kai.db.models import GoogleAccount, User
from kai.gmail.client import (
    GoogleApiError,
    GoogleForbiddenError,
    GoogleInvalidTokenError,
    GoogleNotFoundError,
    GoogleRateLimitError,
)
from kai.gmail.models import EmailAddress, GmailMessage, MessageRef
from kai.gmail.service import (
    get_message as service_get_message,
)
from kai.gmail.service import (
    search_messages as service_search_messages,
)
from kai.google.client import GoogleOAuthError
from kai.google.deps import get_google_http_client
from kai.google.service import GOOGLE_RECONNECT_DETAIL, GoogleReconnectRequiredError

router = APIRouter(prefix="/gmail", tags=["gmail"])

MAX_SEARCH_RESULTS = 50

#: The single governed path to an external write. Named here so a caller that
#: used this endpoint is told where to go rather than only that it failed.
GOVERNED_WRITE_PATH = "/runtime/tasks"


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


def _serialize_email_address(address: EmailAddress) -> dict:
    return {"name": address.name, "email": address.email}


def _serialize_message(message: GmailMessage) -> dict:
    return {
        "message_id": message.message_id,
        "thread_id": message.thread_id,
        "subject": message.subject,
        "from": (
            _serialize_email_address(message.from_addr)
            if message.from_addr is not None
            else None
        ),
        "to": [_serialize_email_address(address) for address in message.to],
        "date": message.date,
        "snippet": message.snippet,
        "body": message.body_text,
        "label_ids": list(message.label_ids),
    }


def _serialize_ref(ref: MessageRef) -> dict:
    return {"message_id": ref.message_id, "thread_id": ref.thread_id}


@router.get("/search")
async def gmail_search(
    q: str = Query(min_length=1, max_length=500),
    max_results: int = Query(default=10, ge=1, le=MAX_SEARCH_RESULTS),
    account_id: UUID | None = None,
    current: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    transport: httpx.AsyncClient = Depends(get_google_http_client),
) -> dict:
    account = await _resolve_account(session, current, account_id)
    try:
        refs = await service_search_messages(
            session,
            transport,
            account_id=account.id,
            tenant_id=current.tenant_id,
            query=q,
            max_results=max_results,
        )
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
    return {"query": q, "messages": [_serialize_ref(ref) for ref in refs]}


@router.get("/messages/{message_id}")
async def gmail_read(
    message_id: str,
    account_id: UUID | None = None,
    current: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    transport: httpx.AsyncClient = Depends(get_google_http_client),
) -> dict:
    account = await _resolve_account(session, current, account_id)
    try:
        message = await service_get_message(
            session,
            transport,
            account_id=account.id,
            tenant_id=current.tenant_id,
            message_id=message_id,
        )
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
    except GoogleNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except GoogleApiError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        ) from exc
    return _serialize_message(message)



@router.post("/send")
async def gmail_send_retired() -> None:
    """Refuse the retired direct send and name the governed path.

    This endpoint used to call ``send_and_verify`` directly. That made it a
    second, ungoverned route to a provider write the ToolGateway already
    governs: no policy, no approval (the catalog rates ``gmail.send`` MEDIUM,
    and MEDIUM requires human approval), no identity resolution, no durable
    evidence, no audit, no alerting. A send is also not repeatable — a retry
    after an unverified outcome delivers twice — so it is the worst possible
    operation to leave unguarded.

    It is kept as an explicit ``410`` rather than deleted so a caller is told
    where the capability went instead of only receiving a bare ``404``. It
    performs no external side effect, so it cannot reintroduce the bypass.
    """
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail={
            "error": "direct_gmail_send_removed",
            "reason": (
                "this endpoint bypassed the governed execution boundary "
                "(policy, approval, identity, evidence, audit)"
            ),
            "use": GOVERNED_WRITE_PATH,
        },
    )
