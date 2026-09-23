"""Domain shapes and defensive parsing for Google Contacts data.

Google's People API payloads are external, untrusted input: every field is
typed defensively and only what the current vertical slice needs is kept.
Contacts are never persisted — they are read per request and discarded.
"""

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class Contact:
    """A single normalized contact. Fields come straight from Google input."""

    resource_name: str
    display_name: str
    emails: tuple[str, ...]
    phone_numbers: tuple[str, ...]


@dataclass(frozen=True)
class ContactsPage:
    """One page of People API connections plus pagination state."""

    contacts: tuple[Contact, ...]
    has_more: bool
    next_page_token: str | None


def _string_values(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    values: list[str] = []
    for entry in value:
        if isinstance(entry, dict) and isinstance(entry.get("value"), str):
            text = entry["value"].strip()
            if text:
                values.append(text)
    return tuple(values)


def _display_name(person: Mapping[str, object]) -> str:
    names = person.get("names")
    if not isinstance(names, list):
        return ""
    for entry in names:
        if not isinstance(entry, dict):
            continue
        display_name = entry.get("displayName")
        if isinstance(display_name, str) and display_name.strip():
            return display_name.strip()
    for entry in names:
        if not isinstance(entry, dict):
            continue
        given = entry.get("givenName")
        family = entry.get("familyName")
        if isinstance(given, str) and given.strip():
            parts = [p for p in (given, family) if isinstance(p, str) and p.strip()]
            return " ".join(parts).strip()
    return ""


def parse_connections_payload(payload: object) -> ContactsPage:
    """Turn an untrusted People API payload into :class:`ContactsPage`.

    Malformed entries are skipped, never ignored wholesale: a single bad
    person must not break the whole read. The parser never raises on hostile
    shapes — it degrades to fewer contacts or an empty page.
    """
    if not isinstance(payload, dict):
        return ContactsPage(contacts=(), has_more=False, next_page_token=None)

    raw_connections = payload.get("connections")
    connections = raw_connections if isinstance(raw_connections, list) else []

    contacts: list[Contact] = []
    for entry in connections:
        if not isinstance(entry, dict):
            continue
        resource_name = entry.get("resourceName")
        if not isinstance(resource_name, str) or not resource_name.strip():
            continue
        display_name = _display_name(entry)
        emails = _string_values(entry.get("emailAddresses"))
        phones = _string_values(entry.get("phoneNumbers"))
        if not display_name and not emails and not phones:
            continue
        contacts.append(
            Contact(
                resource_name=resource_name.strip(),
                display_name=display_name,
                emails=emails,
                phone_numbers=phones,
            )
        )

    next_token = payload.get("nextPageToken")
    next_page_token: str | None = None
    if isinstance(next_token, str) and next_token.strip():
        has_more = True
        next_page_token = next_token.strip()
    else:
        has_more = False
    return ContactsPage(
        contacts=tuple(contacts),
        has_more=has_more,
        next_page_token=next_page_token,
    )