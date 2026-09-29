"""Google access-token lifecycle tests through production seams.

Every test drives a real provider service (``kai.calendar.service``,
``kai.gmail.service``, ``kai.contacts.service``) or the real HTTP callback —
the exact functions production calls — against the real PostgreSQL and the
real crypto layer. Only Google's network sits behind ``httpx.MockTransport``.

These are the regression guards for GOO-01 (access tokens were never refreshed
in production) and SEC-01 (reconnect could overwrite the stored refresh token
with null). They exist to prove the *production* refresh path, not a helper:
``refresh_google_account`` alone was never called by production, which is
precisely the defect these tests pin against.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Final
from urllib.parse import parse_qs, parse_qsl, urlparse
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select, text

from kai.calendar.service import list_events
from kai.contacts.service import list_contacts
from kai.db.engine import async_session_factory, engine
from kai.db.models import GoogleAccount, Tenant, User
from kai.gmail.service import search_messages
from kai.google.client import GoogleOAuthError
from kai.google.crypto import decrypt_credentials
from kai.google.deps import get_google_http_client
from kai.google.service import bind_or_reconnect_google_account
from kai.main import app

API_PREFIX: Final = "/api/v1"

CALENDAR_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
GMAIL_URL = "https://www.googleapis.com/gmail/v1/users/me/messages"
CONTACTS_URL = "https://people.googleapis.com/v1/people/me/connections"

STORED_ACCESS = "stored-access-token"
REFRESHED_ACCESS = "refreshed-access-token"
STORED_REFRESH = "stored-refresh-token"


class LifecycleStub:
    """Deterministic Google transport recording every token/provider call."""

    def __init__(self) -> None:
        self.refresh_issuer_status = 200
        self.refresh_token_in_response = STORED_REFRESH
        self.authorization_access_token = STORED_ACCESS
        self.omit_refresh_on_exchange = False
        self.refresh_delay = 0.0
        self.token_calls: list[httpx.Request] = []
        self.refresh_calls: list[httpx.Request] = []
        self.provider_calls: list[httpx.Request] = []

    def _token_payload(self, form: dict[str, str]) -> dict:
        if form["grant_type"] == "authorization_code":
            payload: dict = {
                "access_token": self.authorization_access_token,
                "expires_in": 3600,
            }
            if not self.omit_refresh_on_exchange:
                payload["refresh_token"] = STORED_REFRESH
            return payload
        payload = {"access_token": REFRESHED_ACCESS, "expires_in": 3599}
        if self.refresh_token_in_response:
            payload["refresh_token"] = self.refresh_token_in_response
        return payload

    async def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com" and request.url.path == "/token":
            self.token_calls.append(request)
            if self.refresh_delay:
                await asyncio.sleep(self.refresh_delay)
            if self.refresh_issuer_status != 200:
                return httpx.Response(
                    self.refresh_issuer_status, json={"error": "invalid_grant"}
                )
            form = dict(parse_qsl(request.content.decode("utf-8")))
            if form["grant_type"] == "refresh_token":
                self.refresh_calls.append(request)
            return httpx.Response(200, json=self._token_payload(form))
        if request.url.host == "www.googleapis.com" and request.url.path.endswith(
            "/oauth2/v3/userinfo"
        ):
            return httpx.Response(
                200,
                json={
                    "sub": "sub-123",
                    "email": "alice@example.com",
                    "email_verified": True,
                    "name": "Alice",
                    "picture": "https://example.com/alice.png",
                },
            )
        if request.url.host == "www.googleapis.com":
            self.provider_calls.append(request)
            if request.url.path.startswith("/calendar/v3/"):
                return httpx.Response(200, json={"items": []})
            if request.url.path.startswith("/gmail/v1/users/me/messages"):
                return httpx.Response(
                    200,
                    json={
                        "messages": [{"id": "m1", "threadId": "t1"}],
                        "resultSizeEstimate": 1,
                    },
                )
        if request.url.host == "people.googleapis.com":
            self.provider_calls.append(request)
            return httpx.Response(200, json={"connections": []})
        return httpx.Response(404)


@pytest.fixture
def stub() -> LifecycleStub:
    return LifecycleStub()


@pytest_asyncio.fixture
async def transport(
    stub: LifecycleStub,
) -> httpx.AsyncClient:
    async with httpx.AsyncClient(transport=httpx.MockTransport(stub.handler)) as t:
        app.dependency_overrides[get_google_http_client] = lambda: t
        try:
            yield t
        finally:
            app.dependency_overrides.pop(get_google_http_client, None)


async def _make_tenant_user() -> tuple[Tenant, User]:
    async with async_session_factory() as session:
        tenant = Tenant(name=f"t-{uuid4()}")
        session.add(tenant)
        await session.flush()
        user = User(
            tenant_id=tenant.id,
            email=f"u-{uuid4()}@example.com",
            password_hash="hash",
        )
        session.add(user)
        await session.commit()
        await session.refresh(tenant)
        await session.refresh(user)
        return tenant, user


async def _bind_account(
    session,
    *,
    tenant_id,
    user_id,
    access_token: str = STORED_ACCESS,
    refresh_token: str | None = STORED_REFRESH,
    expires_in: int = 3600,
) -> GoogleAccount:
    return await bind_or_reconnect_google_account(
        session,
        tenant_id=tenant_id,
        user_id=user_id,
        google_sub=f"sub-{uuid4()}",
        email="alice@example.com",
        scopes="openid email",
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=expires_in,
    )


async def _expire_account(account_id) -> None:
    """Simulate time passing: the stored access token is now expired."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE google_accounts SET token_expires_at = "
                "now() - interval '1 hour' WHERE id = :id"
            ),
            {"id": str(account_id)},
        )


async def _stored(account_id) -> dict:
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                text("SELECT credentials_encrypted FROM google_accounts WHERE id = :id"),
                {"id": str(account_id)},
            )
        ).scalar_one()
    return decrypt_credentials(row)


# --- GOO-01-A: a valid token is used as-is, no refresh ---------------------

async def test_valid_token_is_used_without_refresh(
    google_settings: None, transport: httpx.AsyncClient, stub: LifecycleStub
) -> None:
    tenant, user = await _make_tenant_user()
    async with async_session_factory() as session:
        account = await _bind_account(session, tenant_id=tenant.id, user_id=user.id)
        account_id = account.id
        events = await list_events(
            session,
            transport,
            account_id=account_id,
            tenant_id=tenant.id,
            start=datetime(2026, 1, 1, tzinfo=UTC),
            end=datetime(2026, 1, 2, tzinfo=UTC),
        )
    assert events == []
    assert stub.token_calls == []
    assert (
        stub.provider_calls[0].headers["Authorization"]
        == f"Bearer {STORED_ACCESS}"
    )


# --- GOO-01-B: an expired token is refreshed before the provider call ------

async def test_expired_token_is_refreshed_and_new_token_used(
    google_settings: None, transport: httpx.AsyncClient, stub: LifecycleStub
) -> None:
    tenant, user = await _make_tenant_user()
    async with async_session_factory() as session:
        account = await _bind_account(session, tenant_id=tenant.id, user_id=user.id)
        account_id = account.id
    await _expire_account(account_id)

    async with async_session_factory() as session:
        events = await list_events(
            session,
            transport,
            account_id=account_id,
            tenant_id=tenant.id,
            start=datetime(2026, 1, 1, tzinfo=UTC),
            end=datetime(2026, 1, 2, tzinfo=UTC),
        )
        assert events == []
        after = await _stored(account_id)
    assert len(stub.token_calls) == 1
    assert (
        stub.provider_calls[0].headers["Authorization"]
        == f"Bearer {REFRESHED_ACCESS}"
    )
    assert after["access_token"] == REFRESHED_ACCESS
    assert after["refresh_token"] == STORED_REFRESH


async def test_near_expiry_token_is_refreshed(
    google_settings: None, transport: httpx.AsyncClient, stub: LifecycleStub
) -> None:
    tenant, user = await _make_tenant_user()
    async with async_session_factory() as session:
        account = await _bind_account(
            session, tenant_id=tenant.id, user_id=user.id, expires_in=30
        )
        account_id = account.id
        await list_events(
            session,
            transport,
            account_id=account_id,
            tenant_id=tenant.id,
            start=datetime(2026, 1, 1, tzinfo=UTC),
            end=datetime(2026, 1, 2, tzinfo=UTC),
        )
    assert len(stub.token_calls) == 1


# --- GOO-01-C: expired token with no refresh token is an explicit error -----

async def test_expired_token_without_refresh_token_raises_oauth_error(
    google_settings: None, transport: httpx.AsyncClient
) -> None:
    tenant, user = await _make_tenant_user()
    async with async_session_factory() as session:
        account = await _bind_account(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            refresh_token=None,
        )
        account_id = account.id
        await _expire_account(account_id)

    async with async_session_factory() as session:
        with pytest.raises(GoogleOAuthError):
            await list_events(
                session,
                transport,
                account_id=account_id,
                tenant_id=tenant.id,
                start=datetime(2026, 1, 1, tzinfo=UTC),
                end=datetime(2026, 1, 2, tzinfo=UTC),
            )


# --- GOO-01-D: refresh response omits refresh_token → stored one preserved ---

async def test_refresh_response_without_refresh_token_preserves_stored(
    google_settings: None, transport: httpx.AsyncClient, stub: LifecycleStub
) -> None:
    tenant, user = await _make_tenant_user()
    async with async_session_factory() as session:
        account = await _bind_account(session, tenant_id=tenant.id, user_id=user.id)
        account_id = account.id
        await _expire_account(account_id)

    stub.refresh_token_in_response = None
    async with async_session_factory() as session:
        await list_contacts(
            session, transport, account_id=account_id, tenant_id=tenant.id
        )
        after = await _stored(account_id)
    assert after["access_token"] == REFRESHED_ACCESS
    assert after["refresh_token"] == STORED_REFRESH


async def test_refresh_rejection_fails_the_production_call_and_keeps_credentials(
    google_settings: None, transport: httpx.AsyncClient, stub: LifecycleStub
) -> None:
    """A Google refresh rejection must surface on the production path.

    ``test_google_oauth.py::test_refresh_rejects_revoked_credentials`` already
    pins that the rejection raises, but it calls ``refresh_google_account``
    directly — the helper production never invokes. This test pins the same
    invariant where it actually matters: through the vertical service, so a
    swallowed refresh failure cannot be reported as a successful provider call.
    """
    tenant, user = await _make_tenant_user()
    async with async_session_factory() as session:
        account = await _bind_account(session, tenant_id=tenant.id, user_id=user.id)
        account_id = account.id
    await _expire_account(account_id)
    before = await _stored(account_id)

    # Google rejects the refresh token (revoked consent / invalid_grant).
    stub.refresh_issuer_status = 400

    async with async_session_factory() as session:
        with pytest.raises(GoogleOAuthError):
            await list_events(
                session,
                transport,
                account_id=account_id,
                tenant_id=tenant.id,
                start=datetime(2026, 1, 1, tzinfo=UTC),
                end=datetime(2026, 1, 2, tzinfo=UTC),
            )

    # No provider call was attempted with a token that is known to be unusable.
    assert stub.provider_calls == []
    # The exchange was attempted exactly once and rejected (``refresh_calls`` is
    # only recorded by the stub for a 200, so the attempt is counted here).
    assert len(stub.token_calls) == 1
    # The rejection touched no stored credential.
    after = await _stored(account_id)
    assert after == before


# --- all three Google verticals share the refresh-aware authority -----------

async def test_all_three_verticals_refresh_through_the_same_authority(
    google_settings: None, transport: httpx.AsyncClient, stub: LifecycleStub
) -> None:
    tenant, user = await _make_tenant_user()
    async with async_session_factory() as session:
        account = await _bind_account(session, tenant_id=tenant.id, user_id=user.id)
        account_id = account.id
    await _expire_account(account_id)

    async with async_session_factory() as session:
        await list_contacts(
            session, transport, account_id=account_id, tenant_id=tenant.id
        )
    await _expire_account(account_id)
    async with async_session_factory() as session:
        await list_events(
            session,
            transport,
            account_id=account_id,
            tenant_id=tenant.id,
            start=datetime(2026, 1, 1, tzinfo=UTC),
            end=datetime(2026, 1, 2, tzinfo=UTC),
        )
    await _expire_account(account_id)
    async with async_session_factory() as session:
        await search_messages(
            session,
            transport,
            account_id=account_id,
            tenant_id=tenant.id,
            query="in:inbox",
            max_results=10,
        )

    assert len(stub.refresh_calls) == 3
    assert len(stub.provider_calls) == 3
    assert all(
        call.headers["Authorization"] == f"Bearer {REFRESHED_ACCESS}"
        for call in stub.provider_calls
    )


# --- single-flight: N concurrent callers → exactly one refresh ---------------

async def test_concurrent_refresh_is_single_flight(
    google_settings: None, transport: httpx.AsyncClient, stub: LifecycleStub
) -> None:
    tenant, user = await _make_tenant_user()
    async with async_session_factory() as session:
        account = await _bind_account(session, tenant_id=tenant.id, user_id=user.id)
        account_id = account.id
    await _expire_account(account_id)
    stub.refresh_delay = 0.05  # widen the window where every caller sees "expired"

    async def _call_once() -> None:
        async with async_session_factory() as session:
            await list_events(
                session,
                transport,
                account_id=account_id,
                tenant_id=tenant.id,
                start=datetime(2026, 1, 1, tzinfo=UTC),
                end=datetime(2026, 1, 2, tzinfo=UTC),
            )

    results = await asyncio.gather(*[_call_once() for _ in range(5)])
    assert results == [None] * 5
    assert len(stub.refresh_calls) == 1
    after = await _stored(account_id)
    assert after["access_token"] == REFRESHED_ACCESS


# --- M6: the single-flight waiter releases the row lock as soon as it re-reads


class LockReleaseStub(LifecycleStub):
    """Adds two deterministic gates around the row-lock handoff.

    ``hold_winner_in_refresh`` freezes the winning caller inside the token
    exchange. That is the exact window in which it owns the ``google_accounts``
    row lock, because ``_lock_account`` runs before the exchange. Waiting for
    ``a_in_refresh`` is therefore a positive proof that the lock is taken, with
    no elapsed-time assumption.

    ``hold_waiter_at_provider`` freezes the waiting caller inside its provider
    call, which is the first thing it does *after* ``usable_access_token``
    returned. Waiting for ``b_at_provider`` proves the waiter acquired the lock,
    re-read the account, judged the token fresh, and skipped the exchange.
    """

    def __init__(self) -> None:
        super().__init__()
        self.hold_winner_in_refresh = False
        self.hold_waiter_at_provider = False
        self.a_in_refresh = asyncio.Event()
        self.release_a = asyncio.Event()
        self.b_at_provider = asyncio.Event()
        self.release_b = asyncio.Event()

    async def handler(self, request: httpx.Request) -> httpx.Response:
        if (
            self.hold_winner_in_refresh
            and request.url.host == "oauth2.googleapis.com"
            and request.url.path == "/token"
        ):
            self.a_in_refresh.set()
            await self.release_a.wait()
            return await super().handler(request)
        if (
            self.hold_waiter_at_provider
            and request.url.host == "www.googleapis.com"
            and request.url.path.startswith("/calendar/v3/")
        ):
            self.b_at_provider.set()
            await self.release_b.wait()
        return await super().handler(request)


def _is_lock_unavailable(exc: BaseException) -> bool:
    """True when an exception is PostgreSQL's ``55P03`` (lock not available)."""
    seen = 0
    current: BaseException | None = exc
    while current is not None and seen < 10:
        if getattr(current, "sqlstate", None) == "55P03":
            return True
        if getattr(current, "pgcode", None) == "55P03":
            return True
        current = current.__cause__ or current.__context__
        seen += 1
    return False


async def _google_account_row_lock_is_free(account_id) -> bool:
    """Probe the row lock the way production takes it, but without waiting.

    ``NOWAIT`` means the probe returns instead of blocking, so the answer is a
    fact about the lock at this instant rather than a race against a timeout.
    """
    async with async_session_factory() as session:
        try:
            await session.execute(
                select(GoogleAccount)
                .where(GoogleAccount.id == account_id)
                .with_for_update(nowait=True)
            )
        except Exception as exc:
            await session.rollback()
            if _is_lock_unavailable(exc):
                return False
            raise
        await session.rollback()
        return True


async def _wait_for_a_lock_waiter() -> None:
    """Block until some transaction is actually queued on a lock.

    This waits on a state the database reports, not on a guessed duration, so it
    cannot pass before the waiter is genuinely parked on the winner's lock.
    """
    for _ in range(600):
        async with async_session_factory() as session:
            waiting = (
                await session.execute(
                    text("SELECT count(*) FROM pg_locks WHERE NOT granted")
                )
            ).scalar_one()
        if int(waiting) > 0:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("no transaction ever waited on the google_accounts row lock")


async def test_single_flight_waiter_releases_the_row_lock_before_its_provider_call(
    google_settings: None, transport: httpx.AsyncClient, stub: LifecycleStub
) -> None:
    """The waiter must hand the row lock back once it has re-read a fresh token.

    ``test_concurrent_refresh_is_single_flight`` proves no duplicate exchange
    happens. It does not prove the waiter *released* the lock, because a waiter
    that never committed still returns the right token and still avoids the
    second refresh — it simply keeps the row locked for the rest of its request.
    That is what this test isolates, and it is the behaviour ``M6`` (deleting the
    ``session.commit()`` in the waiter's fresh branch) removes.

    The sequence asserted here, in order:

    1. the winner holds the row lock and is inside the token exchange;
    2. the waiter is genuinely queued on that same row;
    3. the winner refreshes, persists, and commits — releasing the lock;
    4. the waiter acquires the lock, re-reads, sees the fresh token, and does not
       exchange again;
    5. by the time the waiter is inside its provider call — its session still
       open — the row lock is free again.
    """
    locked_stub = LockReleaseStub()
    locked_stub.hold_winner_in_refresh = True
    locked_stub.hold_waiter_at_provider = True
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(locked_stub.handler)
    ) as locked_transport:
        tenant, user = await _make_tenant_user()
        async with async_session_factory() as session:
            account = await _bind_account(
                session, tenant_id=tenant.id, user_id=user.id
            )
            account_id = account.id
        await _expire_account(account_id)

        async def _winner() -> None:
            async with async_session_factory() as session:
                await list_contacts(
                    session, locked_transport, account_id=account_id, tenant_id=tenant.id
                )

        async def _waiter() -> None:
            async with async_session_factory() as session:
                await list_events(
                    session,
                    locked_transport,
                    account_id=account_id,
                    tenant_id=tenant.id,
                    start=datetime(2026, 1, 1, tzinfo=UTC),
                    end=datetime(2026, 1, 2, tzinfo=UTC),
                )

        winner = asyncio.create_task(_winner())
        waiter: asyncio.Task[None] | None = None
        try:
            # 1. the winner owns the row lock.
            await asyncio.wait_for(locked_stub.a_in_refresh.wait(), timeout=15)
            waiter = asyncio.create_task(_waiter())
            # 2. the waiter is parked on it, so the winner's commit below is what
            #    lets the waiter through.
            await asyncio.wait_for(_wait_for_a_lock_waiter(), timeout=15)
            # 3. the winner is allowed to refresh, persist, and commit.
            locked_stub.release_a.set()
            # 4. the waiter came back with the new token without exchanging.
            await asyncio.wait_for(locked_stub.b_at_provider.wait(), timeout=15)
            lock_free_while_waiter_is_in_its_request = (
                await _google_account_row_lock_is_free(account_id)
            )
        finally:
            locked_stub.release_a.set()
            locked_stub.release_b.set()
            await asyncio.gather(winner, waiter, return_exceptions=True)

    # The single flight was genuinely single.
    assert len(locked_stub.refresh_calls) == 1
    # Both callers reached a provider carrying the refreshed token, and neither
    # used the token the loser was waiting on.
    assert [
        call.headers["Authorization"] for call in locked_stub.provider_calls
    ] == [f"Bearer {REFRESHED_ACCESS}", f"Bearer {REFRESHED_ACCESS}"]
    # The refreshed token is what the row now holds.
    stored = await _stored(account_id)
    assert stored["access_token"] == REFRESHED_ACCESS
    # And the waiter had already given the row lock back.
    assert lock_free_while_waiter_is_in_its_request is True


# --- end-to-end: the HTTP vertical slice refreshes on the real path ---------

async def test_http_calendar_slots_refreshes_expired_token(
    client: httpx.AsyncClient,
    google_settings: None,
    transport: httpx.AsyncClient,
    stub: LifecycleStub,
) -> None:
    token = await _register(client)
    url = await _connect(client, token)
    assert (
        await client.get(
            f"{API_PREFIX}/auth/google/callback",
            params={"state": _state_from(url), "code": "auth-code-123"},
        )
    ).status_code == 200

    async with engine.begin() as conn:
        account_id = (
            await conn.execute(text("SELECT id FROM google_accounts"))
        ).scalar_one()
    await _expire_account(account_id)

    resp = await client.get(
        f"{API_PREFIX}/calendar/slots",
        params={"date": "2026-06-24", "timezone": "UTC"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert len(stub.refresh_calls) == 1
    assert stub.provider_calls[0].headers["Authorization"] == (
        f"Bearer {REFRESHED_ACCESS}"
    )


# --- SEC-01: a reconnect must not destroy the stored refresh token --------

async def test_http_reconnect_keeps_refresh_token_and_rearms_the_expiry(
    client: httpx.AsyncClient,
    google_settings: None,
    transport: httpx.AsyncClient,
    stub: LifecycleStub,
) -> None:
    """A reconnect whose token response omits ``refresh_token`` must keep the
    stored one, and must re-arm ``token_expires_at`` from Google's ``expires_in``.

    Two invariants on the production path:

    * the account's only long-lived credential survives the reconnect, so every
      later refresh is still possible;
    * the access token Google just issued is recorded as fresh, so the next
      vertical call does not spend a refresh on a token that is still valid.

    ``test_google_oauth.py`` pins the first invariant against the stored
    credential only. It never inspects the expiry, so a reconnect that stored
    the new token with a stale lifetime passed the whole suite: the row was
    already dead, and every call after it burned a redundant refresh.
    """
    token = await _register(client)
    url = await _connect(client, token)
    assert (
        await client.get(
            f"{API_PREFIX}/auth/google/callback",
            params={"state": _state_from(url), "code": "auth-code-123"},
        )
    ).status_code == 200

    async with engine.begin() as conn:
        account_id = (
            await conn.execute(text("SELECT id FROM google_accounts"))
        ).scalar_one()
    await _expire_account(account_id)

    # Google re-authorizes without changing consent: a new access token, and no
    # refresh_token in the response.
    stub.authorization_access_token = "reconnected-access-token"
    stub.omit_refresh_on_exchange = True
    url2 = await _connect(client, token)
    assert (
        await client.get(
            f"{API_PREFIX}/auth/google/callback",
            params={"state": _state_from(url2), "code": "auth-code-456"},
        )
    ).status_code == 200

    after = await _stored(account_id)
    assert after["access_token"] == "reconnected-access-token"
    assert after["refresh_token"] == STORED_REFRESH

    async with engine.begin() as conn:
        expires_at = (
            await conn.execute(
                text("SELECT token_expires_at FROM google_accounts WHERE id = :id"),
                {"id": str(account_id)},
            )
        ).scalar_one()
    assert expires_at is not None
    assert expires_at > datetime.now(UTC)

    # The reconnected account is immediately usable: no refresh is needed.
    resp = await client.get(
        f"{API_PREFIX}/calendar/slots",
        params={"date": "2026-06-24", "timezone": "UTC"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert stub.refresh_calls == []
    assert stub.provider_calls[0].headers["Authorization"] == (
        "Bearer reconnected-access-token"
    )


# --- helpers mirrored from test_google_oauth where the app is exercised -----

async def _register(client: httpx.AsyncClient) -> str:
    resp = await client.post(
        f"{API_PREFIX}/auth/register",
        json={"email": f"alice-{uuid4()}@example.com", "password": "password123"},
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