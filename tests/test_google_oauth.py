"""Google OAuth integration tests.

Only Google's network is isolated: the app talks to PostgreSQL and the real
crypto layer just like in production, while every ``oauth2.googleapis.com`` /
``www.googleapis.com`` call is served by ``httpx.MockTransport`` injected
through the app dependency override.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Final
from urllib.parse import parse_qs, parse_qsl, urlparse
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select, text

from kai.db.engine import async_session_factory, engine
from kai.db.models import GoogleAccount, Tenant, User
from kai.google import client as google_client
from kai.google.client import GoogleOAuthError
from kai.google.crypto import decrypt_credentials, encrypt_credentials
from kai.google.deps import get_google_http_client
from kai.google.service import (
    GoogleAccountConflictError,
    bind_or_reconnect_google_account,
    refresh_google_account,
)
from kai.main import app

API_PREFIX: Final = "/api/v1"


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
        self.token_calls: list[httpx.Request] = []
        self.userinfo_calls: list[httpx.Request] = []
        self.revoke_calls: list[httpx.Request] = []

    def _token_payload(self, form: dict[str, str]) -> dict:
        if form.get("grant_type") == "refresh_token":
            return {"access_token": self.refresh_access_token, "expires_in": 3599}
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_in": 3599,
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token" and request.url.host == "oauth2.googleapis.com":
            self.token_calls.append(request)
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": self.token_error})
            form = dict(parse_qsl(request.content.decode("utf-8")))
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


async def _connect(client: httpx.AsyncClient, token: str) -> str:
    resp = await client.get(
        f"{API_PREFIX}/auth/google/connect",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    return resp.json()["authorization_url"]


def _state_from(authorization_url: str) -> str:
    return parse_qs(urlparse(authorization_url).query)["state"][0]


async def _callback(
    client: httpx.AsyncClient, *, state: str, code: str = "auth-code-123"
) -> httpx.Response:
    return await client.get(
        f"{API_PREFIX}/auth/google/callback", params={"state": state, "code": code}
    )


async def _accounts(client: httpx.AsyncClient, token: str) -> list[dict]:
    resp = await client.get(
        f"{API_PREFIX}/auth/google/accounts",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    return resp.json()["accounts"]


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
        "https://www.googleapis.com/auth/calendar.events "
        "https://www.googleapis.com/auth/gmail.readonly "
        "https://www.googleapis.com/auth/gmail.send"
    ]
    assert query["response_type"] == ["code"]
    assert query["access_type"] == ["offline"]
    assert query["client_id"] == ["test-client-id"]
    assert len(_state_from(url)) > 20


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
    client: httpx.AsyncClient, google_settings: None, google_transport: httpx.AsyncClient
) -> None:
    await _register(client)
    resp = await client.get(
        f"{API_PREFIX}/auth/google/callback",
        params={"state": "whatever", "error": "access_denied"},
    )
    assert resp.status_code == 400
    assert "access_denied" in resp.json()["detail"]


# --- account binding ------------------------------------------------------

async def test_connect_callback_binds_account_and_encrypts_credentials(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: GoogleStub,
) -> None:
    token = await _register(client)
    resp = await _callback(client, state=_state_from(await _connect(client, token)))
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
    url = await _connect(client, token)
    assert (await _callback(client, state=_state_from(url))).status_code == 200

    stub.email = "alice-renamed@example.com"
    url2 = await _connect(client, token)
    resp = await _callback(client, state=_state_from(url2))
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


async def test_distinct_google_identities_bind_separately(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: GoogleStub,
) -> None:
    token = await _register(client)
    stub.sub = "sub-one"
    assert (
        await _callback(client, state=_state_from(await _connect(client, token)))
    ).status_code == 200
    stub.sub = "sub-two"
    assert (
        await _callback(client, state=_state_from(await _connect(client, token)))
    ).status_code == 200

    accounts = await _accounts(client, token)
    assert [account["google_sub"] for account in accounts] == ["sub-one", "sub-two"]


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
    assert (
        await _callback(client, state=_state_from(await _connect(client, token_a)))
    ).status_code == 200
    stub.sub = "sub-bob"
    assert (
        await _callback(client, state=_state_from(await _connect(client, token_b)))
    ).status_code == 200

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
    assert (
        await _callback(client, state=_state_from(await _connect(client, token_a)))
    ).status_code == 200
    assert (
        await _callback(client, state=_state_from(await _connect(client, token_b)))
    ).status_code == 200

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

        credentials = encrypt_credentials(
            {"access_token": "tok", "refresh_token": "refresh"}
        )
        account = await bind_or_reconnect_google_account(
            session,
            tenant_id=tenant.id,
            user_id=user_a.id,
            google_sub="shared-sub",
            email="a@example.com",
            scopes="openid email",
            credentials_encrypted=credentials,
            token_expires_at=datetime.now(UTC) + timedelta(hours=1),
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
                credentials_encrypted=credentials,
                token_expires_at=datetime.now(UTC) + timedelta(hours=1),
            )


async def test_refresh_rotates_encrypted_access_token(
    google_settings: None, stub: GoogleStub
) -> None:
    tenant, user = await _make_user_and_tenant()
    async with async_session_factory() as session:
        credentials = encrypt_credentials(
            {"access_token": "old-access-token", "refresh_token": stub.refresh_token}
        )
        account = await bind_or_reconnect_google_account(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            google_sub="sub-refresh",
            email="refresh@example.com",
            scopes="openid email",
            credentials_encrypted=credentials,
            token_expires_at=datetime.now(UTC) + timedelta(seconds=30),
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
        credentials = encrypt_credentials(
            {"access_token": "old-access-token", "refresh_token": stub.refresh_token}
        )
        account = await bind_or_reconnect_google_account(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            google_sub="sub-revoked",
            email="revoked@example.com",
            scopes="openid email",
            credentials_encrypted=credentials,
            token_expires_at=datetime.now(UTC) + timedelta(seconds=30),
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
        credentials = encrypt_credentials({"access_token": "only-access"})
        account = await bind_or_reconnect_google_account(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            google_sub="sub-no-refresh",
            email="norefresh@example.com",
            scopes="openid email",
            credentials_encrypted=credentials,
            token_expires_at=datetime.now(UTC) + timedelta(seconds=30),
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
        credentials = encrypt_credentials(
            {"access_token": "tok", "refresh_token": stub.refresh_token}
        )
        account = await bind_or_reconnect_google_account(
            session,
            tenant_id=tenant_a.id,
            user_id=user_a.id,
            google_sub="sub-cross",
            email="cross@example.com",
            scopes="openid email",
            credentials_encrypted=credentials,
            token_expires_at=datetime.now(UTC) + timedelta(seconds=30),
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