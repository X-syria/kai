"""Read-only audit API — one GET, no mutations.

``GET /audit/intent-attempts`` lists the authenticated principal's recorded
INTENT attempts that have **no AUTHORIZED reference at query time**, newest
first, bounded by an explicit limit.

Hard properties of this module, on purpose:

* **Read-only.** The only route is a ``GET`` that performs one ``SELECT``. There
  is no POST/PATCH/DELETE, no approval decision, no gateway call, no provider
  call, and no task-state change anywhere on this path. It deliberately does not
  reuse ``ApprovalService.list_approvals``: that method lazy-expires rows and
  commits, which is a write.
* **Tenant-scoped by construction.** ``tenant_id``/``user_id`` come from the
  bearer token (:func:`kai.auth.deps.get_current_user`), never from a query
  parameter, so there is no field a caller could set to read another tenant's
  attempts.
* **On demand only.** No background scan, no timer, no alerting, no scheduled
  sweep. Absence of a row here is not an event.
* **No verdict.** The payload says, in machine-readable form, that it is not
  evidence of dispatch or non-dispatch; see :mod:`kai.audit.reader` for why
  "no AUTHORIZED record found at query time" is not "not sent", and why approval
  state and concurrent attempts change the interpretation.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from kai.audit.reader import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    list_intent_attempts_without_authorization,
)
from kai.audit.schemas import IntentAttemptListResponse, IntentAttemptResponse
from kai.auth.deps import get_current_user
from kai.db.engine import get_session
from kai.db.models import User

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("/intent-attempts", response_model=IntentAttemptListResponse)
async def intent_attempts_without_authorization(
    limit: int = Query(
        default=DEFAULT_LIMIT,
        ge=1,
        le=MAX_LIMIT,
        description="page size; bounded so the endpoint stays on-demand",
    ),
    current: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> IntentAttemptListResponse:
    """List attempts with no AUTHORIZED reference recorded, newest first.

    Scoped to the authenticated tenant/user. Read-only: one SELECT, no commit,
    no expiry, no dispatch, no provider call. A listed attempt is not evidence
    that anything was or was not sent; the response carries that non-claim
    explicitly.
    """
    attempts = await list_intent_attempts_without_authorization(
        session,
        tenant_id=current.tenant_id,
        user_id=current.id,
        limit=limit,
    )
    return IntentAttemptListResponse(
        attempts=[
            IntentAttemptResponse.model_validate(
                {
                    "id": attempt.id,
                    "tenant_id": attempt.tenant_id,
                    "user_id": attempt.user_id,
                    "task_id": attempt.task_id,
                    "approval_id": attempt.approval_id,
                    "tool_name": attempt.tool_name,
                    "intent_hash": attempt.intent_hash,
                    "account_id": attempt.account_id,
                    "created_at": attempt.created_at,
                }
            )
            for attempt in attempts
        ]
    )


__all__ = ["router"]
