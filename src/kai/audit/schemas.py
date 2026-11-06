"""Wire schemas for the read-only INTENT-attempt audit endpoint.

Nothing here is an input: the endpoint takes a page size and nothing else, and
ownership always comes from the authenticated principal. Every response carries
an explicit, machine-readable non-claim (``evidence_status``) so a consumer
cannot read this list as a dispatch verdict.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final, Literal
from uuid import UUID

from pydantic import BaseModel

#: The human-readable half of the non-claim. Deliberately part of the payload,
#: not just the docstring: a client that shows this list to a person should have
#: the caveat travel with the data.
_INTERPRETATION: Final = (
    "A listed attempt has no AUTHORIZED reference recorded at query time. That"
    " is not 'not sent': the attempt record may never have been committed, may"
    " have been deleted with its owning user, task, or tenant, and this reader"
    " joins no authorization or dispatch evidence. Approval state and"
    " concurrent attempts change the interpretation - on a compare-and-set"
    " failure the attempt row commits even though that attempt did not consume"
    " the approval. A resolved verdict (sent / not sent / failed / stuck /"
    " retryable) is not derivable here."
)


class IntentAttemptResponse(BaseModel):
    """One recorded INTENT attempt. Read-model only; grants nothing.

    ``authorization_id`` / ``authorized_at_utc`` are omitted because this
    endpoint only ever lists rows where no AUTHORIZED reference exists.
    """

    id: UUID
    tenant_id: UUID
    user_id: UUID
    task_id: UUID
    approval_id: UUID
    tool_name: str
    intent_hash: str
    account_id: UUID | None = None
    created_at: datetime


class IntentAttemptListResponse(BaseModel):
    """A bounded page of attempts plus the explicit non-claim about them."""

    attempts: list[IntentAttemptResponse]
    #: Must stay identical to :data:`kai.audit.reader.NOT_EVIDENCE_OF_DISPATCH`
    #: (asserted in the tests): ``Literal`` cannot be built from a variable
    #: without a type error, so the value is spelled twice on purpose.
    evidence_status: Literal["NOT_EVIDENCE_OF_DISPATCH_OR_NON_DISPATCH"] = (
        "NOT_EVIDENCE_OF_DISPATCH_OR_NON_DISPATCH"
    )
    interpretation: str = _INTERPRETATION


__all__ = [
    "IntentAttemptListResponse",
    "IntentAttemptResponse",
]
