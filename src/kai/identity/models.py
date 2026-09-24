"""Identity Resolver domain shapes.

The resolver answers, deterministically, "who is this person or entity, and how
much can we trust that identity?" — no LLM, no fuzzy matching, no probabilities.

The three positive trust categories are deliberately distinct and never
interchangeable:

* ``CONTACT_MATCH`` — a trusted identifier (exact email or exact phone) appears
  in the tenant's Google Contacts.
* ``CHANNEL_VERIFIED`` — the identity is bound through a declared channel
  (``gmail`` / ``calendar``) to the connected Google account.
* ``PROVIDER_VERIFIED`` — the identity is anchored to a provider-guaranteed
  subject (``google_sub``) or to the provider-verified account email.

None of these make the *content* of any message trusted: identity answers
"who", never "what should be done". A trusted identity does not turn external
message text into instructions.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class IdentityStatus(StrEnum):
    """Deterministic verdict the resolver can vouch for about one entity."""

    CONTACT_MATCH = "CONTACT_MATCH"
    PROVIDER_VERIFIED = "PROVIDER_VERIFIED"
    CHANNEL_VERIFIED = "CHANNEL_VERIFIED"
    UNKNOWN = "UNKNOWN"
    CONFLICTING = "CONFLICTING"


class EvidenceKind(StrEnum):
    """The fixed vocabulary of deterministic identity evidence."""

    CONTACT_EMAIL = "contact_email_exact"
    CONTACT_PHONE = "contact_phone_exact"
    PROVIDER_SUBJECT = "provider_subject"
    PROVIDER_EMAIL = "provider_email"
    CHANNEL_IDENTITY = "channel_identity"


#: Deterministic strength of each evidence kind; resolution confidence is always
#: derived from these fixed numbers, never from a model or a guess.
_EVIDENCE_STRENGTH: Final[dict[EvidenceKind, float]] = {
    EvidenceKind.PROVIDER_SUBJECT: 0.9,
    EvidenceKind.PROVIDER_EMAIL: 0.9,
    EvidenceKind.CHANNEL_IDENTITY: 0.7,
    EvidenceKind.CONTACT_EMAIL: 0.5,
    EvidenceKind.CONTACT_PHONE: 0.5,
}

@dataclass(frozen=True)
class IdentityEvidence:
    """One deterministic finding for (or against) an identity.

    ``matched_value`` carries a normalized identifier (email / phone / provider
    subject) only. Message content can never appear here.
    """

    kind: EvidenceKind
    source: str
    matched_value: str
    contact_id: str | None = None

    @property
    def strength(self) -> float:
        return _EVIDENCE_STRENGTH[self.kind]

    def to_audit(self) -> dict[str, str | None]:
        return {
            "kind": self.kind.value,
            "source": self.source,
            "contact_id": self.contact_id,
        }


@dataclass(frozen=True)
class IdentityResult:
    """One resolution verdict plus the evidence it was derived from.

    ``contact_id`` is the matched contact's Google ``resourceName`` (contacts
    are read per request and never persisted, so there is no local UUID to
    name). ``display_name`` is informational only — a display name never
    establishes identity by itself.
    """

    status: IdentityStatus
    confidence: float
    contact_id: str | None = None
    provider: str | None = None
    provider_subject: str | None = None
    email: str | None = None
    phone: str | None = None
    display_name: str | None = None
    matched_by: str | None = None
    evidence: tuple[IdentityEvidence, ...] = ()

    def to_audit(self) -> dict[str, object]:
        """Safe audit metadata; never carries credentials or content."""
        return {
            "status": self.status.value,
            "confidence": self.confidence,
            "contact_id": self.contact_id,
            "provider": self.provider,
            "provider_subject": self.provider_subject,
            "email": self.email,
            "matched_by": self.matched_by,
            "evidence": [entry.to_audit() for entry in self.evidence],
        }


def derive_confidence(*, status: IdentityStatus, evidence: tuple[IdentityEvidence, ...]) -> float:
    """Evidence-derived, deterministic confidence for a resolution verdict.

    UNKNOWN and CONFLICTING always carry zero confidence. Positive verdicts are
    scored from the fixed strength table; two independent trusted sources add a
    capped corroboration bonus. Nothing here is random or model-produced.
    """
    if status in (IdentityStatus.UNKNOWN, IdentityStatus.CONFLICTING):
        return 0.0
    strengths = [entry.strength for entry in evidence]
    base = max(strengths) if strengths else 0.0
    if len({entry.source for entry in evidence}) >= 2:
        base = min(1.0, base + 0.1)
    return base


__all__ = [
    "EvidenceKind",
    "IdentityEvidence",
    "IdentityResult",
    "IdentityStatus",
    "derive_confidence",
]