"""Pydantic wire schemas for the Approval Manager API.

These schemas only ever carry *decisions and metadata*: there is no field on
any request that can change the intent, the ownership, or the expiry of an
approval. ``tenant_id``/``user_id`` are read-model outputs from the trusted
context, never inputs.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

_DECISION_REASON_LIMIT = 1000


def _clean_reason(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = " ".join(value.split()).strip()
    if not stripped:
        return None
    return stripped[: _DECISION_REASON_LIMIT]


class ApprovalResponse(BaseModel):
    """A read-only representation of one approval request.

    ``request_summary`` is the human-readable, safe preview shown to the
    approver; ``intent_hash`` is the SHA-256 binding to the exact tool intent.
    Nothing here exposes arguments, tokens, or credentials.
    """

    id: UUID
    tenant_id: UUID
    user_id: UUID
    task_id: UUID
    tool_name: str
    risk_level: str
    status: str
    recipient_class: str = ""
    intent_hash: str
    request_summary: str
    created_at: datetime
    expires_at: datetime
    decided_at: datetime | None = None
    decided_by: UUID | None = None
    decision_reason: str | None = None
    consumed_at: datetime | None = None


class ApprovalListResponse(BaseModel):
    approvals: list[ApprovalResponse]


class ApprovalDecisionRequest(BaseModel):
    """A human decision on an existing approval.

    Only ``reason`` is accepted — the intent itself is bound by the stored
    ``intent_hash`` and can never be edited through this endpoint. Unknown
    fields (status, expiry, ownership, arguments) are rejected outright.
    """

    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=_DECISION_REASON_LIMIT)

    @field_validator("reason")
    @classmethod
    def _clean(cls, value: str | None) -> str | None:
        return _clean_reason(value)


class SuggestionResponse(BaseModel):
    """A stored non-authorizing rule proposal (approval fatigue)."""

    id: UUID
    tenant_id: UUID
    user_id: UUID
    tool_name: str
    risk_level: str
    recipient_class: str
    allowed_tools: list[str]
    allowed_recipients: list[str]
    risk_ceiling: str
    expiration: datetime
    status: str
    created_at: datetime
    rejected_at: datetime | None = None
    reject_reason: str | None = None


class SuggestionListResponse(BaseModel):
    suggestions: list[SuggestionResponse]


class SuggestionDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=_DECISION_REASON_LIMIT)

    @field_validator("reason")
    @classmethod
    def _clean(cls, value: str | None) -> str | None:
        return _clean_reason(value)


__all__ = [
    "ApprovalDecisionRequest",
    "ApprovalListResponse",
    "ApprovalResponse",
    "SuggestionDecisionRequest",
    "SuggestionListResponse",
    "SuggestionResponse",
]