"""The real, deterministic Identity Resolver.

The resolver answers "who is this person/entity" from trustable sources only:

* **Google Contacts** — read live per request, tenant-scoped. Only *exact*
  email and *exact* phone are identity-establishing; a name or a substring
  never is.
* **Google Account identity** — the tenant/user-scoped ``GoogleAccount`` row is
  provider metadata from the OAuth handshake. A matching ``google_sub``
  (provider subject) or the provider-verified account email anchors
  ``PROVIDER_VERIFIED``.
* **Channels** — a small, extensible rule registry models how a declared
  channel (``gmail`` / ``calendar``) binds an identifier to the connected
  account.

Guarantees:

* Scope is always the trusted ``ToolContext`` (tenant/user/account); the query
  can never move or widen it.
* Message content — body, subject, signature, quoted text, a claimed display
  name — never establishes identity. ``name``/``external_id`` are carried for
  the record and may increase ambiguity (CONFLICTING), never trust.
* Contradictory evidence (email points to one contact, provider subject points
  to the account, and they disagree) is ``CONFLICTING`` — the resolver never
  guesses a winner.
* The resolver never decides ALLOW/DENY; it returns ``IdentityResult`` and
  policy decides.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import httpx
from sqlalchemy import select

from kai.contacts.client import GoogleApiError
from kai.contacts.models import Contact
from kai.contacts.service import AccountNotFoundError, list_contacts, match_contacts
from kai.db.models import GoogleAccount
from kai.google.client import GoogleOAuthError
from kai.identity.models import (
    EvidenceKind,
    IdentityEvidence,
    IdentityResult,
    IdentityStatus,
    derive_confidence,
)
from kai.identity.schemas import IdentityQuery
from kai.logging import get_logger

if TYPE_CHECKING:
    from kai.tools.context import ToolContext

ContactsLoader = Callable[..., Awaitable[Sequence[Contact]]]


@dataclass(frozen=True)
class ChannelRule:
    """How one channel binds an identifier to the connected account.

    ``binds_email``: the query's email is bound when it equals the connected
    account's provider-verified email. ``binds_subject``: the query's
    ``provider_account_id`` is bound when it equals the account's ``google_sub``.
    """

    name: str
    binds_email: bool = True
    binds_subject: bool = True


#: v0.5 channel registry. Deliberately small and sealed-per-construction; a
#: future slice extends it without touching the resolver's decision procedure.
DEFAULT_CHANNELS: Final[dict[str, ChannelRule]] = {
    "gmail": ChannelRule(name="gmail"),
    "calendar": ChannelRule(name="calendar"),
}


@dataclass(frozen=True)
class _Claim:
    """One exact-identifier match against a trusted contact."""

    kind: EvidenceKind
    value: str
    contact: Contact

    @property
    def evidence(self) -> IdentityEvidence:
        return IdentityEvidence(
            kind=self.kind,
            source="google_contacts",
            matched_value=self.value,
            contact_id=self.contact.resource_name,
        )


class IdentityResolver:
    """Tenant/user/account-scoped, fully deterministic identity resolution."""

    def __init__(
        self,
        *,
        channels: dict[str, ChannelRule] | None = None,
        contacts_loader: ContactsLoader | None = None,
    ) -> None:
        self._channels = channels if channels is not None else DEFAULT_CHANNELS
        self._contacts_loader = contacts_loader if contacts_loader is not None else list_contacts
        self._log = get_logger("kai.identity.resolver")

    # ------------------------------------------------------------------ #
    # Public resolution
    # ------------------------------------------------------------------ #

    async def resolve(
        self,
        *,
        query: IdentityQuery,
        context: ToolContext,
    ) -> IdentityResult:
        """Resolve one entity against trustable sources.

        Scope (``tenant_id`` / ``user_id`` / ``google_account_id``) always
        comes from ``context`` — the query is only ever data to match on.
        """
        if not (
            query.name
            or query.email
            or query.phone
            or query.provider_account_id
            or query.channel
        ):
            # No identifier that could ever constrain the answer; nothing claimed.
            return IdentityResult(status=IdentityStatus.UNKNOWN, confidence=0.0)

        account = await self._load_account(context)

        contacts = await self._load_contacts(context, account)

        exact_claims = _exact_claims(query, contacts) if contacts else []
        candidates = _broad_candidates(query, contacts) if contacts else []

        evidence_parts: list[IdentityEvidence] = [claim.evidence for claim in exact_claims]

        provider_evidence: tuple[IdentityEvidence, ...] = ()
        if account is not None:
            provider_evidence = _provider_evidence(query, account)
            evidence_parts.extend(provider_evidence)

        channel_evidence: tuple[IdentityEvidence, ...] = ()
        if account is not None and query.channel:
            rule = self._channels.get(query.channel.strip().lower())
            if rule is not None:
                channel_evidence = _channel_evidence(rule, query, account)
                evidence_parts.extend(channel_evidence)

        evidence = tuple(evidence_parts)

        if _has_conflict(
            exact_claims=exact_claims,
            candidates=candidates,
            query=query,
            account=account,
            provider_evidence=provider_evidence,
            channel_evidence=channel_evidence,
        ):
            return self._build(IdentityStatus.CONFLICTING, evidence, exact_claims, account)

        if provider_evidence:
            status = IdentityStatus.PROVIDER_VERIFIED
        elif channel_evidence:
            status = IdentityStatus.CHANNEL_VERIFIED
        elif len(exact_claims) == 1:
            status = IdentityStatus.CONTACT_MATCH
        else:
            status = IdentityStatus.UNKNOWN
        return self._build(status, evidence, exact_claims, account)

    # ------------------------------------------------------------------ #
    # Evidence gathering
    # ------------------------------------------------------------------ #

    async def _load_account(self, context: ToolContext) -> GoogleAccount | None:
        """The scoped account, or ``None`` for any unknown/foreign scope.

        Requiring ``(id, tenant_id, user_id)`` together is what makes a foreign
        tenant, a foreign user, or a foreign provider subject resolve to
        "unknown" instead of leaking another scope's identity.
        """
        if context.account_id is None:
            return None
        result = await context.session.execute(
            select(GoogleAccount).where(
                GoogleAccount.id == context.account_id,
                GoogleAccount.tenant_id == context.tenant_id,
                GoogleAccount.user_id == context.user_id,
            )
        )
        return result.scalar_one_or_none()

    async def _load_contacts(
        self, context: ToolContext, account: GoogleAccount | None
    ) -> list[Contact]:
        """Best-effort live account contacts. A failure only *removes* evidence,
        it never fabricates trust; provider/channel verification can still run."""
        if account is None or context.transport is None:
            return []
        try:
            fetched = await self._contacts_loader(
                session=context.session,
                transport=context.transport,
                account_id=account.id,
                tenant_id=account.tenant_id,
            )
            return list(fetched)
        except AccountNotFoundError:
            self._log.warning(
                "identity resolver: contacts unavailable", reason="account_not_found"
            )
        except GoogleOAuthError:
            self._log.warning(
                "identity resolver: contacts unavailable", reason="google_oauth_error"
            )
        except GoogleApiError:
            self._log.warning(
                "identity resolver: contacts unavailable", reason="google_api_error"
            )
        except httpx.HTTPError:
            self._log.warning(
                "identity resolver: contacts unavailable", reason="transport_error"
            )
        return []

    # ------------------------------------------------------------------ #
    # Result assembly
    # ------------------------------------------------------------------ #

    def _build(
        self,
        status: IdentityStatus,
        evidence: tuple[IdentityEvidence, ...],
        exact_claims: list[_Claim],
        account: GoogleAccount | None,
    ) -> IdentityResult:
        distinct_ids = {claim.contact.resource_name for claim in exact_claims}
        contact = exact_claims[0].contact if len(distinct_ids) == 1 else None

        email: str | None = None
        phone: str | None = None
        if status in (IdentityStatus.PROVIDER_VERIFIED, IdentityStatus.CHANNEL_VERIFIED):
            if account is not None and account.email:
                email = account.email.strip() or None
        else:
            email_claims = [
                claim for claim in exact_claims if claim.kind is EvidenceKind.CONTACT_EMAIL
            ]
            phone_claims = [
                claim for claim in exact_claims if claim.kind is EvidenceKind.CONTACT_PHONE
            ]
            if email_claims:
                email = email_claims[0].value
            if phone_claims:
                phone = phone_claims[0].value

        provider = "google" if status is IdentityStatus.PROVIDER_VERIFIED else None
        provider_subject: str | None = None
        if status is IdentityStatus.PROVIDER_VERIFIED:
            for entry in evidence:
                if entry.kind is EvidenceKind.PROVIDER_SUBJECT:
                    provider_subject = entry.matched_value
                    break

        matched_by: str | None = None
        if evidence:
            strongest = max(evidence, key=lambda entry: entry.strength)
            matched_by = strongest.kind.value

        display_name: str | None = None
        if contact is not None and contact.display_name:
            display_name = contact.display_name

        return IdentityResult(
            status=status,
            confidence=derive_confidence(status=status, evidence=evidence),
            contact_id=contact.resource_name if contact is not None else None,
            provider=provider,
            provider_subject=provider_subject,
            email=email,
            phone=phone,
            display_name=display_name,
            matched_by=matched_by,
            evidence=evidence,
        )


def _has_conflict(
    *,
    exact_claims: list[_Claim],
    candidates: list[str],
    query: IdentityQuery,
    account: GoogleAccount | None,
    provider_evidence: tuple[IdentityEvidence, ...],
    channel_evidence: tuple[IdentityEvidence, ...],
) -> bool:
    """Deterministic conflict detection; CONFLICTING never picks a winner."""
    # C1: distinct exact identifiers point at different contacts.
    if len({claim.contact.resource_name for claim in exact_claims}) > 1:
        return True

    bound_to_account = bool(provider_evidence or channel_evidence)
    if bound_to_account and account is not None:
        account_email = _norm_email(account.email)
        queried_email = _norm_email(query.email)
        # C2/C3: a bound account identity is contradicted by a different queried email.
        if account_email and queried_email and queried_email != account_email:
            return True
        # C5: an exact contact/phone claim must belong to the account identity.
        if account_email:
            for claim in exact_claims:
                contact_emails = {_norm_email(address) for address in claim.contact.emails}
                if account_email not in contact_emails:
                    return True

    # C4: multiple plausible candidates and no exact identifier to narrow them.
    return not exact_claims and len(candidates) > 1


def _exact_claims(query: IdentityQuery, contacts: Sequence[Contact]) -> list[_Claim]:
    """Exact (identity-establishing) email/phone matches only."""
    claims: list[_Claim] = []
    email = _norm_email(query.email)
    if email:
        for contact in contacts:
            if any(_norm_email(address) == email for address in contact.emails):
                claims.append(_Claim(EvidenceKind.CONTACT_EMAIL, email, contact))
    phone = _norm_phone(query.phone)
    if phone:
        for contact in contacts:
            if any(_norm_phone(number) == phone for number in contact.phone_numbers):
                claims.append(_Claim(EvidenceKind.CONTACT_PHONE, phone, contact))
    return claims


def _broad_candidates(query: IdentityQuery, contacts: Sequence[Contact]) -> list[str]:
    """Broad candidate set via ``match_contacts`` (name/email/phone substring).

    Used for *ambiguity* detection only; a name-match alone can never establish
    identity, it can only make the answer CONFLICTING when no exact identifier
    exists. Delegates to the existing matcher instead of reimplementing it.
    """
    ids: list[str] = []
    for identifier in (query.name, query.email, query.phone):
        if not identifier:
            continue
        for contact in match_contacts(list(contacts), identifier):
            if contact.resource_name not in ids:
                ids.append(contact.resource_name)
    return ids


def _provider_evidence(
    query: IdentityQuery, account: GoogleAccount
) -> tuple[IdentityEvidence, ...]:
    """Provider-verified identity, derived only from scoped provider metadata."""
    if (query.provider or "").strip().lower() != "google":
        return ()
    findings: list[IdentityEvidence] = []
    subject = (query.provider_account_id or "").strip()
    if subject and subject == account.google_sub:
        findings.append(
            IdentityEvidence(
                kind=EvidenceKind.PROVIDER_SUBJECT,
                source="google_account",
                matched_value=subject,
            )
        )
    email = _norm_email(query.email)
    account_email = _norm_email(account.email)
    if email and account_email and email == account_email:
        findings.append(
            IdentityEvidence(
                kind=EvidenceKind.PROVIDER_EMAIL,
                source="google_account",
                matched_value=email,
            )
        )
    return tuple(findings)


def _channel_evidence(
    rule: ChannelRule, query: IdentityQuery, account: GoogleAccount
) -> tuple[IdentityEvidence, ...]:
    """Deterministic channel→account binding for one declared channel."""
    findings: list[IdentityEvidence] = []
    email = _norm_email(query.email)
    account_email = _norm_email(account.email)
    if rule.binds_email and email and account_email and email == account_email:
        findings.append(
            IdentityEvidence(
                kind=EvidenceKind.CHANNEL_IDENTITY,
                source=rule.name,
                matched_value=email,
            )
        )
    subject = (query.provider_account_id or "").strip()
    if rule.binds_subject and subject and subject == account.google_sub:
        findings.append(
            IdentityEvidence(
                kind=EvidenceKind.CHANNEL_IDENTITY,
                source=rule.name,
                matched_value=subject,
            )
        )
    return tuple(findings)


def _norm_email(value: str | None) -> str:
    if not value:
        return ""
    return value.strip().lower()


def _norm_phone(value: str | None) -> str:
    if not value:
        return ""
    return "".join(ch for ch in value.strip() if ch.isdigit())


__all__ = [
    "ChannelRule",
    "DEFAULT_CHANNELS",
    "IdentityResolver",
]