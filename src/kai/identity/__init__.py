"""KAI Identity Resolver (deterministic).

Answers "who is this person/entity, and how much can we trust that identity?"
from trustable sources only — Google Contacts (exact email/phone), the
tenant/user-scoped Google account (provider subject / provider-verified email),
and a small channel rule registry (gmail / calendar). No LLM, no fuzzy
matching, no identity graph. The resolver never decides ALLOW/DENY; it reports
an ``IdentityResult`` (CONTACT_MATCH / PROVIDER_VERIFIED / CHANNEL_VERIFIED /
UNKNOWN / CONFLICTING) and the Policy Engine decides what is allowed.
"""

from kai.identity.errors import IdentityError, IdentityResolutionError
from kai.identity.models import (
    EvidenceKind,
    IdentityEvidence,
    IdentityResult,
    IdentityStatus,
    derive_confidence,
)
from kai.identity.schemas import IdentityQuery
from kai.identity.service import DEFAULT_CHANNELS, ChannelRule, IdentityResolver

__all__ = [
    "ChannelRule",
    "DEFAULT_CHANNELS",
    "EvidenceKind",
    "IdentityError",
    "IdentityEvidence",
    "IdentityQuery",
    "IdentityResolutionError",
    "IdentityResolver",
    "IdentityResult",
    "IdentityStatus",
    "derive_confidence",
]