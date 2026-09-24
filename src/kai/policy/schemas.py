"""Deterministic content-policy vocabulary.

This is *not* a classifier: the mapping from a ``ContentCategory`` to a
handling mode (allow / escalate / block) is a table, decided by the
application, never by a model. The category itself is supplied by the trusted
execution context — not parsed out of the message body — so even an
injection-looking message cannot change the outcome on its own.
"""

from __future__ import annotations

from enum import StrEnum

# --- categories the MVP recognizes -----------------------------------------
ALLOWED_CATEGORIES = frozenset(
    {
        "propose_time",
        "confirm_meeting",
        "reschedule_meeting",
        "cancel_meeting",
        "short_courtesy_message",
    }
)
ESCALATION_CATEGORIES = frozenset(
    {"request_unrelated_task", "share_document", "financial_mention"}
)
BLOCKED_CATEGORIES = frozenset(
    {
        "credential_request",
        "secret_disclosure",
        "permission_change_request",
        "payment_request",
    }
)
_KNOWN_CATEGORIES = ALLOWED_CATEGORIES | ESCALATION_CATEGORIES | BLOCKED_CATEGORIES


class ContentCategory(StrEnum):
    """The exact, closed set of content categories the MVP understands.

    Anything outside this set cannot be classified deterministically and is
    therefore escalated, never silently allowed.
    """

    PROPOSE_TIME = "propose_time"
    CONFIRM_MEETING = "confirm_meeting"
    RESCHEDULE_MEETING = "reschedule_meeting"
    CANCEL_MEETING = "cancel_meeting"
    SHORT_COURTESY_MESSAGE = "short_courtesy_message"
    REQUEST_UNRELATED_TASK = "request_unrelated_task"
    SHARE_DOCUMENT = "share_document"
    FINANCIAL_MENTION = "financial_mention"
    CREDENTIAL_REQUEST = "credential_request"
    SECRET_DISCLOSURE = "secret_disclosure"
    PERMISSION_CHANGE_REQUEST = "permission_change_request"
    PAYMENT_REQUEST = "payment_request"

    @classmethod
    def from_value(cls, value: object) -> ContentCategory | None:
        """Map a trusted context value to a category, or None when unknown."""
        if not isinstance(value, str) or value not in _KNOWN_CATEGORIES:
            return None
        return cls(value)


class ContentMode(StrEnum):
    """What the application decides to do with one category."""

    ALLOW = "ALLOW"
    ESCALATE = "ESCALATE"
    BLOCK = "BLOCK"


CATEGORY_MODE: dict[ContentCategory, ContentMode] = {
    ContentCategory.PROPOSE_TIME: ContentMode.ALLOW,
    ContentCategory.CONFIRM_MEETING: ContentMode.ALLOW,
    ContentCategory.RESCHEDULE_MEETING: ContentMode.ALLOW,
    ContentCategory.CANCEL_MEETING: ContentMode.ALLOW,
    ContentCategory.SHORT_COURTESY_MESSAGE: ContentMode.ALLOW,
    ContentCategory.REQUEST_UNRELATED_TASK: ContentMode.ESCALATE,
    ContentCategory.SHARE_DOCUMENT: ContentMode.ESCALATE,
    ContentCategory.FINANCIAL_MENTION: ContentMode.ESCALATE,
    ContentCategory.CREDENTIAL_REQUEST: ContentMode.BLOCK,
    ContentCategory.SECRET_DISCLOSURE: ContentMode.BLOCK,
    ContentCategory.PERMISSION_CHANGE_REQUEST: ContentMode.BLOCK,
    ContentCategory.PAYMENT_REQUEST: ContentMode.BLOCK,
}

__all__ = [
    "ALLOWED_CATEGORIES",
    "BLOCKED_CATEGORIES",
    "CATEGORY_MODE",
    "ContentCategory",
    "ContentMode",
    "ESCALATION_CATEGORIES",
]