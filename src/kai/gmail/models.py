"""Domain shapes and defensive parsing for Gmail API data.

Gmail payloads are T2 untrusted external data — values, not instructions.
Headers, snippets and bodies are parsed defensively and always surfaced as
data: nothing here can change policy, permissions, or trigger actions. Bodies
are decoded and length-capped; malformed parts degrade to empties, never to
exceptions that break a whole read.
"""

import base64
import binascii
import re
from collections.abc import Mapping
from dataclasses import dataclass

MAX_BODY_CHARS = 50_000
MAX_SUBJECT_CHARS = 255

_TAG_STRIP = re.compile(r"<[^>]*>")
_MULTILINE_WS = re.compile(r"[ \t\r\f\v]+")
_ADDRESS_TOKEN = re.compile(
    r"(?P<name>[^<>]*)\s*<\s*(?P<email>[^<>@\s]+@[^>]+)\s*>"
    r"|(?P<bare>[^<>,\s]+@[^<>,\s]+)"
)


@dataclass(frozen=True)
class MessageRef:
    """A single search hit: enough to read or follow a thread."""

    message_id: str
    thread_id: str


@dataclass(frozen=True)
class SearchPage:
    """One page of search results plus pagination state."""

    refs: tuple[MessageRef, ...]
    has_more: bool
    next_page_token: str | None


@dataclass(frozen=True)
class EmailAddress:
    """A display name plus a mailbox."""

    name: str
    email: str


@dataclass(frozen=True)
class GmailMessage:
    """A parsed message. ``body_text`` is data, never instructions."""

    message_id: str
    thread_id: str
    subject: str
    from_addr: EmailAddress | None
    to: tuple[EmailAddress, ...]
    date: str
    snippet: str
    body_text: str
    label_ids: tuple[str, ...]


def _decode_data(data: object) -> str:
    """Base64url-decode Gmail ``body.data`` ``str``, tolerating missing padding."""
    if not isinstance(data, str) or not data:
        return ""
    padded = data.replace("-", "+").replace("_", "/")
    padded += "=" * (-len(padded) % 4)
    try:
        raw = base64.b64decode(padded)
    except (ValueError, binascii.Error):
        return ""
    for encoding in ("utf-8", "latin-1"):
        try:
            decoded = raw.decode(encoding)
            return _MULTILINE_WS.sub(" ", decoded).strip()
        except UnicodeDecodeError:
            continue
    return ""


def _part_body_data(node: Mapping[str, object]) -> object:
    body = node.get("body")
    if not isinstance(body, dict):
        return None
    return body.get("data")


def _collect_body_text(node: Mapping[str, object]) -> str:
    """Find the best readable text: plain parts, else stripped HTML."""
    mime_type = node.get("mimeType")
    parts = node.get("parts")
    if isinstance(parts, list):
        for part in parts:
            if isinstance(part, dict):
                text = _collect_body_text(part)
                if text:
                    return text
    if isinstance(mime_type, str) and "text/plain" in mime_type:
        return _decode_data(_part_body_data(node))
    return ""


def _collect_body(node: Mapping[str, object]) -> str:
    """Body text with preference for plain text, HTML tags stripped as fallback."""
    mime_type = node.get("mimeType")
    body = _collect_body_text(node)
    if isinstance(mime_type, str) and "text/html" in mime_type and not body:
        raw = _decode_data(_part_body_data(node))
        body = _MULTILINE_WS.sub(" ", _TAG_STRIP.sub(" ", raw)).strip() if raw else ""
    return body[:MAX_BODY_CHARS]


def _headers(node: Mapping[str, object]) -> dict[str, str]:
    result: dict[str, str] = {}
    headers = node.get("headers")
    if not isinstance(headers, list):
        return result
    for header in headers:
        if not isinstance(header, dict):
            continue
        name = header.get("name")
        value = header.get("value")
        if isinstance(name, str) and isinstance(value, str):
            result.setdefault(name.lower(), value)
    return result


def _parse_addresses(value: str) -> tuple[EmailAddress, ...]:
    addresses: list[EmailAddress] = []
    for match in _ADDRESS_TOKEN.finditer(value):
        email = (match.group("email") or match.group("bare") or "").strip()
        if not email:
            continue
        name = (match.group("name") or "").strip(" \"'")
        addresses.append(EmailAddress(name=name, email=email))
    return tuple(addresses)


def parse_message_payload(payload: object) -> GmailMessage | None:
    """Turn one untrusted Gmail message resource into :class:`GmailMessage`.

    Only ``id`` is required; the ``payload`` section, headers and bodies are
    all optional and degrade to empty/None when absent or malformed.
    """
    if not isinstance(payload, dict):
        return None
    message_id = payload.get("id")
    if not isinstance(message_id, str) or not message_id.strip():
        return None

    thread_id = payload.get("threadId")
    thread_id = thread_id.strip() if isinstance(thread_id, str) else ""
    snippet = payload.get("snippet")
    snippet = snippet if isinstance(snippet, str) else ""

    label_ids: tuple[str, ...] = ()
    raw_labels = payload.get("labelIds")
    if isinstance(raw_labels, list):
        label_ids = tuple(label for label in raw_labels if isinstance(label, str))

    inner = payload.get("payload")
    if not isinstance(inner, dict):
        inner = {}
    header_values = _headers(inner)

    subject = header_values.get("subject", "")
    subject = subject[:MAX_SUBJECT_CHARS].strip()
    date = header_values.get("date", "")

    def _first_address(key: str) -> EmailAddress | None:
        addresses = _parse_addresses(header_values.get(key, ""))
        return addresses[0] if addresses else None

    return GmailMessage(
        message_id=message_id.strip(),
        thread_id=thread_id,
        subject=subject,
        from_addr=_first_address("from"),
        to=_parse_addresses(header_values.get("to", "")),
        date=date,
        snippet=snippet,
        body_text=_collect_body(inner),
        label_ids=label_ids,
    )


def parse_search_payload(payload: object) -> SearchPage:
    """Turn an untrusted messages.list payload into :class:`SearchPage`."""
    if not isinstance(payload, dict):
        return SearchPage(refs=(), has_more=False, next_page_token=None)
    raw_messages = payload.get("messages")
    messages = raw_messages if isinstance(raw_messages, list) else []
    refs: list[MessageRef] = []
    for entry in messages:
        if not isinstance(entry, dict):
            continue
        message_id = entry.get("id")
        thread_id = entry.get("threadId")
        if not isinstance(message_id, str) or not message_id.strip():
            continue
        refs.append(
            MessageRef(
                message_id=message_id.strip(),
                thread_id=thread_id.strip() if isinstance(thread_id, str) else "",
            )
        )
    next_token = payload.get("nextPageToken")
    next_page_token: str | None = None
    if isinstance(next_token, str) and next_token.strip():
        has_more = True
        next_page_token = next_token.strip()
    else:
        has_more = False
    return SearchPage(refs=tuple(refs), has_more=has_more, next_page_token=next_page_token)