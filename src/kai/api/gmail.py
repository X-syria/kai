"""Gmail vertical slice endpoints.

Every operation resolves the user's Google account through
``(current.tenant_id, current.user_id)`` — an ``account_id`` from another
tenant simply does not resolve. Message bodies arrive as T2 untrusted external
data and are returned as data, never executed or allowed to change policy.
Sending reports success only after deterministic read-after-send verification;
the sender is taken from the stored account, never from the request. Gmail
exposes no read receipts, so read acknowledgement is never claimed.
"""

from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, EmailStr, Field
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
    AccountNotFoundError,
    SendRequest,
    send_and_verify,
)
from kai.gmail.service import (
    get_message as service_get_message,
)
from kai.gmail.service import (
    search_messages as service_search_messages,
)
from kai.google.deps import get_google_http_client

router = APIRouter(prefix="/gmail", tags=["gmail"])

MAX_SEARCH_RESULTS = 50


class SendBody(BaseModel):
    to: EmailStr
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=100_000)


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
async def gmail_send(
    body: SendBody,
    account_id: UUID | None = None,
    current: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    transport: httpx.AsyncClient = Depends(get_google_http_client),
) -> dict:
    if "\r" in body.subject or "\n" in body.subject:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="subject must not contain line breaks",
        )
    account = await _resolve_account(session, current, account_id)
    req = SendRequest(to=str(body.to), subject=body.subject, body=body.body)
    try:
        outcome = await send_and_verify(
            session,
            transport,
            account_id=account.id,
            tenant_id=current.tenant_id,
            req=req,
        )
    except AccountNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
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

    if not outcome.verified:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "verified": False,
                "mismatches": [
                    {
                        "field": mismatch.field,
                        "expected": mismatch.expected,
                        "actual": mismatch.actual,
                    }
                    for mismatch in outcome.mismatches
                ],
            },
        )
    evidence = _evidence(outcome.message) if outcome.message is not None else {}
    return {
        "verified": True,
        "evidence": evidence,
        "read_by_recipient": None,
        "message": _serialize_message(outcome.message) if outcome.message else None,
    }


def _evidence(message: GmailMessage) -> dict:
    """Concrete proof of dispatch from Gmail's own records.

    A message id, thread, and SENT label prove the provider accepted the
    message. Gmail's API exposes no read receipts, so nothing here claims the
    recipient read it.
    """
    return {
        "message_id": message.message_id,
        "thread_id": message.thread_id,
        "label_ids": list(message.label_ids),
    }