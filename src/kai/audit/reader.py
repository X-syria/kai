"""On-demand, read-only reader for INTENT attempts without an AUTHORIZED link.

The only thing this module does is one ``SELECT``: a tenant/user's
``intent_attempts`` rows whose L2 ``authorization_id`` is NULL, newest first,
bounded by an explicit limit. It is a *reader*; there is deliberately no
expiry, no sweep, no background job, no timer, no alert, no consume, no gateway
call, no provider call, and no task-state transition anywhere on this path.

What an empty or non-empty answer means -- stated here because the data is easy
to over-read:

* A row means an attempt was recorded and, **at query time**, no AUTHORIZED
  reference was recorded for it. That is all.
* "No AUTHORIZED record found at query time" is **not** "not sent". The record
  may be missing because the write never committed (a crash before the commit,
  or any error before it), because the owning tenant/user/task was deleted
  (``ON DELETE CASCADE`` removes its attempt rows), because the row was written
  by a version that has no L2 writer yet, or because an authorization exists in
  a future subsystem that this reader does not join. Silence is not evidence of
  non-dispatch.
* Approval state and concurrent attempts change the interpretation. On a
  compare-and-set failure the attempt row commits even though **that attempt**
  did not consume the approval, and a concurrent attempt may have consumed it
  since - so "attempt recorded" and "approval used" are independent facts, in
  both directions. This reader does not read the approval at all.
* Nothing here says an attempt is stuck, failed, not sent, or safe to retry. A
  resolved verdict is **not derivable** from this table, and the API response
  says so in machine-readable form (``evidence_status``).

That is why the reader returns the rows plus an explicit non-claim, instead of
a status per row.
"""

from __future__ import annotations

from typing import Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kai.db.models import IntentAttempt

#: Default and hard upper bound for one page. A bounded page keeps the endpoint
#: on-demand: there is no unbounded scan of the table.
DEFAULT_LIMIT: Final = 50
MAX_LIMIT: Final = 200

#: Machine-readable non-claim carried by every response: this list is not
#: evidence that anything was dispatched, nor that it was not.
NOT_EVIDENCE_OF_DISPATCH: Final = "NOT_EVIDENCE_OF_DISPATCH_OR_NON_DISPATCH"


async def list_intent_attempts_without_authorization(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    user_id: UUID,
    limit: int = DEFAULT_LIMIT,
) -> list[IntentAttempt]:
    """Return one tenant/user's unauthorized INTENT attempts, newest first.

    ``tenant_id`` and ``user_id`` are **required keyword arguments and are always
    the authenticated principal's**; there is no code path here that takes either
    from a query parameter or a request body, so another tenant's rows cannot be
    requested at all.

    Read-only by construction: a single ``SELECT``, no ``commit``, no ``flush``,
    no lazy expiry, no ORM mutation. The request-scoped session is closed (and
    its implicit read transaction rolled back) by the caller's session factory;
    this function never ends the transaction itself, because doing so would be a
    write-shaped operation on a read path.

    Rows are ordered by ``created_at`` descending with the primary key as a
    deterministic tie-break, so a page is stable. ``limit`` is refused outside
    ``[1, MAX_LIMIT]`` rather than silently clamped: a caller that asks for
    something else has a bug, and a silent clamp would hide it.
    """
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be in [1, {MAX_LIMIT}]")

    stmt = (
        select(IntentAttempt)
        .where(
            IntentAttempt.tenant_id == tenant_id,
            IntentAttempt.user_id == user_id,
            # No AUTHORIZED reference recorded for this attempt.
            IntentAttempt.authorization_id.is_(None),
        )
        .order_by(IntentAttempt.created_at.desc(), IntentAttempt.id.asc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "NOT_EVIDENCE_OF_DISPATCH",
    "list_intent_attempts_without_authorization",
]
