"""D41: deferred Google linking and the pending-confirmation contract.

Two groups live here.

The ``no_db_unit`` group needs no PostgreSQL and no Google: it pins the digest
handling, the safe response shape, the refresh-merge policy, the fail-closed
guard, the confirmation page's resource and storage policy, and the refresh
error classification.

The remaining tests are PostgreSQL integration tests written against the real
engine/crypto and the deterministic Google stub. They are not runnable without a
disposable database (``docs/TEST_DATABASE.md``), and this repository's D41 run
did **not** execute them.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Final
from urllib.parse import parse_qs, urlparse
from uuid import UUID, uuid4

import asyncpg
import httpx
import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql.asyncpg import AsyncAdapt_asyncpg_dbapi
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from kai.api import google as google_api
from kai.api.google import (
    CONFIRM_PAGE_CSP,
    CONFIRM_PAGE_HTML,
    OAUTH_BINDING_COOKIE,
    PENDING_COOKIE,
)
from kai.auth.jwt import create_access_token
from kai.auth.security import hash_password
from kai.db.engine import async_session_factory, engine
from kai.db.models import GoogleAccount, User
from kai.google import pending as pending_module
from kai.google import service as service_module
from kai.google.client import (
    GoogleOAuthError,
    GoogleTokenRequestError,
    is_permanent_refresh_rejection,
)
from kai.google.crypto import decrypt_credentials
from kai.google.deps import get_google_http_client
from kai.google.pending import (
    PENDING_TTL_SECONDS,
    TERMINAL_RETENTION,
    PendingNotPendingError,
    PendingView,
    confirm_pending_transaction,
    hash_token,
    new_confirmation_token,
    pending_view_payload,
    purge_pending_transactions,
)
from kai.google.service import (
    GoogleAccountConflictError,
    GoogleReconnectRequiredError,
    bind_or_reconnect_google_account,
    merge_credentials,
)
from kai.main import app
from test_google_oauth import GoogleStub

API_PREFIX: Final = "/api/v1"

IDENTITY_SCOPES: Final = ["openid", "email"]
CONTACTS_SCOPE: Final = "https://www.googleapis.com/auth/contacts.readonly"
CALENDAR_SCOPE: Final = "https://www.googleapis.com/auth/calendar.events"
CONNECT_GRANTED: Final = " ".join([*IDENTITY_SCOPES, CONTACTS_SCOPE, CALENDAR_SCOPE])


# --------------------------------------------------------------------------- #
# Pure unit tests (no database, no network)
# --------------------------------------------------------------------------- #


@pytest.mark.no_db_unit
def test_pending_lifetimes_are_fixed_not_configurable() -> None:
    assert PENDING_TTL_SECONDS == 600
    assert TERMINAL_RETENTION.total_seconds() == 24 * 60 * 60


@pytest.mark.no_db_unit
def test_pending_integrity_error_filters_expected_uniqueness_constraints() -> None:
    def _asyncpg_error(constraint_name: str) -> IntegrityError:
        native = asyncpg.exceptions.UniqueViolationError.new(
            {"C": "23505", "n": constraint_name}
        )
        adapted = AsyncAdapt_asyncpg_dbapi.IntegrityError(str(native), native)
        adapted.__cause__ = native
        return IntegrityError("INSERT INTO google_oauth_pending", {}, adapted)

    expected = _asyncpg_error("uq_google_oauth_pending_state_hash")
    unexpected = _asyncpg_error("uq_users_email")
    without_constraint = IntegrityError(
        "INSERT INTO google_oauth_pending", {}, RuntimeError("duplicate key")
    )

    assert pending_module._constraint_name_for(expected) == "uq_google_oauth_pending_state_hash"
    assert pending_module._constraint_name_for(unexpected) == "uq_users_email"
    assert pending_module._constraint_name_for(without_constraint) is None


@pytest.mark.no_db_unit
def test_google_account_integrity_error_filters_only_expected_unique_constraint() -> None:
    def _asyncpg_error(constraint_name: str) -> IntegrityError:
        native = asyncpg.exceptions.UniqueViolationError.new(
            {"C": "23505", "n": constraint_name}
        )
        adapted = AsyncAdapt_asyncpg_dbapi.IntegrityError(str(native), native)
        adapted.__cause__ = native
        return IntegrityError("INSERT INTO google_accounts", {}, adapted)

    expected = _asyncpg_error("uq_google_accounts_tenant_google_sub")
    unexpected = _asyncpg_error("uq_users_email")
    without_constraint = IntegrityError(
        "INSERT INTO google_accounts", {}, RuntimeError("duplicate key")
    )

    assert service_module._constraint_name_for(expected) == "uq_google_accounts_tenant_google_sub"
    assert service_module._constraint_name_for(unexpected) == "uq_users_email"
    assert service_module._constraint_name_for(without_constraint) is None


@pytest.mark.no_db_unit
def test_invalid_origin_is_rejected_without_500() -> None:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/auth/google/pending/confirm",
        "headers": [(b"origin", "https://例子.com".encode())],
        "scheme": "https",
        "server": ("testserver", 443),
        "root_path": "",
        "client": ("127.0.0.1", 1234),
        "query_string": b"",
    }
    request = Request(scope)

    with pytest.raises(HTTPException) as exc:
        google_api._require_same_origin(request)

    assert exc.value.status_code == 403
    assert exc.value.detail == "cross-origin request refused"


@pytest.mark.no_db_unit
def test_non_ascii_host_is_rejected_without_type_error() -> None:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/auth/google/pending/confirm",
        "headers": [
            (b"host", b"test\xff"),
            (b"origin", b"https://testserver"),
        ],
        "scheme": "https",
        "server": ("testserver", 443),
        "root_path": "",
        "client": ("127.0.0.1", 1234),
        "query_string": b"",
    }
    request = Request(scope)

    with pytest.raises(HTTPException) as exc:
        google_api._require_same_origin(request)

    assert exc.value.status_code == 403
    assert exc.value.detail == "cross-origin request refused"


@pytest.mark.no_db_unit
def test_confirmation_token_is_random_and_only_its_digest_is_shape_stable() -> None:
    first = new_confirmation_token()
    second = new_confirmation_token()

    assert first != second
    assert len(first) >= 43
    # A digest is fixed-width and contains nothing of the raw value.
    for token in (first, second):
        digest = hash_token(token)
        assert len(digest) == 64
        assert digest == hash_token(token)
        assert token not in digest
        int(digest, 16)


@pytest.mark.no_db_unit
def test_empty_token_has_no_digest() -> None:
    with pytest.raises(ValueError):
        hash_token("")


@pytest.mark.no_db_unit
def test_pending_payload_is_exactly_the_safe_field_set() -> None:
    view = PendingView(
        id=uuid4(),
        tenant_id=uuid4(),
        user_id=uuid4(),
        google_sub="sub-123",
        google_email="alice@example.com",
        email_verified=True,
        scopes=CONNECT_GRANTED,
        scopes_from_provider=True,
        status="pending",
        expires_at=datetime.now(UTC) + timedelta(seconds=600),
        binding="new",
    )

    payload = pending_view_payload(view)
    serialized = json.dumps(payload)

    assert set(payload) == {
        "google_email",
        "google_sub",
        "email_verified",
        "scopes",
        "scopes_from_provider",
        "binding",
        "status",
        "expires_at",
    }
    assert payload["scopes"] == CONNECT_GRANTED.split()
    assert "credential" not in serialized
    assert "access_token" not in serialized
    assert "refresh_token" not in serialized
    assert "encrypted" not in serialized


@pytest.mark.no_db_unit
def test_merge_keeps_the_stored_refresh_token_when_google_omits_it() -> None:
    merged = merge_credentials(
        {"access_token": "old", "refresh_token": "stored-refresh"},
        access_token="new",
        refresh_token=None,
    )

    assert merged == {"access_token": "new", "refresh_token": "stored-refresh"}


@pytest.mark.no_db_unit
def test_merge_replaces_the_refresh_token_only_when_google_issues_one() -> None:
    merged = merge_credentials(
        {"access_token": "old", "refresh_token": "stored-refresh"},
        access_token="new",
        refresh_token="rotated-refresh",
    )

    assert merged == {"access_token": "new", "refresh_token": "rotated-refresh"}


@pytest.mark.no_db_unit
def test_only_a_provider_side_rejection_is_a_permanent_refresh_failure() -> None:
    assert is_permanent_refresh_rejection(
        GoogleTokenRequestError(
            "google token request failed (400: invalid_grant)",
            status_code=400,
            error_code="invalid_grant",
        )
    )
    # A 5xx, a client misconfiguration, or a plain transport error says nothing
    # about the user's grant and must stay retryable.
    assert not is_permanent_refresh_rejection(
        GoogleTokenRequestError(
            "google token request failed (503: unknown)",
            status_code=503,
            error_code="unknown",
        )
    )
    assert not is_permanent_refresh_rejection(
        GoogleOAuthError("google token endpoint is unreachable")
    )


@pytest.mark.no_db_unit
def test_reconnect_error_stays_a_google_oauth_error() -> None:
    """Existing handlers catch ``GoogleOAuthError``; the subtype must fit."""
    assert issubclass(GoogleReconnectRequiredError, GoogleOAuthError)


@pytest.mark.no_db_unit
def test_confirmation_page_uses_no_external_resource_or_persistent_storage() -> None:
    html = CONFIRM_PAGE_HTML

    assert "http://" not in html
    assert "https://" not in html
    assert "localStorage" not in html
    assert "sessionStorage" not in html
    assert "document.cookie" not in html
    assert "innerHTML" not in html
    # Every server- or provider-supplied value is written as text.
    assert "textContent" in html
    assert "/auth/login" in html


@pytest.mark.no_db_unit
def test_confirmation_page_policy_is_self_contained() -> None:
    assert "default-src 'none'" in CONFIRM_PAGE_CSP
    assert "frame-ancestors 'none'" in CONFIRM_PAGE_CSP
    assert "connect-src 'self'" in CONFIRM_PAGE_CSP
    assert "https://" not in CONFIRM_PAGE_CSP


@pytest.mark.no_db_unit
def test_linking_surface_never_calls_google_revoke() -> None:
    """D41 adds no automatic revoke: rejection is local containment only."""
    for module in (google_api, pending_module, service_module):
        assert "revoke_access_token" not in inspect.getsource(module)


@pytest_asyncio.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    """A secure-origin ASGI client: ``__Host-`` cookies require https."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://testserver"
    ) as c:
        yield c


@pytest.fixture
def stub() -> GoogleStub:
    stub = GoogleStub()
    # Pending-view integration tests assert the provider-confirmed scope flag.
    # Make the default stub response carry the same granted scopes that the
    # OAuth state requests; tests that exercise an absent scope field belong to
    # the OAuth client contract suite and override this explicitly there.
    stub.token_scope = CONNECT_GRANTED
    return stub


@pytest_asyncio.fixture
async def google_transport(stub: GoogleStub) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(stub.handler)
    ) as transport:
        app.dependency_overrides[get_google_http_client] = lambda: transport
        try:
            yield transport
        finally:
            app.dependency_overrides.pop(get_google_http_client, None)


@pytest.fixture(autouse=False)
def _forbid_real_database() -> Iterator[None]:
    async def _no_session() -> AsyncIterator[object]:
        yield object()

    from kai.db.engine import get_session

    app.dependency_overrides[get_session] = _no_session
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_session, None)


@pytest.mark.no_db_unit
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/auth/google/confirm"),
        ("GET", "/auth/google/pending"),
        ("POST", "/auth/google/pending/confirm"),
        ("POST", "/auth/google/pending/cancel"),
    ],
)
async def test_guard_blocks_every_pending_route(
    method: str,
    path: str,
    client: httpx.AsyncClient,
    _forbid_real_database: None,
) -> None:
    """D37 survives D41: there is no env switch and no unguarded new route."""
    resp = await client.request(method, f"{API_PREFIX}{path}")

    assert resp.status_code == 503
    assert resp.json()["detail"] == "google account linking is temporarily disabled"


@pytest.mark.no_db_unit
async def test_confirmation_page_headers_are_hardened(
    client: httpx.AsyncClient, google_settings: None
) -> None:
    """The only way to see the page is the explicit test override."""
    resp = await client.get(f"{API_PREFIX}/auth/google/confirm")

    assert resp.status_code == 200
    assert resp.headers["content-security-policy"] == CONFIRM_PAGE_CSP
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["referrer-policy"] == "no-referrer"
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["x-content-type-options"] == "nosniff"


# --------------------------------------------------------------------------- #
# PostgreSQL integration helpers
# --------------------------------------------------------------------------- #


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


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _pending_view(client: httpx.AsyncClient, token: str) -> httpx.Response:
    return await client.get(
        f"{API_PREFIX}/auth/google/pending", headers=_bearer(token)
    )


async def _confirm(client: httpx.AsyncClient, token: str) -> httpx.Response:
    return await client.post(
        f"{API_PREFIX}/auth/google/pending/confirm", headers=_bearer(token)
    )


async def _cancel(client: httpx.AsyncClient, token: str) -> httpx.Response:
    return await client.post(
        f"{API_PREFIX}/auth/google/pending/cancel", headers=_bearer(token)
    )


async def _start_pending(
    client: httpx.AsyncClient, token: str
) -> tuple[httpx.Response, str]:
    """Run connect+callback and return the redirect and the raw pending cookie."""
    started = await _callback(client, state=_state_from(await _connect(client, token)))
    assert started.status_code == 303, started.text
    raw = client.cookies.get(PENDING_COOKIE)
    assert raw is not None
    return started, raw


async def _bind_fully(client: httpx.AsyncClient, token: str) -> httpx.Response:
    _started, _raw = await _start_pending(client, token)
    resp = await _confirm(client, token)
    assert resp.status_code == 200, resp.text
    return resp


async def _account_count() -> int:
    async with engine.begin() as conn:
        return (
            await conn.execute(text("SELECT count(*) FROM google_accounts"))
        ).scalar_one()


async def _pending_count() -> int:
    async with engine.begin() as conn:
        return (
            await conn.execute(text("SELECT count(*) FROM google_oauth_pending"))
        ).scalar_one()


async def _open_pending_count() -> int:
    async with engine.begin() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT count(*) FROM google_oauth_pending"
                    " WHERE status = 'pending'"
                )
            )
        ).scalar_one()


async def _pending_row() -> dict:
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT status, failure_reason, credentials_encrypted,"
                    " token_expires_at, confirmation_hash, google_sub"
                    " FROM google_oauth_pending"
                )
            )
        ).mappings().one()
    return dict(row)


async def _stored_credentials() -> dict:
    async with engine.begin() as conn:
        row = (
            await conn.execute(text("SELECT credentials_encrypted FROM google_accounts"))
        ).scalar_one()
    return decrypt_credentials(row)


async def _expire_pending() -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE google_oauth_pending SET expires_at = :past,"
                " updated_at = :past"
            ),
            {"past": datetime.now(UTC) - timedelta(seconds=1)},
        )


async def _expire_account_token() -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE google_accounts SET token_expires_at = :past"
            ),
            {"past": datetime.now(UTC) - timedelta(hours=1)},
        )


# --------------------------------------------------------------------------- #
# Callback → pending (no account written)
# --------------------------------------------------------------------------- #


async def test_callback_creates_pending_and_writes_no_account(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: GoogleStub,
) -> None:
    token = await _register(client)

    started, raw = await _start_pending(client, token)

    assert started.headers["location"].endswith("/api/v1/auth/google/confirm")
    assert started.headers["cache-control"] == "no-store"
    assert started.headers["referrer-policy"] == "no-referrer"
    assert client.cookies.get(OAUTH_BINDING_COOKIE) is None
    assert await _account_count() == 0

    row = await _pending_row()
    assert row["status"] == "pending"
    assert row["google_sub"] == stub.sub
    assert row["failure_reason"] is None
    assert row["token_expires_at"] is not None
    # The cookie is a bearer value; the row holds only its digest.
    assert row["confirmation_hash"] == hash_token(raw)
    assert raw not in str(row)
    assert row["credentials_encrypted"] is not None
    assert stub.access_token not in row["credentials_encrypted"]
    assert decrypt_credentials(row["credentials_encrypted"]) == {
        "access_token": stub.access_token,
        "refresh_token": stub.refresh_token,
    }


async def test_pending_view_is_safe_and_links_nothing(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: GoogleStub,
) -> None:
    token = await _register(client)
    await _start_pending(client, token)

    resp = await _pending_view(client, token)

    assert resp.status_code == 200
    body = resp.json()
    assert body["google_email"] == stub.email
    assert body["google_sub"] == stub.sub
    assert body["kai_email"] == "alice@example.com"
    assert body["email_verified"] is True
    assert body["scopes"] == CONNECT_GRANTED.split()
    assert body["scopes_from_provider"] is True
    assert body["binding"] == "new"
    assert body["status"] == "pending"
    assert "credentials" not in resp.text
    assert stub.access_token not in resp.text
    assert stub.refresh_token not in resp.text
    assert await _account_count() == 0


async def test_pending_view_requires_the_cookie(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
) -> None:
    token = await _register(client)

    resp = await _pending_view(client, token)

    assert resp.status_code == 404


async def test_pending_view_is_not_reachable_with_another_users_cookie(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
) -> None:
    alice = await _register(client, "alice@example.com")
    _started, alice_raw = await _start_pending(client, alice)
    bob = await _register(client, "bob@example.com")

    client.cookies.set(PENDING_COOKIE, alice_raw, path="/")
    resp = await _pending_view(client, bob)

    assert resp.status_code == 404
    assert resp.json()["detail"] == "pending google authorization not found"


# --------------------------------------------------------------------------- #
# Confirmation
# --------------------------------------------------------------------------- #


async def test_confirm_links_the_account_and_consumes_the_transaction(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: GoogleStub,
) -> None:
    token = await _register(client)
    await _start_pending(client, token)

    resp = await _confirm(client, token)

    assert resp.status_code == 200
    body = resp.json()
    assert body["connected"] is True
    assert body["email"] == stub.email
    assert body["google_sub"] == stub.sub
    assert client.cookies.get(PENDING_COOKIE) is None
    assert await _account_count() == 1

    row = await _pending_row()
    assert row["status"] == "consumed"
    assert row["credentials_encrypted"] is None
    assert row["token_expires_at"] is None


async def test_new_account_without_a_refresh_token_is_refused_at_the_callback(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: GoogleStub,
) -> None:
    stub.omit_refresh_on_exchange = True
    token = await _register(client)

    resp = await _callback(client, state=_state_from(await _connect(client, token)))

    assert resp.status_code == 400
    assert resp.json()["detail"] == "refresh_token_required"
    assert client.cookies.get(OAUTH_BINDING_COOKIE) is None
    assert client.cookies.get(PENDING_COOKIE) is None
    assert await _pending_count() == 0
    assert await _account_count() == 0


async def test_reconnect_without_a_new_refresh_token_preserves_the_stored_one(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: GoogleStub,
) -> None:
    token = await _register(client)
    await _bind_fully(client, token)
    assert (await _stored_credentials())["refresh_token"] == stub.refresh_token

    stub.access_token = "reconnected-access-token"
    stub.omit_refresh_on_exchange = True
    _started, _raw = await _start_pending(client, token)
    resp = await _confirm(client, token)

    assert resp.status_code == 200
    stored = await _stored_credentials()
    assert stored["access_token"] == "reconnected-access-token"
    assert stored["refresh_token"] == stub.refresh_token
    assert await _account_count() == 1


async def test_reconnect_with_no_renewable_grant_is_refused_and_marks_failed(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: GoogleStub,
) -> None:
    """A reconnect must never consume the last refresh token it cannot replace."""
    token = await _register(client)
    async with async_session_factory() as session:
        me = (
            await session.execute(text("SELECT id, tenant_id FROM users"))
        ).mappings().one()
        await bind_or_reconnect_google_account(
            session,
            tenant_id=me["tenant_id"],
            user_id=me["id"],
            google_sub=stub.sub,
            email=stub.email,
            scopes=CONNECT_GRANTED,
            access_token="stored-access",
            refresh_token=None,
            expires_in=3600,
        )
    before = await _stored_credentials()

    stub.omit_refresh_on_exchange = True
    _started, _raw = await _start_pending(client, token)
    resp = await _confirm(client, token)

    assert resp.status_code == 400
    assert resp.json()["detail"] == "reconnect_required"
    row = await _pending_row()
    assert row["status"] == "failed"
    assert row["failure_reason"] == "reconnect_required"
    assert row["credentials_encrypted"] is None
    assert await _stored_credentials() == before


async def test_ownership_conflict_is_409_without_revoke_or_reassignment(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: GoogleStub,
) -> None:
    alice = await _register(client, "alice@example.com")
    stub.sub = "shared-sub"
    await _bind_fully(client, alice)
    before = await _stored_credentials()

    # Registration currently provisions one tenant per user. Create Bob in
    # Alice's tenant explicitly so this test exercises the documented
    # tenant-scoped ownership conflict rather than a valid cross-tenant link.
    async with async_session_factory() as session:
        alice_user = (
            await session.execute(
                select(User).where(User.email == "alice@example.com")
            )
        ).scalar_one()
        bob_user = User(
            tenant_id=alice_user.tenant_id,
            email="bob@example.com",
            password_hash=hash_password("password123"),
        )
        session.add(bob_user)
        await session.commit()
        bob = create_access_token(
            user_id=bob_user.id,
            tenant_id=bob_user.tenant_id,
        )
    stub.access_token = "bob-access-token"
    resp = await _callback(client, state=_state_from(await _connect(client, bob)))

    assert resp.status_code == 409
    assert resp.json()["detail"] == "google account is already linked to another user"
    assert client.cookies.get(OAUTH_BINDING_COOKIE) is None
    assert client.cookies.get(PENDING_COOKIE) is None
    assert stub.revoke_calls == []
    assert await _open_pending_count() == 0
    assert await _account_count() == 1
    assert await _stored_credentials() == before


async def test_conflict_during_binding_marks_pending_failed(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = await _register(client)
    await _start_pending(client, token)

    def _explode(*_: object, **__: object) -> object:
        raise GoogleAccountConflictError(
            "google account is already bound to another user in this tenant"
        )

    import kai.google.pending as pending

    monkeypatch.setattr(
        pending,
        "bind_or_reconnect_google_account_in_transaction",
        _explode,
    )

    resp = await _confirm(client, token)

    assert resp.status_code == 409
    assert resp.json()["detail"] == "google account is already linked to another user"
    row = await _pending_row()
    assert row["status"] == "failed"
    assert row["failure_reason"] == "ownership_conflict"
    assert row["credentials_encrypted"] is None
    assert await _account_count() == 0


async def test_partial_account_insert_rolls_back_before_pending_failure(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = await _register(client)
    await _start_pending(client, token)

    async def _insert_then_conflict(
        session: AsyncSession,
        *,
        tenant_id: UUID,
        user_id: UUID,
        google_sub: str,
        email: str,
        scopes: str,
        access_token: str,
        refresh_token: str | None,
        expires_in: int,
        now: datetime | None = None,
    ) -> None:
        account = GoogleAccount(
            tenant_id=tenant_id,
            user_id=user_id,
            google_sub=google_sub,
            email=email,
            scopes=scopes,
            credentials_encrypted="partial-write",
            token_expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
        )
        session.add(account)
        await session.flush()
        raise GoogleAccountConflictError("conflict after account insert")

    monkeypatch.setattr(
        pending_module,
        "bind_or_reconnect_google_account_in_transaction",
        _insert_then_conflict,
    )

    resp = await _confirm(client, token)

    assert resp.status_code == 409
    row = await _pending_row()
    assert row["status"] == "failed"
    assert row["credentials_encrypted"] is None
    assert await _account_count() == 0


async def test_expired_pending_is_410_and_loses_its_credentials(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: GoogleStub,
) -> None:
    token = await _register(client)
    _started, raw = await _start_pending(client, token)
    await _expire_pending()

    view = await _pending_view(client, token)

    assert view.status_code == 410
    assert view.json()["detail"] == "google authorization expired"
    row = await _pending_row()
    assert row["status"] == "expired"
    assert row["credentials_encrypted"] is None
    assert await _account_count() == 0

    # The same cookie cannot confirm an expired transaction either.
    client.cookies.set(PENDING_COOKIE, raw, path="/")
    confirmed = await _confirm(client, token)
    assert confirmed.status_code == 410
    assert await _account_count() == 0


async def test_cancel_is_local_and_clears_the_credentials(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: GoogleStub,
) -> None:
    token = await _register(client)
    _started, raw = await _start_pending(client, token)

    resp = await _cancel(client, token)

    assert resp.status_code == 200
    assert resp.json() == {"cancelled": True, "status": "cancelled"}
    assert client.cookies.get(PENDING_COOKIE) is None
    row = await _pending_row()
    assert row["status"] == "cancelled"
    assert row["credentials_encrypted"] is None
    assert stub.revoke_calls == []
    assert await _account_count() == 0

    # A cancelled transaction is terminal, even with the raw cookie.
    client.cookies.set(PENDING_COOKIE, raw, path="/")
    assert (await _confirm(client, token)).status_code == 404


async def test_consumed_transaction_cannot_be_replayed(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
) -> None:
    token = await _register(client)
    _started, raw = await _start_pending(client, token)
    assert (await _confirm(client, token)).status_code == 200

    client.cookies.set(PENDING_COOKIE, raw, path="/")
    replay = await _confirm(client, token)

    assert replay.status_code == 404
    assert await _account_count() == 1


async def test_transient_confirmation_failure_keeps_the_transaction_pending(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = await _register(client)
    await _start_pending(client, token)

    def _explode(*_: object, **__: object) -> None:
        raise RuntimeError("database went away")

    import kai.google.pending as pending

    monkeypatch.setattr(
        pending, "bind_or_reconnect_google_account_in_transaction", _explode
    )

    resp = await _confirm(client, token)

    assert resp.status_code == 503
    assert resp.json()["detail"] == "google account linking is temporarily unavailable"
    assert client.cookies.get(PENDING_COOKIE) is not None
    row = await _pending_row()
    assert row["status"] == "pending"
    assert row["credentials_encrypted"] is not None
    assert await _account_count() == 0


async def test_concurrent_confirmations_link_exactly_once(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
) -> None:
    token = await _register(client)
    _started, raw = await _start_pending(client, token)

    async with async_session_factory() as session:
        me = (
            await session.execute(text("SELECT id, tenant_id FROM users"))
        ).mappings().one()

    async def _attempt() -> str:
        async with async_session_factory() as session:
            try:
                account = await confirm_pending_transaction(
                    session,
                    tenant_id=me["tenant_id"],
                    user_id=me["id"],
                    raw_confirmation_token=raw,
                )
            except PendingNotPendingError as exc:
                return exc.status
            return str(account.id)

    results = await asyncio.gather(_attempt(), _attempt())

    assert sum(1 for result in results if result == "consumed") == 1
    assert await _account_count() == 1


# --------------------------------------------------------------------------- #
# Housekeeping
# --------------------------------------------------------------------------- #


async def test_purge_expires_pending_rows_and_deletes_old_terminal_ones(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
) -> None:
    token = await _register(client)
    _started, raw = await _start_pending(client, token)
    await _expire_pending()

    row = await _pending_row()
    assert row["status"] == "pending"
    assert row["credentials_encrypted"] is not None

    async with async_session_factory() as session:
        expired, deleted = await purge_pending_transactions(session)

    assert (expired, deleted) == (1, 0)
    row = await _pending_row()
    assert row["status"] == "expired"
    assert row["credentials_encrypted"] is None

    # Idempotent: a second run finds nothing to expire.
    async with async_session_factory() as session:
        assert await purge_pending_transactions(session) == (0, 0)

    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE google_oauth_pending SET updated_at = :old"
            ),
            {"old": datetime.now(UTC) - TERMINAL_RETENTION - timedelta(minutes=1)},
        )

    async with async_session_factory() as session:
        assert await purge_pending_transactions(session) == (0, 1)
    assert await _pending_count() == 0


async def test_purge_recovers_credentials_after_failure_cleanup_rollback(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = await _register(client)
    _started, raw = await _start_pending(client, token)
    async with async_session_factory() as session:
        user = (
            await session.execute(text("SELECT id, tenant_id FROM users"))
        ).mappings().one()

    session_factory = pending_module.async_session_factory

    @asynccontextmanager
    async def _fail_cleanup_commit():
        async with session_factory() as failure_session:
            async def _fail_commit() -> None:
                raise RuntimeError("injected cleanup commit failure")

            monkeypatch.setattr(failure_session, "commit", _fail_commit)
            yield failure_session

    monkeypatch.setattr(
        pending_module, "async_session_factory", lambda: _fail_cleanup_commit()
    )

    with pytest.raises(RuntimeError, match="injected cleanup commit failure"):
        async with async_session_factory() as session:
            await pending_module._mark_failed(
                session,
                confirmation_hash=hash_token(raw),
                tenant_id=user["tenant_id"],
                user_id=user["id"],
                reason="ownership_conflict",
            )

    row = await _pending_row()
    assert row["status"] == "pending"
    assert row["credentials_encrypted"] is not None

    await _expire_pending()
    async with async_session_factory() as session:
        assert await purge_pending_transactions(session) == (1, 0)

    row = await _pending_row()
    assert row["status"] == "expired"
    assert row["credentials_encrypted"] is None


# --------------------------------------------------------------------------- #
# Refresh / reconnect HTTP contract
# --------------------------------------------------------------------------- #


async def test_dead_refresh_grant_is_409_reconnect_required(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: GoogleStub,
) -> None:
    token = await _register(client)
    await _bind_fully(client, token)
    before = await _stored_credentials()
    await _expire_account_token()

    stub.token_status = 400
    stub.token_error = "invalid_grant"
    resp = await client.get(
        f"{API_PREFIX}/calendar/slots",
        params={"date": "2026-06-24", "timezone": "UTC"},
        headers=_bearer(token),
    )

    assert resp.status_code == 409
    assert resp.json()["detail"] == "google_reconnect_required"
    assert "invalid_grant" not in resp.text
    # No failed refresh may destroy the stored long-lived credential.
    assert await _stored_credentials() == before
    assert stub.revoke_calls == []


async def test_transient_refresh_failure_is_502_without_provider_detail(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: GoogleStub,
) -> None:
    token = await _register(client)
    await _bind_fully(client, token)
    before = await _stored_credentials()
    await _expire_account_token()

    stub.token_status = 503
    resp = await client.get(
        f"{API_PREFIX}/calendar/slots",
        params={"date": "2026-06-24", "timezone": "UTC"},
        headers=_bearer(token),
    )

    assert resp.status_code == 502
    assert resp.json()["detail"] == "google provider temporarily unavailable"
    assert "google token request failed" not in resp.text
    assert await _stored_credentials() == before
