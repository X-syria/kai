"""D37: live Google account linking is fail-closed.

These tests never open a database or make a Google request. The production
routes are guarded unconditionally; the one opt-in test uses the shared
``google_settings`` fixture, which is the only place tests override the guard.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio

from kai.api import google as google_api
from kai.api.google import OAUTH_BINDING_COOKIE
from kai.auth.deps import get_current_user
from kai.config import get_settings
from kai.db.engine import get_session
from kai.main import app

API_PREFIX = "/api/v1"
pytestmark = pytest.mark.no_db_unit


@pytest_asyncio.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://testserver"
    ) as c:
        yield c


@pytest.fixture(autouse=True)
def _forbid_real_database() -> Iterator[None]:
    """Keep the guard tests off the database even if a bug lets them through."""

    async def _no_session() -> AsyncIterator[object]:
        yield object()

    app.dependency_overrides[get_session] = _no_session
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_session, None)


async def test_connect_is_blocked_without_building_an_authorization_url(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _must_not_build(**_: object) -> str:
        raise AssertionError("authorization URL must not be built while linking is closed")

    monkeypatch.setattr(google_api, "build_authorization_url", _must_not_build)

    resp = await client.get(f"{API_PREFIX}/auth/google/connect")

    assert resp.status_code == 503
    assert resp.json()["detail"] == "google account linking is temporarily disabled"
    assert "authorization_url" not in resp.json()
    assert "set-cookie" not in resp.headers


async def test_authenticated_connect_is_still_blocked(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A legitimate caller must not get a URL while linking is closed."""

    def _must_not_build(**_: object) -> str:
        raise AssertionError("authorization URL must not be built while linking is closed")

    monkeypatch.setattr(google_api, "build_authorization_url", _must_not_build)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=uuid4(), tenant_id=uuid4()
    )
    try:
        resp = await client.get(f"{API_PREFIX}/auth/google/connect")
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert resp.status_code == 503
    assert "authorization_url" not in resp.json()


async def test_callback_is_blocked_before_exchange_or_binding(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _must_not_exchange(*_: object, **__: object) -> None:
        raise AssertionError("code exchange must not run while linking is closed")

    async def _must_not_bind(*_: object, **__: object) -> None:
        raise AssertionError(
            "pending authorization must not be created while linking is closed"
        )

    monkeypatch.setattr(google_api.client, "exchange_code", _must_not_exchange)
    monkeypatch.setattr(
        google_api, "create_pending_transaction", _must_not_bind
    )

    # A forged state plus a binding cookie, and Google's own error redirect,
    # must not get a different response that would reveal the endpoint went
    # further than the guard.
    secret = "b" * 43
    state = "forged-state-value"
    client.cookies.set(OAUTH_BINDING_COOKIE, secret, path="/")

    resp = await client.get(
        f"{API_PREFIX}/auth/google/callback",
        params={"state": state, "code": "auth-code"},
    )
    assert resp.status_code == 503

    denied = await client.get(
        f"{API_PREFIX}/auth/google/callback",
        params={"state": state, "error": "access_denied"},
    )
    assert denied.status_code == 503
    assert "set-cookie" not in resp.headers
    assert "set-cookie" not in denied.headers


async def test_environment_variable_cannot_enable_linking(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """There is deliberately no env/settings switch to re-open the flow."""

    monkeypatch.setenv("GOOGLE_OAUTH_LINKING_ENABLED", "true")
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "inherited-client-id")
    get_settings.cache_clear()
    try:
        resp = await client.get(f"{API_PREFIX}/auth/google/connect")
    finally:
        get_settings.cache_clear()

    assert resp.status_code == 503
    assert "authorization_url" not in resp.json()


@pytest.mark.parametrize(
    "path",
    ["/auth/google/accounts", "/calendar/slots"],
)
async def test_provider_paths_are_not_guarded(
    client: httpx.AsyncClient, path: str
) -> None:
    """Account listing and Calendar token use must not inherit the link guard."""

    resp = await client.get(f"{API_PREFIX}{path}")

    # Authentication answers first; a 503 here would mean the guard leaked onto
    # a provider/account path that must remain usable.
    assert resp.status_code == 401


async def test_explicit_test_override_enables_connect_without_network(
    client: httpx.AsyncClient,
    google_settings: None,
) -> None:
    """The shared fixture is the explicit, isolated test-only opt-in."""

    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=uuid4(), tenant_id=uuid4()
    )
    try:
        resp = await client.get(f"{API_PREFIX}/auth/google/connect")
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert resp.status_code == 200
    assert "authorization_url" in resp.json()
    assert resp.json()["authorization_url"].startswith(
        "https://accounts.google.com/o/oauth2/v2/auth?"
    )
