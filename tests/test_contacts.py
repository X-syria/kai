"""Google Contacts integration tests.

Only Google's network is isolated: the app talks to PostgreSQL and decrypts
real stored credentials, while ``oauth2.googleapis.com`` /
``www.googleapis.com`` / ``people.googleapis.com`` are served by
``httpx.MockTransport`` through the app dependency override.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Final
from urllib.parse import parse_qs, parse_qsl, urlparse

import httpx
import pytest
import pytest_asyncio

from kai.google.deps import get_google_http_client
from kai.main import app

API_PREFIX: Final = "/api/v1"

CONTACTS_READONLY_SCOPE = "https://www.googleapis.com/auth/contacts.readonly"

VALID_CONNECTION = {
    "resourceName": "people/100",
    "names": [{"displayName": "Alice Smith"}],
    "emailAddresses": [{"value": "alice@example.com"}],
    "phoneNumbers": [{"value": "+1-555-0100"}],
}
BOB_CONNECTION = {
    "resourceName": "people/200",
    "names": [{"givenName": "Bob", "familyName": "Smith"}],
    "emailAddresses": [{"value": "bob@example.com"}],
    "phoneNumbers": [],
}
CAROL_CONNECTION = {
    "resourceName": "people/300",
    "names": [{"displayName": "Carol"}],
    "emailAddresses": [{"value": "carol@example.com"}],
}


class ContactsStub:
    """Deterministic stand-in for Google's OAuth + People transport."""

    def __init__(self) -> None:
        self.access_token = "test-access-token"
        self.refresh_token = "test-refresh-token"
        self.sub = "google-sub-contacts"
        self.email = "alice@example.com"
        self.email_verified = True
        self.token_status = 200
        self.userinfo_status = 200
        self.connections_status = 200
        self.connections_payload: dict = {
            "connections": [VALID_CONNECTION, BOB_CONNECTION, CAROL_CONNECTION]
        }
        self.connections_text: str | None = None
        self.pages: list[dict] | None = None
        self.people_calls: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token" and request.url.host == "oauth2.googleapis.com":
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "invalid_grant"})
            form = dict(parse_qsl(request.content.decode("utf-8")))
            if form.get("grant_type") == "refresh_token":
                return httpx.Response(
                    200,
                    json={"access_token": "refreshed-token", "expires_in": 3599},
                )
            return httpx.Response(
                200,
                json={
                    "access_token": self.access_token,
                    "refresh_token": self.refresh_token,
                    "expires_in": 3599,
                },
            )
        if (
            request.url.path == "/oauth2/v3/userinfo"
            and request.url.host == "www.googleapis.com"
        ):
            if self.userinfo_status != 200:
                return httpx.Response(self.userinfo_status)
            return httpx.Response(
                200,
                json={
                    "sub": self.sub,
                    "email": self.email,
                    "email_verified": self.email_verified,
                },
            )
        if (
            request.url.path == "/v1/people/me/connections"
            and request.url.host == "people.googleapis.com"
        ):
            self.people_calls.append(request)
            if self.connections_status != 200:
                return httpx.Response(self.connections_status)
            if self.connections_text is not None:
                return httpx.Response(200, text=self.connections_text)
            if self.pages is not None:
                idx = min(len(self.people_calls) - 1, len(self.pages) - 1)
                return httpx.Response(200, json=self.pages[idx])
            return httpx.Response(200, json=self.connections_payload)
        return httpx.Response(404)


@pytest.fixture
def stub() -> ContactsStub:
    return ContactsStub()


@pytest_asyncio.fixture
async def google_transport(stub: ContactsStub) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=httpx.MockTransport(stub.handler)) as transport:
        app.dependency_overrides[get_google_http_client] = lambda: transport
        try:
            yield transport
        finally:
            app.dependency_overrides.pop(get_google_http_client, None)


async def _register(client: httpx.AsyncClient, email: str = "alice@example.com") -> str:
    resp = await client.post(
        f"{API_PREFIX}/auth/register",
        json={"email": email, "password": "password123"},
    )
    assert resp.status_code == 201
    return resp.json()["access_token"]


async def _connect(client: httpx.AsyncClient, token: str) -> str:
    resp = await client.get(
        f"{API_PREFIX}/auth/google/connect",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    return resp.json()["authorization_url"]


def _state_from(authorization_url: str) -> str:
    return parse_qs(urlparse(authorization_url).query)["state"][0]


async def _bind(client: httpx.AsyncClient, token: str) -> httpx.Response:
    """Link the stub's Google identity through the pending-confirmation flow."""
    url = await _connect(client, token)
    started = await client.get(
        f"{API_PREFIX}/auth/google/callback",
        params={"state": _state_from(url), "code": "auth-code-123"},
    )
    assert started.status_code == 303
    confirmed = await client.post(
        f"{API_PREFIX}/auth/google/pending/confirm",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert confirmed.status_code == 200
    return confirmed


async def _contacts(
    client: httpx.AsyncClient,
    token: str,
    *,
    account_id: str | None = None,
    q: str | None = None,
) -> httpx.Response:
    params: dict[str, str] = {}
    if account_id is not None:
        params["account_id"] = account_id
    if q is not None:
        params["q"] = q
    return await client.get(
        f"{API_PREFIX}/contacts", params=params, headers={"Authorization": f"Bearer {token}"}
    )


async def _account_id(client: httpx.AsyncClient, token: str) -> str:
    resp = await client.get(
        f"{API_PREFIX}/auth/google/accounts",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    accounts = resp.json()["accounts"]
    assert len(accounts) == 1
    return accounts[0]["account_id"]


# --- least-privilege scope ------------------------------------------------

async def test_connect_requests_readonly_contacts_scope_only(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
) -> None:
    token = await _register(client)
    url = await _connect(client, token)
    scopes = parse_qs(urlparse(url).query)["scope"][0].split()
    assert scopes == [
        "openid",
        "email",
        CONTACTS_READONLY_SCOPE,
        "https://www.googleapis.com/auth/calendar.events",
    ]
    forbidden = (
        "https://www.googleapis.com/auth/contacts",  # read/write variant
        "https://www.googleapis.com/auth/gmail",  # full read/write variant
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/calendar",
        "https://www.googleapis.com/auth/calendar.readonly",
        "https://www.googleapis.com/auth/drive",
    )
    for scope in forbidden:
        assert scope not in scopes


# --- authentication / account binding -------------------------------------

async def test_contacts_require_authentication(
    client: httpx.AsyncClient, google_settings: None, google_transport: httpx.AsyncClient
) -> None:
    resp = await client.get(f"{API_PREFIX}/contacts")
    assert resp.status_code == 401


async def test_contacts_without_connected_account_is_404(
    client: httpx.AsyncClient, google_settings: None, google_transport: httpx.AsyncClient
) -> None:
    token = await _register(client)
    assert (await _contacts(client, token)).status_code == 404


async def test_bound_account_serves_contacts_and_never_leaks_tokens(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: ContactsStub,
) -> None:
    token = await _register(client)
    await _bind(client, token)

    resp = await _contacts(client, token)
    assert resp.status_code == 200
    contacts = resp.json()["contacts"]
    assert len(contacts) == 3
    assert contacts[0]["resource_name"] == "people/100"
    assert contacts[0]["display_name"] == "Alice Smith"
    assert contacts[0]["emails"] == ["alice@example.com"]
    assert contacts[0]["phone_numbers"] == ["+1-555-0100"]

    body_text = str(resp.json())
    assert stub.access_token not in body_text
    assert stub.refresh_token not in body_text
    assert "credentials_encrypted" not in body_text

    assert stub.people_calls[0].headers["Authorization"] == f"Bearer {stub.access_token}"


# --- tenant isolation -----------------------------------------------------

async def test_contacts_account_id_is_tenant_scoped(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: ContactsStub,
) -> None:
    token_a = await _register(client, "alice@example.com")
    token_b = await _register(client, "bob@example.com")
    stub.sub = "sub-a"
    await _bind(client, token_a)
    stub.sub = "sub-b"
    await _bind(client, token_b)

    account_a = await _account_id(client, token_a)
    assert (await _contacts(client, token_a, account_id=account_a)).status_code == 200
    assert (await _contacts(client, token_b, account_id=account_a)).status_code == 404


# --- contact lookup: exact / ambiguous / no-match --------------------------

async def test_lookup_by_exact_email(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: ContactsStub,
) -> None:
    token = await _register(client)
    await _bind(client, token)
    resp = await _contacts(client, token, q="alice@example.com")
    assert resp.status_code == 200
    contacts = resp.json()["contacts"]
    assert len(contacts) == 1
    assert contacts[0]["resource_name"] == "people/100"


async def test_lookup_ambiguous_returns_all_matches(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: ContactsStub,
) -> None:
    token = await _register(client)
    await _bind(client, token)
    resp = await _contacts(client, token, q="Smith")
    assert resp.status_code == 200
    matches = resp.json()["contacts"]
    assert [c["resource_name"] for c in matches] == ["people/100", "people/200"]


async def test_lookup_no_match_is_empty(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: ContactsStub,
) -> None:
    token = await _register(client)
    await _bind(client, token)
    resp = await _contacts(client, token, q="nobody-here")
    assert resp.status_code == 200
    assert resp.json()["contacts"] == []


async def test_lookup_is_case_insensitive(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: ContactsStub,
) -> None:
    token = await _register(client)
    await _bind(client, token)
    resp = await _contacts(client, token, q="CaRoL@example.com")
    assert resp.status_code == 200
    assert [c["resource_name"] for c in resp.json()["contacts"]] == ["people/300"]


async def test_lookup_by_phone_digits(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: ContactsStub,
) -> None:
    token = await _register(client)
    await _bind(client, token)
    resp = await _contacts(client, token, q="15550100")
    assert resp.status_code == 200
    assert [c["resource_name"] for c in resp.json()["contacts"]] == ["people/100"]


# --- malformed external data ----------------------------------------------

async def test_malformed_connections_are_skipped_not_fatal(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: ContactsStub,
) -> None:
    token = await _register(client)
    await _bind(client, token)
    stub.connections_payload = {
        "connections": [
            VALID_CONNECTION,
            {"resourceName": "people/999"},  # empty: skipped
            {"names": [{"displayName": "NoResource"}], "emailAddresses": []},  # no resourceName
            "not-a-dict",
            {"resourceName": "people/888", "names": "boom", "emailAddresses": "boom"},
            None,
        ]
    }
    resp = await _contacts(client, token)
    assert resp.status_code == 200
    assert [c["resource_name"] for c in resp.json()["contacts"]] == ["people/100"]


async def test_connections_not_a_page_is_an_empty_contact_list(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: ContactsStub,
) -> None:
    token = await _register(client)
    await _bind(client, token)
    stub.connections_payload = {"connections": "not-a-list"}
    resp = await _contacts(client, token)
    assert resp.status_code == 200
    assert resp.json()["contacts"] == []


async def test_non_json_google_response_is_502(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: ContactsStub,
) -> None:
    token = await _register(client)
    await _bind(client, token)
    stub.connections_text = "<html>overloaded proxy</html>"
    resp = await _contacts(client, token)
    assert resp.status_code == 502


# --- API errors / rate limit ----------------------------------------------

async def test_rate_limit_is_429(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: ContactsStub,
) -> None:
    token = await _register(client)
    await _bind(client, token)
    stub.connections_status = 429
    resp = await _contacts(client, token)
    assert resp.status_code == 429
    assert "rate" in resp.json()["detail"]


async def test_generic_api_error_is_502(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: ContactsStub,
) -> None:
    token = await _register(client)
    await _bind(client, token)
    stub.connections_status = 500
    resp = await _contacts(client, token)
    assert resp.status_code == 502


async def test_insufficient_scope_is_403(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: ContactsStub,
) -> None:
    token = await _register(client)
    await _bind(client, token)
    stub.connections_status = 403
    resp = await _contacts(client, token)
    assert resp.status_code == 403
    assert "scope" in resp.json()["detail"]


async def test_invalid_token_is_401(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: ContactsStub,
) -> None:
    token = await _register(client)
    await _bind(client, token)
    stub.connections_status = 401
    resp = await _contacts(client, token)
    assert resp.status_code == 401
    assert stub.access_token not in resp.json()["detail"]


# --- pagination -----------------------------------------------------------

async def test_pagination_fetches_every_page(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: ContactsStub,
) -> None:
    token = await _register(client)
    await _bind(client, token)
    stub.pages = [
        {"connections": [VALID_CONNECTION], "nextPageToken": "page-2"},
        {"connections": [CAROL_CONNECTION]},
    ]
    resp = await _contacts(client, token)
    assert resp.status_code == 200
    assert [c["resource_name"] for c in resp.json()["contacts"]] == [
        "people/100",
        "people/300",
    ]
    assert len(stub.people_calls) == 2
    assert stub.people_calls[1].url.params["pageToken"] == "page-2"