"""KAI Approval Manager (v0.5 slice).

A real, persisted, tenant-scoped approval pipeline:

    Policy = REQUIRE_APPROVAL
        -> request (PENDING)
        -> human decision
        -> APPROVED / DENIED / EXPIRED

Bindings are exact: an approval is keyed to one canonical tool intent via a
SHA-256 ``intent_hash``, is single-use, lazy-expires, and can never override a
Policy DENY. The gateway consults this manager *only* when Policy says
REQUIRE_APPROVAL.
"""

from kai.approvals.errors import (
    ApprovalError,
    ApprovalNotFoundError,
    ApprovalSuggestionNotFoundError,
    ApprovedApprovalError,
    ExpiredApprovalError,
    InvalidApprovalTransition,
    InvalidSuggestionTransition,
)
from kai.approvals.models import Approval, ApprovalRuleSuggestion, ApprovalStatus
from kai.approvals.service import (
    ApprovalAction,
    ApprovalService,
    build_request_summary,
    canonical_payload,
    intent_hash,
)

__all__ = [
    "Approval",
    "ApprovalAction",
    "ApprovalError",
    "ApprovalNotFoundError",
    "ApprovalRuleSuggestion",
    "ApprovalService",
    "ApprovalStatus",
    "ApprovalSuggestionNotFoundError",
    "ApprovedApprovalError",
    "ExpiredApprovalError",
    "InvalidApprovalTransition",
    "InvalidSuggestionTransition",
    "build_request_summary",
    "canonical_payload",
    "intent_hash",
]