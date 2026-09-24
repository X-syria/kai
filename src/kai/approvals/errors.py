"""Approval Manager error vocabulary.

Every error is short, non-sensitive, and safe to return to a caller. Approval
errors never carry arguments, secrets, or message content.
"""

from __future__ import annotations


class ApprovalError(ValueError):
    """Base class for all Approval Manager errors."""


class ApprovalNotFoundError(ApprovalError):
    """No approval resolves for the requested ``(tenant, user, id)`` tuple.

    A foreign-tenant or foreign-user approval is deliberately indistinguishable
    from one that never existed: it "does not resolve".
    """


class InvalidApprovalTransition(ApprovalError):
    """A decision was requested on a non-PENDING approval.

    A terminal approval (APPROVED / DENIED / EXPIRED) is immutable; changing it
    requires a brand-new approval with a fresh request.
    """


class ApprovedApprovalError(InvalidApprovalTransition):
    """Raised when an already-approved approval is decided again."""


class ExpiredApprovalError(InvalidApprovalTransition):
    """Raised when a decision is attempted on a PENDING approval past expiry.

    The row is lazy-expired first (deterministic) and the decision is refused.
    """


class ApprovalSuggestionNotFoundError(ApprovalError):
    """No rule suggestion resolves for the requested scope."""


class InvalidSuggestionTransition(ApprovalError):
    """A suggestion that is not OFFERED cannot be rejected."""


__all__ = [
    "ApprovalError",
    "ApprovalNotFoundError",
    "ApprovalSuggestionNotFoundError",
    "ApprovedApprovalError",
    "ExpiredApprovalError",
    "InvalidApprovalTransition",
    "InvalidSuggestionTransition",
]