"""Google OAuth integration tests.

Only Google's network is isolated: the app talks to PostgreSQL and the real
crypto layer just like in production, while every ``oauth2.googleapis.com`` /
``www.googleapis.com`` call is served by ``httpx.MockTransport`` injected
through the app dependency override.
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Final
from urllib.parse import parse_qs, parse_qsl, urlparse
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select, text

from kai.api.google import (
    OAUTH_BINDING_COOKIE,
    PENDING_COOKIE,
    _expired_binding_cookie,
)
from kai.config import get_settings
from kai.db.engine import async_session_factory, engine
from kai.db.models import GoogleAccount, Tenant, User
from kai.google import client as google_client
from kai.google.client import GoogleOAuthError, build_authorization_url
from kai.google.crypto import decrypt_credentials
from kai.google.deps import get_google_http_client
from kai.google.pkce import code_challenge
from kai.google.service import (
    GoogleAccountConflictError,
    bind_or_reconnect_google_account,
    refresh_google_account,
)
from kai.google.state import STATE_TTL_SECONDS, _sign, create_state, hash_binding
from kai.main import app

API_PREFIX: Final = "/api/v1"

IDENTITY_SCOPES: Final = ["openid", "email"]
CONTACTS_SCOPE: Final = "https://www.googleapis.com/auth/contacts.readonly"
CALENDAR_SCOPE: Final = "https://www.googleapis.com/auth/calendar.events"
GMAIL_READ_SCOPE: Final = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_SEND_SCOPE: Final = "https://www.googleapis.com/auth/gmail.send"
DRIVE_SCOPE: Final = "https://www.googleapis.com/auth/drive"
CONNECT_GRANTED: Final = " ".join(
    [*IDENTITY_SCOPES, CONTACTS_SCOPE, CALENDAR_SCOPE]
)
GMAIL_GRANTED: Final = " ".join([CONNECT_GRANTED, GMAIL_READ_SCOPE, GMAIL_SEND_SCOPE])


class GoogleStub:
    """Deterministic stand-in for the Google transport."""

    def __init__(self) -> None:
        self.access_token = "test-access-token"
        self.refresh_token = "test-refresh-token"
        self.refresh_access_token = "refreshed-access-token"
        self.sub = "google-sub-123"
        self.email = "alice@example.com"
        self.email_verified = True
        self.token_status = 200
        self.token_error = "invalid_request"
        self.userinfo_status = 200
        self.revoke_status = 200
        self.omit_refresh_on_exchange = False
        self.token_scope: str | None = None
        self.enforce_pkce = False
        self.expected_challenge: str | None = None
        self.token_calls: list[httpx.Request] = []
        self.userinfo_calls: list[httpx.Request] = []
        self.revoke_calls: list[httpx.Request] = []

    def _token_payload(self, form: dict[str, str]) -> dict:
        if form.get("grant_type") == "refresh_token":
            payload = {"access_token": self.refresh_access_token, "expires_in": 3599}
        else:
            payload = {"access_token": self.access_token, "expires_in": 3599}
            if not self.omit_refresh_on_exchange:
                payload["refresh_token"] = self.refresh_token
        if self.token_scope is not None:
            payload["scope"] = self.token_scope
        return payload

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token" and request.url.host == "oauth2.googleapis.com":
            self.token_calls.append(request)
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": self.token_error})
            form = dict(parse_qsl(request.content.decode("utf-8")))
            if (
                self.enforce_pkce
                and form.get("grant_type") == "authorization_code"
                and (
                    not form.get("code_verifier")
                    or self.expected_challenge is None
                    or code_challenge(form["code_verifier"])
                    != self.expected_challenge
                )
            ):
                # A real provider rejects a verifier that does not hash to the
                # challenge it recorded when the code was issued.
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json=self._token_payload(form))
        if request.url.path == "/revoke" and request.url.host == "oauth2.googleapis.com":
            self.revoke_calls.append(request)
            return httpx.Response(self.revoke_status)
        if (
            request.url.path == "/oauth2/v3/userinfo"
            and request.url.host == "www.googleapis.com"
        ):
            self.userinfo_calls.append(request)
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
        return httpx.Response(404)


@pytest.fixture
def stub() -> GoogleStub:
    return GoogleStub()


@pytest_asyncio.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    """This module's ASGI client, served from a *secure* origin.

    It shadows ``conftest.client`` for this file only; the conftest fixture is
    left untouched. The browser-binding cookie is ``Secure`` and carries the
    ``__Host-`` prefix, so a cookie jar only stores it - and only sends it back
    - when the request origin is ``https``. Keeping the same ASGI app and only
    changing the scheme makes the whole lifecycle testable: set on
    ``/connect``, presented on ``/callback``, cleared on terminal paths.
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://testserver"
    ) as c:
        yield c


@pytest_asyncio.fixture
async def google_transport(stub: GoogleStub) -> AsyncIterator[httpx.AsyncClient]:
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


async def _connect(
    client: httpx.AsyncClient, token: str, *, include_gmail: bool = False
) -> str:
    resp = await client.get(
        f"{API_PREFIX}/auth/google/connect",
        headers={"Authorization": f"Bearer {token}"},
        params={"include_gmail": "true"} if include_gmail else {},
    )
    assert resp.status_code == 200
    return resp.json()["authorization_url"]


def _state_from(authorization_url: str) -> str:
    return parse_qs(urlparse(authorization_url).query)["state"][0]


def _payload_of(state: str) -> str:
    """Decode the (signed, unencrypted) payload of a state token."""
    body = state.split(".", 1)[0]
    return base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode("utf-8")


def _binding_cookie_value(client: httpx.AsyncClient) -> str:
    value = client.cookies.get(OAUTH_BINDING_COOKIE)
    assert value is not None, "the browser-binding cookie was not set"
    return value


def _set_binding_cookie(client: httpx.AsyncClient, value: str) -> None:
    client.cookies.delete(OAUTH_BINDING_COOKIE)
    client.cookies.set(OAUTH_BINDING_COOKIE, value, path="/")


def _clear_binding_cookie(client: httpx.AsyncClient) -> None:
    client.cookies.delete(OAUTH_BINDING_COOKIE)


async def _google_account_count() -> int:
    async with engine.begin() as conn:
        return (
            await conn.execute(text("SELECT count(*) FROM google_accounts"))
        ).scalar_one()


async def _callback(
    client: httpx.AsyncClient, *, state: str, code: str = "auth-code-123"
) -> httpx.Response:
    return await client.get(
        f"{API_PREFIX}/auth/google/callback", params={"state": state, "code": code}
    )


def _pending_cookie_value(client: httpx.AsyncClient) -> str:
    value = client.cookies.get(PENDING_COOKIE)
    assert value is not None, "the pending-confirmation cookie was not set"
    return value


async def _confirm(client: httpx.AsyncClient, token: str) -> httpx.Response:
    return await client.post(
        f"{API_PREFIX}/auth/google/pending/confirm",
        headers={"Authorization": f"Bearer {token}"},
    )


async def _bind(
    client: httpx.AsyncClient, token: str, *, include_gmail: bool = False
) -> httpx.Response:
    """Run both halves of the link: callback creates pending, confirm links."""
    url = await _connect(client, token, include_gmail=include_gmail)
    started = await _callback(client, state=_state_from(url))
    assert started.status_code == 303, started.text
    assert started.headers["location"].endswith("/auth/google/confirm")
    _pending_cookie_value(client)
    return await _confirm(client, token)


async def _accounts(client: httpx.AsyncClient, token: str) -> list[dict]:
    resp = await client.get(
        f"{API_PREFIX}/auth/google/accounts",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    return resp.json()["accounts"]


async def _stored_scopes(client: httpx.AsyncClient, token: str) -> list[str]:
    accounts = await _accounts(client, token)
    assert len(accounts) == 1
    return str(accounts[0]["scopes"]).split()


async def _make_user_and_tenant() -> tuple[Tenant, User]:
    async with async_session_factory() as session:
        tenant = Tenant(name=f"svc-{datetime.now(UTC).timestamp()}")
        session.add(tenant)
        await session.flush()
        user = User(
            tenant_id=tenant.id,
            email=f"svc-{uuid4()}@example.com",
            password_hash="hash",
        )
        session.add(user)
        await session.commit()
        await session.refresh(tenant)
        await session.refresh(user)
        return tenant, user


# --- authorization URL ----------------------------------------------------

async def test_connect_requires_authentication(
    client: httpx.AsyncClient, google_settings: None, google_transport: httpx.AsyncClient
) -> None:
    resp = await client.get(f"{API_PREFIX}/auth/google/connect")
    assert resp.status_code == 401


async def test_connect_requests_least_privilege_scope_set(
    client: httpx.AsyncClient, google_settings: None, google_transport: httpx.AsyncClient
) -> None:
    token = await _register(client)
    url = await _connect(client, token)
    query = parse_qs(urlparse(url).query)
    assert query["scope"] == [
        "openid email "
        "https://www.googleapis.com/auth/contacts.readonly "
        "https://www.googleapis.com/auth/calendar.events"
    ]
    assert query["response_type"] == ["code"]
    assert query["access_type"] == ["offline"]
    assert query["client_id"] == ["test-client-id"]
    assert len(_state_from(url)) > 20


async def test_connect_sends_an_s256_pkce_challenge(
    client: httpx.AsyncClient, google_settings: None, google_transport: httpx.AsyncClient
) -> None:
    """The authorization request carries S256, never the verifier itself."""
    token = await _register(client)
    url = await _connect(client, token)
    query = parse_qs(urlparse(url).query)

    assert query["code_challenge_method"] == ["S256"]
    challenge = query["code_challenge"][0]
    assert len(challenge) == 43
    assert "=" not in challenge

    secret = _binding_cookie_value(client)
    assert challenge == code_challenge(secret)
    assert secret not in url


async def test_callback_sends_the_binding_secret_as_code_verifier(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: GoogleStub,
) -> None:
    token = await _register(client)
    state = _state_from(await _connect(client, token))
    secret = _binding_cookie_value(client)

    resp = await _callback(client, state=state)
    assert resp.status_code == 303

    form = dict(parse_qsl(stub.token_calls[0].content.decode("utf-8")))
    assert form["grant_type"] == "authorization_code"
    assert form["code_verifier"] == secret


async def test_code_bound_to_a_different_pkce_challenge_is_rejected(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: GoogleStub,
) -> None:
    """A code issued for another challenge cannot be exchanged here.

    The provider stub enforces PKCE: it hashes the received ``code_verifier``
    and compares it with the challenge the authorization request carried. A
    mismatch is the provider's ``invalid_grant``.
    """
    token = await _register(client)
    url = await _connect(client, token)
    presented = parse_qs(urlparse(url).query)["code_challenge"][0]

    stub.enforce_pkce = True
    stub.expected_challenge = code_challenge("z" * 43)
    assert stub.expected_challenge != presented

    resp = await _callback(client, state=_state_from(url))

    assert resp.status_code == 401
    assert stub.token_calls != []
    assert await _google_account_count() == 0


async def test_code_bound_to_the_presented_challenge_is_accepted(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: GoogleStub,
) -> None:
    token = await _register(client)
    url = await _connect(client, token)
    presented = parse_qs(urlparse(url).query)["code_challenge"][0]

    stub.enforce_pkce = True
    stub.expected_challenge = presented

    resp = await _callback(client, state=_state_from(url))

    assert resp.status_code == 303
    confirmed = await _confirm(client, token)
    assert confirmed.status_code == 200
    assert confirmed.json()["connected"] is True


async def test_exchange_failure_does_not_echo_the_verifier(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: GoogleStub,
) -> None:
    token = await _register(client)
    state = _state_from(await _connect(client, token))
    secret = _binding_cookie_value(client)
    stub.token_status = 400

    resp = await _callback(client, state=state)

    assert resp.status_code == 401
    assert secret not in resp.text
    assert secret not in str(resp.json())


# --- state validation on callback -----------------------------------------

async def test_callback_rejects_forged_state(
    client: httpx.AsyncClient, google_settings: None, google_transport: httpx.AsyncClient
) -> None:
    await _register(client)
    resp = await _callback(client, state="forged-state-value")
    assert resp.status_code == 400
    assert "state" in resp.json()["detail"]


async def test_callback_missing_state_is_400(
    client: httpx.AsyncClient, google_settings: None, google_transport: httpx.AsyncClient
) -> None:
    await _register(client)
    resp = await client.get(f"{API_PREFIX}/auth/google/callback", params={"code": "abc"})
    assert resp.status_code == 400


async def test_callback_denied_authorization_is_400(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: GoogleStub,
) -> None:
    """Google's error redirect ends the transaction only after binding proof.

    The provider-supplied ``error`` string is never reflected and never starts a
    pending transaction; the verified binding cookie is retired.
    """
    token = await _register(client)
    state = _state_from(await _connect(client, token))

    resp = await client.get(
        f"{API_PREFIX}/auth/google/callback",
        params={"state": state, "error": "access_denied"},
    )

    assert resp.status_code == 400
    assert "access_denied" not in resp.json()["detail"]
    assert client.cookies.get(OAUTH_BINDING_COOKIE) is None
    assert client.cookies.get(PENDING_COOKIE) is None
    assert stub.token_calls == []
    assert await _google_account_count() == 0


async def test_callback_error_before_binding_check_does_not_clear_the_cookie(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: GoogleStub,
) -> None:
    """``error=access_denied`` must not be processed before state/binding.

    A forwarded link carrying an error, opened in a browser that still holds a
    legitimate transaction, must not cancel it.
    """
    token = await _register(client)
    _state_from(await _connect(client, token))
    legitimate = _binding_cookie_value(client)

    resp = await client.get(
        f"{API_PREFIX}/auth/google/callback",
        params={"state": "forged-state", "error": "access_denied"},
    )

    assert resp.status_code == 400
    assert "state" in resp.json()["detail"]
    assert client.cookies.get(OAUTH_BINDING_COOKIE) == legitimate
    assert stub.token_calls == []
    assert await _google_account_count() == 0


# --- browser binding (cookie + digest in state) ----------------------------

async def test_callback_without_binding_cookie_is_rejected_before_exchange(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: GoogleStub,
) -> None:
    """A browser that never called ``/connect`` cannot complete the flow.

    The state is genuine and unexpired; only the cookie is missing. Nothing may
    reach Google and nothing may be bound.
    """
    token = await _register(client)
    state = _state_from(await _connect(client, token))
    _clear_binding_cookie(client)

    resp = await _callback(client, state=state)

    assert resp.status_code == 400
    assert "state" in resp.json()["detail"]
    assert stub.token_calls == []
    assert stub.userinfo_calls == []
    assert await _google_account_count() == 0


async def test_callback_with_wrong_binding_cookie_is_rejected_before_exchange(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: GoogleStub,
) -> None:
    token = await _register(client)
    state = _state_from(await _connect(client, token))
    _set_binding_cookie(client, "not-the-secret-that-was-minted")

    resp = await _callback(client, state=state)

    assert resp.status_code == 400
    assert stub.token_calls == []
    assert await _google_account_count() == 0


async def test_wrong_binding_cookie_does_not_cancel_the_legitimate_transaction(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: GoogleStub,
) -> None:
    """A failed binding check must not delete the cookie it was presented.

    Otherwise an attacker who gets the callback URL opened once in the victim's
    browser - with any cookie value, or none - could cancel the legitimate
    transaction. The genuine browser completes afterwards.
    """
    token = await _register(client)
    state = _state_from(await _connect(client, token))
    legitimate = _binding_cookie_value(client)

    _set_binding_cookie(client, "not-the-secret-that-was-minted")
    assert (await _callback(client, state=state)).status_code == 400
    assert client.cookies.get(OAUTH_BINDING_COOKIE) == "not-the-secret-that-was-minted"
    assert stub.token_calls == []

    _set_binding_cookie(client, legitimate)
    resp = await _callback(client, state=state)
    assert resp.status_code == 303
    assert (await _confirm(client, token)).status_code == 200
    assert stub.token_calls != []


async def test_successful_callback_deletes_the_binding_cookie(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: GoogleStub,
) -> None:
    token = await _register(client)
    state = _state_from(await _connect(client, token))
    assert _binding_cookie_value(client) != ""

    resp = await _callback(client, state=state)

    assert resp.status_code == 303
    assert client.cookies.get(OAUTH_BINDING_COOKIE) is None
    # The second half of the flow starts with its own, differently scoped
    # secret; no Google identity is linked until it is confirmed.
    assert client.cookies.get(PENDING_COOKIE) is not None
    assert await _google_account_count() == 0


@pytest.mark.no_db_unit
def test_expired_binding_cookie_header_is_hardened() -> None:
    """The failure path can only clear the cookie if the expiry header is valid.

    ``__Host-`` cookies are dropped by browsers unless the ``Set-Cookie`` that
    deletes them is itself ``Secure`` with ``Path=/`` and no ``Domain``. The
    callback builds that header through ``_expired_binding_cookie`` because it
    cannot use the injected response on an exception path.
    """
    header = _expired_binding_cookie()

    assert header.startswith(f"{OAUTH_BINDING_COOKIE}=")
    assert "Max-Age=0" in header
    assert "Path=/" in header
    assert "Secure" in header
    assert "HttpOnly" in header
    assert "SameSite=lax" in header
    assert "Domain=" not in header


async def test_terminal_failure_after_binding_clears_the_cookie(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: GoogleStub,
) -> None:
    """A verified transaction retires its secret even when it fails.

    ``state`` is genuine and its cookie matches, but no ``code`` is present.
    The transaction is over, so the cookie must not survive to be replayed; no
    token may be exchanged either.
    """
    token = await _register(client)
    state = _state_from(await _connect(client, token))
    assert _binding_cookie_value(client) != ""

    resp = await client.get(
        f"{API_PREFIX}/auth/google/callback", params={"state": state}
    )

    assert resp.status_code == 400
    assert client.cookies.get(OAUTH_BINDING_COOKIE) is None
    assert stub.token_calls == []
    assert await _google_account_count() == 0


async def test_state_from_another_browser_is_rejected(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: GoogleStub,
) -> None:
    """The exact replay the binding exists to stop.

    User A starts the flow, then user B's ``/connect`` overwrites the cookie in
    this browser (the name is fixed), and A's state is presented with B's
    cookie. The digests differ, so nothing is exchanged and nothing is bound.
    """
    token_a = await _register(client, "alice@example.com")
    state_a = _state_from(await _connect(client, token_a))

    token_b = await _register(client, "bob@example.com")
    _state_from(await _connect(client, token_b))

    resp = await _callback(client, state=state_a)

    assert resp.status_code == 400
    assert stub.token_calls == []
    assert await _google_account_count() == 0


async def test_callback_rejects_four_field_state_without_browser_binding(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: GoogleStub,
) -> None:
    """The pre-binding (identity+scopes) payload stays refused even when it is
    correctly signed: it names no browser, so it could be replayed."""
    tenant, user = await _make_user_and_tenant()
    exp = int((datetime.now(UTC) + timedelta(seconds=600)).timestamp())
    payload = f"{user.id}:{tenant.id}:{exp}:openid email"
    body = (
        base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")
    )
    state = f"{body}.{_sign(payload)}"

    resp = await _callback(client, state=state)

    assert resp.status_code == 400
    assert "state" in resp.json()["detail"]
    assert stub.token_calls == []
    assert await _google_account_count() == 0


async def test_expired_state_with_matching_cookie_is_rejected(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: GoogleStub,
) -> None:
    """A stale transaction cannot be revived by a matching cookie."""
    token = await _register(client)
    tenant, user = await _make_user_and_tenant()
    await _connect(client, token)
    secret = _binding_cookie_value(client)

    state = create_state(
        user_id=user.id,
        tenant_id=tenant.id,
        scopes=list(IDENTITY_SCOPES),
        binding_hash=hash_binding(secret),
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    resp = await _callback(client, state=state)

    assert resp.status_code == 400
    assert "state" in resp.json()["detail"]
    assert stub.token_calls == []
    assert await _google_account_count() == 0


async def test_connect_sets_a_hardened_binding_cookie(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
) -> None:
    token = await _register(client)
    resp = await client.get(
        f"{API_PREFIX}/auth/google/connect",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200

    set_cookies = resp.headers.get_list("set-cookie")
    assert len(set_cookies) == 1
    header = set_cookies[0]

    assert header.startswith(f"{OAUTH_BINDING_COOKIE}=")
    name, _, rest = header.partition("=")
    assert name == OAUTH_BINDING_COOKIE
    value = rest.split(";", 1)[0]
    assert value != ""
    assert "HttpOnly" in header
    assert "Secure" in header
    assert "SameSite=lax" in header
    assert "Path=/" in header
    assert f"Max-Age={STATE_TTL_SECONDS}" in header
    # ``__Host-`` requires no Domain attribute at all.
    assert "Domain=" not in header
    assert STATE_TTL_SECONDS == 600


async def test_raw_binding_secret_is_never_exposed(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
) -> None:
    """Only the digest may travel: not in the URL, not in the signed payload."""
    token = await _register(client)
    url = await _connect(client, token)
    secret = _binding_cookie_value(client)
    state = _state_from(url)
    payload = _payload_of(state)

    assert secret not in url
    assert secret not in state
    assert secret not in payload
    assert hash_binding(secret) in payload


# --- account binding ------------------------------------------------------

async def test_connect_callback_binds_account_and_encrypts_credentials(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: GoogleStub,
) -> None:
    token = await _register(client)
    resp = await _bind(client, token)
    assert resp.status_code == 200
    body = resp.json()
    assert body["connected"] is True
    assert body["email"] == stub.email
    assert body["google_sub"] == stub.sub

    accounts = await _accounts(client, token)
    assert len(accounts) == 1
    assert accounts[0]["email"] == stub.email
    assert accounts[0]["google_sub"] == stub.sub

    async with engine.begin() as conn:
        row = (
            await conn.execute(text("SELECT credentials_encrypted FROM google_accounts"))
        ).scalar_one()
    assert stub.access_token not in row
    assert stub.refresh_token not in row
    assert decrypt_credentials(row) == {
        "access_token": stub.access_token,
        "refresh_token": stub.refresh_token,
    }


async def test_callback_exchange_failure_is_401(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: GoogleStub,
) -> None:
    token = await _register(client)
    url = await _connect(client, token)
    stub.token_status = 400
    stub.token_error = "invalid_grant"
    resp = await _callback(client, state=_state_from(url), code="stale-code")
    assert resp.status_code == 401


async def test_callback_userinfo_failure_is_401(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: GoogleStub,
) -> None:
    token = await _register(client)
    url = await _connect(client, token)
    stub.userinfo_status = 401
    resp = await _callback(client, state=_state_from(url))
    assert resp.status_code == 401


async def test_callback_rejects_unverified_email(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: GoogleStub,
) -> None:
    token = await _register(client)
    url = await _connect(client, token)
    stub.email_verified = False
    resp = await _callback(client, state=_state_from(url))
    assert resp.status_code == 400
    assert "verified" in resp.json()["detail"]


# --- duplicate / reconnect ------------------------------------------------

async def test_same_google_identity_reconnects_without_duplicate(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: GoogleStub,
) -> None:
    token = await _register(client)
    stub.email = "alice@example.com"
    assert (await _bind(client, token)).status_code == 200

    stub.email = "alice-renamed@example.com"
    resp = await _bind(client, token)
    assert resp.status_code == 200
    assert resp.json()["email"] == stub.email

    accounts = await _accounts(client, token)
    assert len(accounts) == 1
    assert accounts[0]["email"] == stub.email

    async with engine.begin() as conn:
        count = (
            await conn.execute(text("SELECT count(*) FROM google_accounts"))
        ).scalar_one()
    assert count == 1


async def _stored_credentials() -> dict:
    async with engine.begin() as conn:
        row = (
            await conn.execute(text("SELECT credentials_encrypted FROM google_accounts"))
        ).scalar_one()
    return decrypt_credentials(row)


async def test_reconnect_preserves_refresh_token_when_google_omits_it(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: GoogleStub,
) -> None:
    """A reconnect whose token response omits ``refresh_token`` must not erase
    the stored one: the account would otherwise lose its only long-lived
    credential and every later refresh would fail."""
    token = await _register(client)
    stub.sub = "sub-sec01"
    assert (await _bind(client, token)).status_code == 200
    assert (await _stored_credentials())["refresh_token"] == stub.refresh_token

    stub.access_token = "new-access-token"
    stub.omit_refresh_on_exchange = True
    assert (await _bind(client, token)).status_code == 200

    after = await _stored_credentials()
    assert after["access_token"] == "new-access-token"
    assert after["refresh_token"] == stub.refresh_token


async def test_reconnect_replaces_refresh_token_when_google_issues_one(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: GoogleStub,
) -> None:
    """When Google does issue a new ``refresh_token``, the stored one is
    replaced — the preservation rule must never keep a stale token."""
    token = await _register(client)
    stub.sub = "sub-sec01b"
    assert (await _bind(client, token)).status_code == 200
    assert (await _stored_credentials())["refresh_token"] == stub.refresh_token

    stub.access_token = "new-access-token-2"
    stub.refresh_token = "brand-new-refresh-token"
    assert (await _bind(client, token)).status_code == 200

    after = await _stored_credentials()
    assert after["access_token"] == "new-access-token-2"
    assert after["refresh_token"] == "brand-new-refresh-token"


async def test_distinct_google_identities_bind_separately(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: GoogleStub,
) -> None:
    token = await _register(client)
    stub.sub = "sub-one"
    assert (await _bind(client, token)).status_code == 200
    stub.sub = "sub-two"
    assert (await _bind(client, token)).status_code == 200

    accounts = await _accounts(client, token)
    assert [account["google_sub"] for account in accounts] == ["sub-one", "sub-two"]


# --- granted scopes persistence -------------------------------------------

async def test_callback_persists_granted_scopes_not_raw_setting(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: GoogleStub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stored value describes what Google granted, not the configuration.

    ``GOOGLE_OAUTH_SCOPES`` is deliberately over-broad here; a raw copy of it
    would record scopes the app never requested and the token never carried.
    """
    monkeypatch.setenv(
        "GOOGLE_OAUTH_SCOPES",
        " ".join(
            [*IDENTITY_SCOPES, GMAIL_READ_SCOPE, GMAIL_SEND_SCOPE, DRIVE_SCOPE]
        ),
    )
    get_settings.cache_clear()
    stub.token_scope = CONNECT_GRANTED

    token = await _register(client)
    resp = await _bind(client, token)
    assert resp.status_code == 200
    assert await _stored_scopes(client, token) == [
        *IDENTITY_SCOPES,
        CONTACTS_SCOPE,
        CALENDAR_SCOPE,
    ]


async def test_callback_stores_the_four_default_scopes(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: GoogleStub,
) -> None:
    stub.token_scope = CONNECT_GRANTED
    token = await _register(client)
    resp = await _bind(client, token)
    assert resp.status_code == 200
    assert await _stored_scopes(client, token) == CONNECT_GRANTED.split()


async def test_scope_absent_from_token_response_stores_requested_scopes(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: GoogleStub,
) -> None:
    """Google omits ``scope`` when the grant exactly matches the request.

    The state's signed requested list is then the authoritative value.
    """
    stub.token_scope = None
    token = await _register(client)
    resp = await _bind(client, token)
    assert resp.status_code == 200
    assert await _stored_scopes(client, token) == [
        *IDENTITY_SCOPES,
        CONTACTS_SCOPE,
        CALENDAR_SCOPE,
    ]


async def test_include_gmail_stores_six_scopes_when_granted(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: GoogleStub,
) -> None:
    stub.token_scope = GMAIL_GRANTED
    token = await _register(client)
    resp = await _bind(client, token, include_gmail=True)
    assert resp.status_code == 200
    assert await _stored_scopes(client, token) == GMAIL_GRANTED.split()


async def test_gmail_scopes_not_granted_are_not_stored(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: GoogleStub,
) -> None:
    stub.token_scope = CONNECT_GRANTED
    token = await _register(client)
    resp = await _bind(client, token, include_gmail=True)
    assert resp.status_code == 200
    stored = await _stored_scopes(client, token)
    assert stored == CONNECT_GRANTED.split()
    assert GMAIL_READ_SCOPE not in stored
    assert GMAIL_SEND_SCOPE not in stored


async def test_missing_core_scope_is_400_and_binds_nothing(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: GoogleStub,
) -> None:
    stub.token_scope = " ".join(IDENTITY_SCOPES)
    token = await _register(client)
    resp = await _callback(client, state=_state_from(await _connect(client, token)))
    assert resp.status_code == 400
    assert "did not grant required permissions" in resp.json()["detail"]

    async with engine.begin() as conn:
        count = (
            await conn.execute(text("SELECT count(*) FROM google_accounts"))
        ).scalar_one()
    assert count == 0


async def test_callback_rejects_state_without_scope_context(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
) -> None:
    """A correctly signed legacy state (no scope field) must fail closed."""
    tenant, user = await _make_user_and_tenant()
    exp = int((datetime.now(UTC) + timedelta(seconds=600)).timestamp())
    payload = f"{user.id}:{tenant.id}:{exp}"
    body = (
        base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")
    )
    state = f"{body}.{_sign(payload)}"

    resp = await _callback(client, state=state)
    assert resp.status_code == 400
    assert "state" in resp.json()["detail"]


async def test_invalid_scope_type_in_token_response_is_401(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: GoogleStub,
) -> None:
    stub.token_scope = 123  # type: ignore[assignment]
    token = await _register(client)
    resp = await _callback(client, state=_state_from(await _connect(client, token)))
    assert resp.status_code == 401


async def test_reconnect_updates_stored_scopes(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: GoogleStub,
) -> None:
    stub.token_scope = CONNECT_GRANTED
    token = await _register(client)
    assert (await _bind(client, token)).status_code == 200
    assert await _stored_scopes(client, token) == CONNECT_GRANTED.split()

    stub.token_scope = " ".join([*CONNECT_GRANTED.split(), GMAIL_READ_SCOPE])
    assert (await _bind(client, token, include_gmail=True)).status_code == 200
    assert await _stored_scopes(client, token) == [
        *IDENTITY_SCOPES,
        CONTACTS_SCOPE,
        CALENDAR_SCOPE,
        GMAIL_READ_SCOPE,
    ]
    assert len(await _accounts(client, token)) == 1


# --- tenant isolation -----------------------------------------------------

async def test_google_accounts_are_tenant_scoped(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: GoogleStub,
) -> None:
    token_a = await _register(client, "alice@example.com")
    token_b = await _register(client, "bob@example.com")
    stub.sub = "sub-alice"
    assert (await _bind(client, token_a)).status_code == 200
    stub.sub = "sub-bob"
    assert (await _bind(client, token_b)).status_code == 200

    assert [a["google_sub"] for a in await _accounts(client, token_a)] == ["sub-alice"]
    assert [a["google_sub"] for a in await _accounts(client, token_b)] == ["sub-bob"]


async def test_same_google_sub_across_tenants_is_allowed(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: GoogleStub,
) -> None:
    token_a = await _register(client, "alice@example.com")
    token_b = await _register(client, "bob@example.com")
    stub.sub = "shared-sub"
    assert (await _bind(client, token_a)).status_code == 200
    assert (await _bind(client, token_b)).status_code == 200

    async with engine.begin() as conn:
        count = (
            await conn.execute(text("SELECT count(*) FROM google_accounts"))
        ).scalar_one()
    assert count == 2


async def test_google_accounts_require_authentication(
    client: httpx.AsyncClient, google_settings: None, google_transport: httpx.AsyncClient
) -> None:
    resp = await client.get(f"{API_PREFIX}/auth/google/accounts")
    assert resp.status_code == 401


# --- binding guard / refresh / revoke (service level) ---------------------

async def test_bind_rejects_account_owned_by_another_user_in_tenant(
    google_settings: None,
) -> None:
    tenant, user_a = await _make_user_and_tenant()
    async with async_session_factory() as session:
        user_b = User(
            tenant_id=tenant.id,
            email=f"other-{uuid4()}@example.com",
            password_hash="hash",
        )
        session.add(user_b)
        await session.commit()
        await session.refresh(user_b)

        account = await bind_or_reconnect_google_account(
            session,
            tenant_id=tenant.id,
            user_id=user_a.id,
            google_sub="shared-sub",
            email="a@example.com",
            scopes="openid email",
            access_token="tok",
            refresh_token="refresh",
            expires_in=3600,
        )
        assert account.user_id == user_a.id

        with pytest.raises(GoogleAccountConflictError):
            await bind_or_reconnect_google_account(
                session,
                tenant_id=tenant.id,
                user_id=user_b.id,
                google_sub="shared-sub",
                email="b@example.com",
                scopes="openid email",
                access_token="tok",
                refresh_token="refresh",
                expires_in=3600,
            )


async def test_refresh_rotates_encrypted_access_token(
    google_settings: None, stub: GoogleStub
) -> None:
    tenant, user = await _make_user_and_tenant()
    async with async_session_factory() as session:
        account = await bind_or_reconnect_google_account(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            google_sub="sub-refresh",
            email="refresh@example.com",
            scopes="openid email",
            access_token="old-access-token",
            refresh_token=stub.refresh_token,
            expires_in=30,
        )
        account_id = account.id
        before_expiry = account.token_expires_at

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(stub.handler)) as transport,
        async_session_factory() as session,
    ):
        refreshed = await refresh_google_account(
            session, transport, account_id=account_id, tenant_id=tenant.id
        )
        rotated = decrypt_credentials(refreshed.credentials_encrypted)
        assert rotated["access_token"] == stub.refresh_access_token
        assert rotated["refresh_token"] == stub.refresh_token
        assert refreshed.token_expires_at is not None
        assert refreshed.token_expires_at > before_expiry


async def test_refresh_rejects_revoked_credentials(
    google_settings: None, stub: GoogleStub
) -> None:
    tenant, user = await _make_user_and_tenant()
    async with async_session_factory() as session:
        account = await bind_or_reconnect_google_account(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            google_sub="sub-revoked",
            email="revoked@example.com",
            scopes="openid email",
            access_token="old-access-token",
            refresh_token=stub.refresh_token,
            expires_in=30,
        )
        account_id = account.id
        before_expiry = account.token_expires_at

    stub.token_status = 400
    stub.token_error = "invalid_grant"
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(stub.handler)) as transport,
        async_session_factory() as session,
    ):
        with pytest.raises(GoogleOAuthError):
            await refresh_google_account(
                session, transport, account_id=account_id, tenant_id=tenant.id
            )
        untouched = (
            await session.execute(
                select(GoogleAccount).where(GoogleAccount.id == account_id)
            )
        ).scalar_one()
        assert decrypt_credentials(untouched.credentials_encrypted)[
            "access_token"
        ] == "old-access-token"
        assert untouched.token_expires_at == before_expiry


async def test_refresh_without_refresh_token_is_rejected(
    google_settings: None, stub: GoogleStub
) -> None:
    tenant, user = await _make_user_and_tenant()
    async with async_session_factory() as session:
        account = await bind_or_reconnect_google_account(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            google_sub="sub-no-refresh",
            email="norefresh@example.com",
            scopes="openid email",
            access_token="only-access",
            refresh_token=None,
            expires_in=30,
        )
        account_id = account.id

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(stub.handler)) as transport,
        async_session_factory() as session,
    ):
        with pytest.raises(GoogleOAuthError):
            await refresh_google_account(
                session, transport, account_id=account_id, tenant_id=tenant.id
            )


async def test_refresh_is_tenant_scoped(google_settings: None, stub: GoogleStub) -> None:
    tenant_a, user_a = await _make_user_and_tenant()
    tenant_b, _ = await _make_user_and_tenant()
    async with async_session_factory() as session:
        account = await bind_or_reconnect_google_account(
            session,
            tenant_id=tenant_a.id,
            user_id=user_a.id,
            google_sub="sub-cross",
            email="cross@example.com",
            scopes="openid email",
            access_token="tok",
            refresh_token=stub.refresh_token,
            expires_in=30,
        )
        account_id = account.id

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(stub.handler)) as transport,
        async_session_factory() as session,
    ):
        with pytest.raises(GoogleAccountConflictError):
            await refresh_google_account(
                session, transport, account_id=account_id, tenant_id=tenant_b.id
            )


async def test_revoke_contract(google_settings: None, stub: GoogleStub) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(stub.handler)) as transport:
        await google_client.revoke_access_token(transport, access_token="revokable-token")
        assert stub.revoke_calls[0].url.params["token"] == "revokable-token"

        stub.revoke_status = 400
        await google_client.revoke_access_token(transport, access_token="stale-token")
        assert len(stub.revoke_calls) == 2


# --- token response ``scope`` parsing (pure, no database) -------------------
#
# The callback's granted-scope persistence is covered by the database-backed
# tests above. These four pin the wire-level parser directly so the
# "reject an invalid scope field" rule is executable without PostgreSQL.


@pytest.mark.no_db_unit
def test_token_response_valid_scope_is_preserved() -> None:
    parsed = google_client._parse_token_response(
        httpx.Response(200, json={"access_token": "a", "scope": "  openid email  "})
    )

    assert parsed.scope is not None
    assert parsed.scope.split() == ["openid", "email"]


@pytest.mark.no_db_unit
def test_token_response_absent_scope_is_none() -> None:
    parsed = google_client._parse_token_response(
        httpx.Response(200, json={"access_token": "a"})
    )

    assert parsed.scope is None


@pytest.mark.no_db_unit
def test_token_response_non_string_scope_is_rejected() -> None:
    with pytest.raises(GoogleOAuthError):
        google_client._parse_token_response(
            httpx.Response(200, json={"access_token": "a", "scope": 123})
        )


@pytest.mark.no_db_unit
def test_token_response_blank_scope_is_rejected() -> None:
    with pytest.raises(GoogleOAuthError):
        google_client._parse_token_response(
            httpx.Response(200, json={"access_token": "a", "scope": "   "})
        )


# --- PKCE wire contract (pure, no database) --------------------------------


@pytest.mark.no_db_unit
def test_authorization_url_carries_the_s256_challenge(google_settings: None) -> None:
    verifier = "v" * 43
    challenge = code_challenge(verifier)

    url = build_authorization_url(
        scopes=["openid"],
        redirect_uri="https://app.example/callback",
        state="signed-state",
        code_challenge=challenge,
    )

    query = parse_qs(urlparse(url).query)
    assert query["code_challenge"] == [challenge]
    assert query["code_challenge_method"] == ["S256"]
    assert verifier not in url


@pytest.mark.no_db_unit
async def test_exchange_code_sends_the_verifier_only_in_the_body(
    google_settings: None,
) -> None:
    captured: dict[str, str] = {}
    captured_url = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_url
        captured_url = str(request.url)
        captured.update(dict(parse_qsl(request.content.decode("utf-8"))))
        return httpx.Response(200, json={"access_token": "a", "expires_in": 1})

    verifier = "v" * 43
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
        token = await google_client.exchange_code(
            transport,
            code="auth-code",
            redirect_uri="https://app.example/callback",
            code_verifier=verifier,
        )

    assert token.access_token == "a"
    assert captured["grant_type"] == "authorization_code"
    assert captured["code_verifier"] == verifier
    assert verifier not in captured_url


@pytest.mark.no_db_unit
async def test_exchange_code_propagates_a_pkce_mismatch(
    google_settings: None,
) -> None:
    """A provider enforcing PKCE rejects a verifier for another challenge.

    This pins the client side of the contract without a database: the verifier
    really is in the body, and the provider's ``invalid_grant`` is surfaced.
    """
    expected = code_challenge("e" * 43)

    def handler(request: httpx.Request) -> httpx.Response:
        form = dict(parse_qsl(request.content.decode("utf-8")))
        if code_challenge(form["code_verifier"]) != expected:
            return httpx.Response(400, json={"error": "invalid_grant"})
        return httpx.Response(200, json={"access_token": "a", "expires_in": 1})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
        with pytest.raises(GoogleOAuthError, match="invalid_grant"):
            await google_client.exchange_code(
                transport,
                code="auth-code",
                redirect_uri="https://app.example/callback",
                code_verifier="v" * 43,
            )