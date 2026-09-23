"""Tenant-scoped Contacts operations on top of the stored Google account."""

from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kai.contacts import client
from kai.contacts.models import Contact
from kai.db.models import GoogleAccount
from kai.google.client import GoogleOAuthError
from kai.google.crypto import decrypt_credentials


class AccountNotFoundError(ValueError):
    """Raised when a Google account does not exist for the current tenant."""


async def list_contacts(
    session: AsyncSession,
    transport: httpx.AsyncClient,
    *,
    account_id: UUID,
    tenant_id: UUID,
) -> list[Contact]:
    """Read the account's contacts, scoped to ``(account_id, tenant_id)``.

    The access token is decrypted from the stored credentials only for the
    duration of this request and is never returned or logged.
    """
    result = await session.execute(
        select(GoogleAccount).where(
            GoogleAccount.id == account_id,
            GoogleAccount.tenant_id == tenant_id,
        )
    )
    account = result.scalar_one_or_none()
    if account is None:
        raise AccountNotFoundError("google account not found for tenant")

    credentials = decrypt_credentials(account.credentials_encrypted)
    access_token = credentials.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise GoogleOAuthError("google account has no stored access token")

    return await client.fetch_all_contacts(transport, access_token=access_token)


def _email_key(value: str) -> str:
    return value.strip().lower()


def _phone_key(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())


def match_contacts(contacts: list[Contact], query: str) -> list[Contact]:
    """Return contacts matching ``query`` by name, email, or phone.

    Empty ``query`` returns all contacts. Multiple matches are the caller's
    "ambiguous" answer and zero matches are the caller's "no-match" answer.
    """
    needle = query.strip()
    if not needle:
        return list(contacts)
    name_needle = needle.lower()
    phone_needle = _phone_key(needle)
    matches: list[Contact] = []
    for contact in contacts:
        if contact.display_name and name_needle in contact.display_name.lower():
            matches.append(contact)
            continue
        if any(name_needle in _email_key(email) for email in contact.emails):
            matches.append(contact)
            continue
        if phone_needle and any(
            _phone_key(phone) == phone_needle for phone in contact.phone_numbers
        ):
            matches.append(contact)
    return matches