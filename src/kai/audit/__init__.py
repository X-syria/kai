"""Read-only INTENT-attempt audit surfaces (P2-A).

This package answers one narrow question -- "which recorded INTENT attempts have
no AUTHORIZED reference at query time?" -- and nothing else. It never writes,
never expires, never dispatches, and never derives a verdict.

See :mod:`kai.audit.reader` for the exact limits of what the answer means.
"""

from kai.audit.reader import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    NOT_EVIDENCE_OF_DISPATCH,
    list_intent_attempts_without_authorization,
)
from kai.audit.schemas import (
    IntentAttemptListResponse,
    IntentAttemptResponse,
)

__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "NOT_EVIDENCE_OF_DISPATCH",
    "IntentAttemptListResponse",
    "IntentAttemptResponse",
    "list_intent_attempts_without_authorization",
]
