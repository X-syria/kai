"""Google People API read-only client for the Contacts vertical slice.

Network traffic only flows through an injected ``httpx.AsyncClient`` so tests
can isolate Google's network behind ``httpx.MockTransport`` while the
database and credential decryption stay real. Requests carry the
least-privilege contacts scope the account was granted at connect time
(``openid email`` + ``contacts.readonly``); no other Google API is touched.
"""

import httpx

from kai.contacts.models import Contact, ContactsPage, parse_connections_payload
from kai.observability import metrics
from kai.observability.providers import provider_call


class GoogleApiError(RuntimeError):
    """Raised when Google's People API rejects or fails a request."""


class GoogleInvalidTokenError(GoogleApiError):
    """HTTP 401: the stored access token is invalid or expired."""


class GoogleForbiddenError(GoogleApiError):
    """HTTP 403: scope is missing or consent was revoked."""


class GoogleRateLimitError(GoogleApiError):
    """HTTP 429: quota/rate limit exceeded."""


CONNECTIONS_URL = "https://people.googleapis.com/v1/people/me/connections"
PERSON_FIELDS = "names,emailAddresses,phoneNumbers"
PAGE_SIZE = 200
MAX_PAGES = 50
MAX_CONTACTS = 10_000


def _raise_for_status(resp: httpx.Response) -> None:
    """Map Google's failure modes onto typed errors (never echoes tokens)."""
    if resp.status_code == 401:
        raise GoogleInvalidTokenError(
            "google access token is invalid or expired (consent may be revoked)"
        )
    if resp.status_code == 403:
        raise GoogleForbiddenError(
            "google denied the request; contacts scope may be missing"
        )
    if resp.status_code == 429:
        raise GoogleRateLimitError("google api rate limit exceeded, try again later")
    if resp.status_code >= 400:
        raise GoogleApiError(f"google people api request failed ({resp.status_code})")


async def fetch_connections_page(
    transport: httpx.AsyncClient, *, access_token: str, page_token: str | None = None
) -> ContactsPage:
    """Fetch a single page of the authenticated user's contacts."""
    params: dict[str, str] = {"personFields": PERSON_FIELDS, "pageSize": str(PAGE_SIZE)}
    if page_token:
        params["pageToken"] = page_token
    with provider_call(provider=metrics.PROVIDER_GOOGLE, operation="list_contacts"):
        resp = await transport.get(
            CONNECTIONS_URL,
            params=params,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        _raise_for_status(resp)
        try:
            payload = resp.json()
        except ValueError as exc:
            raise GoogleApiError(
                "google people api returned a malformed response"
            ) from exc
        return parse_connections_payload(payload)


async def fetch_all_contacts(
    transport: httpx.AsyncClient, *, access_token: str
) -> list[Contact]:
    """Fetch every contact, bounded to avoid unbounded loops on bad input."""
    contacts: list[Contact] = []
    page_token: str | None = None
    for _ in range(MAX_PAGES):
        page = await fetch_connections_page(
            transport, access_token=access_token, page_token=page_token
        )
        contacts.extend(page.contacts)
        if not page.has_more or len(contacts) >= MAX_CONTACTS:
            break
        page_token = page.next_page_token
    return contacts