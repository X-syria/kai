"""Gmail API client for the vertical slice.

Network traffic only flows through an injected ``httpx.AsyncClient`` so tests
can isolate Google's network behind ``httpx.MockTransport`` while the database
and credential decryption stay real. Read operations use the ``gmail.readonly``
grant; send uses the separate ``gmail.send`` grant. Nothing here executes
message content — bodies are parsed as data and returned as data.
"""

from urllib.parse import quote

import httpx

from kai.gmail.models import (
    GmailMessage,
    MessageRef,
    SearchPage,
    parse_message_payload,
    parse_search_payload,
)

MESSAGES_URL = "https://www.googleapis.com/gmail/v1/users/me/messages"
SEND_URL = "https://www.googleapis.com/gmail/v1/users/me/messages/send"
MAX_RESULTS = 50
MAX_PAGES = 20
MAX_MESSAGES = 1_000


class GoogleApiError(RuntimeError):
    """Raised when Google's Gmail API rejects or fails a request."""


class GoogleInvalidTokenError(GoogleApiError):
    """HTTP 401: the stored access token is invalid or expired."""


class GoogleForbiddenError(GoogleApiError):
    """HTTP 403: scope is missing or consent was revoked."""


class GoogleRateLimitError(GoogleApiError):
    """HTTP 429: quota/rate limit exceeded."""


class GoogleNotFoundError(GoogleApiError):
    """HTTP 404: the requested message does not exist."""


def _raise_for_status(resp: httpx.Response) -> None:
    """Map Google's failure modes onto typed errors (never echoes tokens)."""
    if resp.status_code == 401:
        raise GoogleInvalidTokenError(
            "google access token is invalid or expired (consent may be revoked)"
        )
    if resp.status_code == 403:
        raise GoogleForbiddenError(
            "google denied the request; gmail scope may be missing"
        )
    if resp.status_code == 404:
        raise GoogleNotFoundError("gmail message was not found")
    if resp.status_code == 429:
        raise GoogleRateLimitError("google api rate limit exceeded, try again later")
    if resp.status_code >= 400:
        raise GoogleApiError(f"gmail api request failed ({resp.status_code})")


async def search_messages(
    transport: httpx.AsyncClient,
    *,
    access_token: str,
    query: str,
    max_results: int = MAX_RESULTS,
) -> list[MessageRef]:
    """Search the mailbox with a Gmail ``q`` query, following pagination."""
    refs: list[MessageRef] = []
    page_token: str | None = None
    for _ in range(MAX_PAGES):
        page = await _search_page(
            transport,
            access_token=access_token,
            query=query,
            max_results=max_results,
            page_token=page_token,
        )
        refs.extend(page.refs)
        if not page.has_more or len(refs) >= MAX_MESSAGES:
            break
        page_token = page.next_page_token
    return refs[:max_results]


async def _search_page(
    transport: httpx.AsyncClient,
    *,
    access_token: str,
    query: str,
    max_results: int,
    page_token: str | None,
) -> SearchPage:
    params: dict[str, str] = {
        "q": query,
        "maxResults": str(max_results),
        "includeSpamTrash": "false",
    }
    if page_token:
        params["pageToken"] = page_token
    resp = await transport.get(
        MESSAGES_URL,
        params=params,
        headers={"Authorization": f"Bearer {access_token}"},
    )
    _raise_for_status(resp)
    try:
        payload = resp.json()
    except ValueError as exc:
        raise GoogleApiError("gmail search returned a malformed response") from exc
    return parse_search_payload(payload)


async def get_message(
    transport: httpx.AsyncClient, *, access_token: str, message_id: str
) -> GmailMessage:
    """Read one message (headers, snippet, text body) from the mailbox."""
    resp = await transport.get(
        f"{MESSAGES_URL}/{quote(message_id, safe='')}",
        params={"format": "full"},
        headers={"Authorization": f"Bearer {access_token}"},
    )
    _raise_for_status(resp)
    try:
        payload = resp.json()
    except ValueError as exc:
        raise GoogleApiError("gmail read returned a malformed response") from exc
    message = parse_message_payload(payload)
    if message is None:
        raise GoogleApiError("gmail read returned an unparseable message")
    return message


async def send_message(
    transport: httpx.AsyncClient, *, access_token: str, raw: str
) -> GmailMessage:
    """Send a raw RFC5322 message; return the created Gmail message."""
    resp = await transport.post(
        SEND_URL,
        json={"raw": raw},
        headers={"Authorization": f"Bearer {access_token}"},
    )
    _raise_for_status(resp)
    try:
        payload = resp.json()
    except ValueError as exc:
        raise GoogleApiError("gmail send returned a malformed response") from exc
    message = parse_message_payload(payload)
    if message is None or not message.message_id:
        raise GoogleApiError("gmail send returned no accepted message_id")
    return message