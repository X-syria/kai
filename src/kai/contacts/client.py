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
    """A Google API request failed. Carries the HTTP status, when there was one.

    ``status_code`` is what lets a caller tell a rejection from a server-side
    failure **without parsing the message**. That distinction decides whether a
    write can be reported as definitely-not-applied, so it has to be data rather
    than prose: a 4xx is the provider saying no, while a 5xx says nothing about
    whether the operation was committed before the error came back. ``None``
    means no response was ever received, which is a third case again and not a
    missing detail.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code

    @property
    def is_server_failure(self) -> bool:
        """Did the provider answer, but with an error that does not settle it?

        True only for a status that exists — a 5xx. A 4xx is excluded because
        it is a rejection, and ``None`` is excluded because there was no answer
        to classify.
        """
        return self.status_code is not None and self.status_code >= 500


class _TypedStatusError(GoogleApiError):
    """A named status whose class already says what the status was."""


    """Raised when Google's People API rejects or fails a request."""


class GoogleInvalidTokenError(_TypedStatusError):
    """HTTP 401: the stored access token is invalid or expired."""


class GoogleForbiddenError(_TypedStatusError):
    """HTTP 403: scope is missing or consent was revoked."""


class GoogleRateLimitError(_TypedStatusError):
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
            "google access token is invalid or expired (consent may be revoked)",
            status_code=resp.status_code,
        )
    if resp.status_code == 403:
        raise GoogleForbiddenError(
            "google denied the request; contacts scope may be missing",
            status_code=resp.status_code,
        )
    if resp.status_code == 429:
        raise GoogleRateLimitError(
            "google api rate limit exceeded, try again later",
            status_code=resp.status_code,
        )
    if resp.status_code >= 400:
        raise GoogleApiError(
            f"google people api request failed ({resp.status_code})",
            status_code=resp.status_code,
        )


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