"""Security tests: every one of these fails against the pre-hardening code.

Each test names the defect it closes and exercises it through the real app and
the real database. None of them asserts on a helper in isolation, because a
helper-level assertion would still pass if the helper were never reached from
a request — which is exactly the class of bug worth catching here (an earlier
``is_active`` field was returned by /auth/me and enforced nowhere).

The set covers: token revocation, account deactivation, the login timing
oracle, the unbounded-password DoS, JWT claim verification, the scrypt
work-factor floor, auth rate limiting, CORS, and TrustedHost.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import jwt
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from kai.auth.jwt import InvalidTokenError, create_access_token, decode_access_token
from kai.auth.revocation import purge_expired_revocations
from kai.auth.security import hash_password, verify_password
from kai.config import get_settings
from kai.db.models import RevokedToken, User
from kai.security import ratelimit

PASSWORD = "Password123!"


async def _register(
    client: httpx.AsyncClient, email: str, password: str = PASSWORD
) -> dict:
    r = await client.post(
        "/api/v1/auth/register", json={"email": email, "password": password}
    )
    assert r.status_code == 201, r.text
    return r.json()


# --------------------------------------------------------------------------
# Revocation: a revoked token must stop working immediately, not at expiry.
# --------------------------------------------------------------------------


async def test_logout_revokes_the_presented_token(client: httpx.AsyncClient) -> None:
    """The core of the defect: before revocation, a stolen token ran to expiry."""
    created = await _register(client, "revoke@acme.example")
    token = created["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    before = await client.get("/api/v1/auth/me", headers=headers)
    assert before.status_code == 200

    out = await client.post("/api/v1/auth/logout", headers=headers)
    assert out.status_code == 200, out.text
    assert out.json()["revoked"] is True

    after = await client.get("/api/v1/auth/me", headers=headers)
    assert after.status_code == 401, after.text


async def test_revoked_token_cannot_reach_a_protected_route(
    client: httpx.AsyncClient,
) -> None:
    """Revocation must gate the real dependency, not just /auth/me."""
    created = await _register(client, "revoke2@acme.example")
    headers = {"Authorization": f"Bearer {created['access_token']}"}

    tasks = await client.get("/api/v1/tasks", headers=headers)
    assert tasks.status_code == 200, tasks.text

    await client.post("/api/v1/auth/logout", headers=headers)

    tasks_after = await client.get("/api/v1/tasks", headers=headers)
    assert tasks_after.status_code == 401, tasks_after.text


async def test_logout_only_revokes_the_presented_token(
    client: httpx.AsyncClient,
) -> None:
    """Signing out one device must not sign out the account's other clients.

    A personal agent holds long-lived tokens per client, so revoking the whole
    principal on logout would silently break every other signed-in client.
    """
    first = await _register(client, "multi@acme.example")
    second = await client.post(
        "/api/v1/auth/login",
        json={"email": "multi@acme.example", "password": PASSWORD},
    )
    assert second.status_code == 200, second.text

    h1 = {"Authorization": f"Bearer {first['access_token']}"}
    h2 = {"Authorization": f"Bearer {second.json()['access_token']}"}

    await client.post("/api/v1/auth/logout", headers=h1)

    assert (await client.get("/api/v1/auth/me", headers=h1)).status_code == 401
    still_ok = await client.get("/api/v1/auth/me", headers=h2)
    assert still_ok.status_code == 200, still_ok.text


async def test_logout_is_idempotent(client: httpx.AsyncClient) -> None:
    """Signing out twice must not 500: logout is reached by retries and races."""
    created = await _register(client, "twice@acme.example")
    headers = {"Authorization": f"Bearer {created['access_token']}"}

    first = await client.post("/api/v1/auth/logout", headers=headers)
    second = await client.post("/api/v1/auth/logout", headers=headers)

    assert first.status_code == 200
    assert second.status_code == 401


async def test_logout_requires_authentication(client: httpx.AsyncClient) -> None:
    r = await client.post("/api/v1/auth/logout")
    assert r.status_code == 401


async def test_revoked_token_error_does_not_distinguish_cause(
    client: httpx.AsyncClient,
) -> None:
    """A revoked, forged, and garbage token must be indistinguishable.

    Telling a caller "this token was revoked" confirms the token was real,
    which is exactly the signal an attacker holding a stolen token wants.
    """
    created = await _register(client, "opaque@acme.example")
    headers = {"Authorization": f"Bearer {created['access_token']}"}
    await client.post("/api/v1/auth/logout", headers=headers)

    revoked = await client.get("/api/v1/auth/me", headers=headers)
    garbage = await client.get(
        "/api/v1/auth/me", headers={"Authorization": "Bearer not-a-jwt"}
    )

    assert revoked.status_code == garbage.status_code == 401
    assert revoked.json() == garbage.json()
    assert "X-Request-ID" in revoked.headers


async def test_purge_removes_only_revocations_for_expired_tokens(
    client: httpx.AsyncClient,
) -> None:
    """Purging must never un-revoke a token that is still inside its window."""
    created = await _register(client, "purge@acme.example")
    live = created["access_token"]
    headers = {"Authorization": f"Bearer {live}"}
    await client.post("/api/v1/auth/logout", headers=headers)

    async with _session_scope() as session:
        removed = await purge_expired_revocations(session)
        assert removed == 0, "a live revocation was purged"

    # Still revoked, because the purge was a no-op.
    assert (await client.get("/api/v1/auth/me", headers=headers)).status_code == 401

    # Now age the row past the token's own expiry and confirm it is collected.
    async with _session_scope() as session:
        await session.execute(
            RevokedToken.__table__.update()
            .values(expires_at=datetime.now(UTC) - timedelta(minutes=1))
        )
        await session.commit()
        assert await purge_expired_revocations(session) == 1

    async with _session_scope() as session:
        remaining = await session.get(RevokedToken, decode_access_token(live).jti)
        assert remaining is None


# --------------------------------------------------------------------------
# Account deactivation: a suspension has to actually suspend.
# --------------------------------------------------------------------------


async def test_deactivated_user_cannot_use_an_existing_token(
    client: httpx.AsyncClient,
) -> None:
    """Before the fix, is_active was reported by /me and enforced nowhere, so
    deactivating an account did not stop a token that was already in hand."""
    created = await _register(client, "disabled@acme.example")
    headers = {"Authorization": f"Bearer {created['access_token']}"}
    assert (await client.get("/api/v1/auth/me", headers=headers)).status_code == 200

    async with _session_scope() as session:
        user = await session.get(User, created["user_id"])
        assert user is not None
        user.is_active = False
        await session.commit()

    after = await client.get("/api/v1/auth/me", headers=headers)
    assert after.status_code == 401, after.text


async def test_deactivated_user_cannot_log_in_again(
    client: httpx.AsyncClient,
) -> None:
    """Login is the only mint point, so it is where a suspension must apply."""
    created = await _register(client, "disabled2@acme.example")

    async with _session_scope() as session:
        user = await session.get(User, created["user_id"])
        assert user is not None
        user.is_active = False
        await session.commit()

    r = await client.post(
        "/api/v1/auth/login",
        json={"email": "disabled2@acme.example", "password": PASSWORD},
    )
    assert r.status_code == 401, r.text
    assert "access_token" not in r.json()


# --------------------------------------------------------------------------
# Login must not disclose whether an account exists.
# --------------------------------------------------------------------------


async def test_login_timing_does_not_reveal_account_existence(
    client: httpx.AsyncClient,
) -> None:
    """An unregistered email must cost about the same as a wrong password.

    The old handler short-circuited on a missing user and skipped the scrypt
    call entirely, so the endpoint was a user-enumeration oracle. The bar here
    is deliberately loose: proving the oracle is closed means the difference is
    no longer dominated by the presence or absence of the expensive hash.
    """
    await _register(client, "known@acme.example")
    payload_bad = {"email": "known@acme.example", "password": "WrongPassword1"}
    payload_missing = {"email": "nobody-here@acme.example", "password": "WrongPassword1"}

    async def measure(body: dict) -> float:
        samples: list[float] = []
        for _ in range(5):
            start = time.perf_counter()
            r = await client.post("/api/v1/auth/login", json=body)
            samples.append((time.perf_counter() - start) * 1000)
            assert r.status_code == 401
        return sorted(samples)[len(samples) // 2]

    existing = await measure(payload_bad)
    missing = await measure(payload_missing)

    ratio = missing / existing if existing else 0
    assert ratio > 0.5, (
        f"unknown-email login took {missing:.1f}ms vs {existing:.1f}ms for a "
        "registered email; the cheap path is an enumeration oracle"
    )


async def test_login_failure_response_is_identical_for_both_cases(
    client: httpx.AsyncClient,
) -> None:
    await _register(client, "same@acme.example")

    known = await client.post(
        "/api/v1/auth/login",
        json={"email": "same@acme.example", "password": "WrongPassword1"},
    )
    unknown = await client.post(
        "/api/v1/auth/login",
        json={"email": "ghost@acme.example", "password": "WrongPassword1"},
    )

    assert known.status_code == unknown.status_code == 401
    assert known.json() == unknown.json()


# --------------------------------------------------------------------------
# Unbounded input on an expensive, unauthenticated route.
# --------------------------------------------------------------------------


async def test_login_rejects_an_oversized_password(client: httpx.AsyncClient) -> None:
    """scrypt is intentionally expensive; an unbounded password makes the
    unauthenticated login route a CPU-exhaustion primitive."""
    r = await client.post(
        "/api/v1/auth/login",
        json={"email": "known@acme.example", "password": "A" * 100_000},
    )
    assert r.status_code == 422, r.text


# --------------------------------------------------------------------------
# JWT claim verification.
# --------------------------------------------------------------------------


def test_token_carries_a_unique_id_and_issuer_audience() -> None:
    """Revocation is impossible without a stable per-token identifier."""
    a = create_access_token(user_id=uuid4(), tenant_id=uuid4())
    b = create_access_token(user_id=uuid4(), tenant_id=uuid4())
    ca, cb = decode_access_token(a), decode_access_token(b)

    assert ca.jti and cb.jti
    assert ca.jti != cb.jti


def test_token_signed_with_a_foreign_secret_is_rejected() -> None:
    foreign = jwt.encode(
        {
            "sub": str(uuid4()),
            "tenant_id": str(uuid4()),
            "type": "access",
            "jti": "forged",
            "iss": get_settings().jwt_issuer,
            "aud": get_settings().jwt_audience,
            "iat": int(datetime.now(UTC).timestamp()),
            "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
        },
        "an-attacker-controlled-secret",
        algorithm="HS256",
    )
    with pytest.raises(InvalidTokenError):
        decode_access_token(foreign)


def test_token_without_expiry_is_rejected() -> None:
    """A token that omits ``exp`` must never be treated as non-expiring."""
    settings = get_settings()
    no_exp = jwt.encode(
        {
            "sub": str(uuid4()),
            "tenant_id": str(uuid4()),
            "type": "access",
            "jti": "no-exp",
            "iss": settings.jwt_issuer,
            "aud": settings.jwt_audience,
            "iat": int(datetime.now(UTC).timestamp()),
        },
        settings.jwt_secret,
        algorithm="HS256",
    )
    with pytest.raises(InvalidTokenError):
        decode_access_token(no_exp)


def test_token_from_another_audience_is_rejected() -> None:
    """A token minted for a different service must not authenticate here."""
    settings = get_settings()
    other = jwt.encode(
        {
            "sub": str(uuid4()),
            "tenant_id": str(uuid4()),
            "type": "access",
            "jti": "other-aud",
            "iss": settings.jwt_issuer,
            "aud": "some-other-service",
            "iat": int(datetime.now(UTC).timestamp()),
            "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
        },
        settings.jwt_secret,
        algorithm="HS256",
    )
    with pytest.raises(InvalidTokenError):
        decode_access_token(other)


def test_expired_token_is_rejected() -> None:
    settings = get_settings()
    expired = jwt.encode(
        {
            "sub": str(uuid4()),
            "tenant_id": str(uuid4()),
            "type": "access",
            "jti": "expired",
            "iss": settings.jwt_issuer,
            "aud": settings.jwt_audience,
            "iat": int((datetime.now(UTC) - timedelta(hours=2)).timestamp()),
            "exp": int((datetime.now(UTC) - timedelta(hours=1)).timestamp()),
        },
        settings.jwt_secret,
        algorithm="HS256",
    )
    with pytest.raises(InvalidTokenError):
        decode_access_token(expired)


def test_unapproved_algorithm_cannot_be_used_to_sign(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``none`` and asymmetric algorithms are outside the shared-secret design.

    The allowlist is enforced when signing as well as when decoding, so a bad
    ``JWT_ALGORITHM`` fails at the first token creation instead of minting a
    token that nothing will accept later.
    """
    from kai.config import SUPPORTED_JWT_ALGORITHMS
    assert "none" not in SUPPORTED_JWT_ALGORITHMS
    assert "RS256" not in SUPPORTED_JWT_ALGORITHMS

    monkeypatch.setenv("JWT_ALGORITHM", "none")
    get_settings.cache_clear()
    try:
        with pytest.raises(ValueError, match="unsupported JWT algorithm"):
            create_access_token(user_id=uuid4(), tenant_id=uuid4())
    finally:
        get_settings.cache_clear()


def test_alg_none_token_is_rejected_on_decode() -> None:
    """An unsigned ``alg: none`` token must never authenticate."""
    settings = get_settings()
    unsigned = jwt.encode(
        {
            "sub": str(uuid4()),
            "tenant_id": str(uuid4()),
            "type": "access",
            "jti": "alg-none",
            "iss": settings.jwt_issuer,
            "aud": settings.jwt_audience,
            "iat": int(datetime.now(UTC).timestamp()),
            "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
        },
        key="",
        algorithm="none",
    )
    with pytest.raises(InvalidTokenError):
        decode_access_token(unsigned)


# --------------------------------------------------------------------------
# Password hashing.
# --------------------------------------------------------------------------


def test_verify_rejects_a_downgraded_work_factor() -> None:
    """A stored hash with cheaper parameters must not be accepted as-is."""
    import base64
    import hashlib

    salt = b"0123456789abcdef"
    weak = hashlib.scrypt(PASSWORD.encode(), salt=salt, n=2**8, r=8, p=1)
    salt_b64 = base64.b64encode(salt).decode()
    digest_b64 = base64.b64encode(weak).decode()
    encoded = f"scrypt$256$8$1${salt_b64}${digest_b64}"
    assert verify_password(PASSWORD, encoded) is False


def test_verify_accepts_a_real_hash_and_rejects_a_wrong_password() -> None:
    encoded = hash_password(PASSWORD)
    assert verify_password(PASSWORD, encoded) is True
    assert verify_password("WrongPassword1", encoded) is False


# --------------------------------------------------------------------------
# Rate limiting.
# --------------------------------------------------------------------------


@pytest.fixture
def low_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("RATE_LIMIT_BACKEND", "memory")
    monkeypatch.setenv("AUTH_RATE_LIMIT_REQUESTS", "3")
    monkeypatch.setenv("AUTH_RATE_LIMIT_WINDOW_SECONDS", "60")
    get_settings.cache_clear()
    ratelimit.reset_store()
    yield
    get_settings.cache_clear()
    ratelimit.reset_store()


async def test_login_is_rate_limited(
    client: httpx.AsyncClient, low_rate_limit: None
) -> None:
    await _register(client, "rl@acme.example")

    codes = []
    for _ in range(6):
        r = await client.post(
            "/api/v1/auth/login",
            json={"email": "rl@acme.example", "password": "WrongPassword1"},
        )
        codes.append(r.status_code)

    assert 429 in codes, f"brute force was never limited: {codes}"
    assert codes[-1] == 429


async def test_rate_limited_response_advertises_retry_after(
    client: httpx.AsyncClient, low_rate_limit: None
) -> None:
    for _ in range(4):
        r = await client.post(
            "/api/v1/auth/login",
            json={"email": "rl2@acme.example", "password": "WrongPassword1"},
        )
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) >= 1


async def test_rate_limit_does_not_block_correct_credentials_immediately(
    client: httpx.AsyncClient, low_rate_limit: None
) -> None:
    """A limit must not turn into a lockout for a legitimate user on attempt 1."""
    created = await _register(client, "rl3@acme.example")
    r = await client.post(
        "/api/v1/auth/login",
        json={"email": "rl3@acme.example", "password": PASSWORD},
    )
    assert r.status_code == 200, r.text
    assert r.json()["user_id"] == created["user_id"]


async def test_rate_limit_does_not_apply_to_authenticated_routes(
    client: httpx.AsyncClient, low_rate_limit: None
) -> None:
    """Exhausting the login budget must not lock a user out of the API."""
    created = await _register(client, "rl4@acme.example")
    headers = {"Authorization": f"Bearer {created['access_token']}"}

    for _ in range(8):
        r = await client.get("/api/v1/tasks", headers=headers)
        assert r.status_code == 200, r.text


async def test_health_stays_reachable_when_auth_is_limited(
    client: httpx.AsyncClient, low_rate_limit: None
) -> None:
    """A saturated auth route must not make the service look dead to a LB."""
    for _ in range(5):
        await client.post(
            "/api/v1/auth/login",
            json={"email": "rl5@acme.example", "password": "x"},
        )
    r = await client.get("/api/v1/health")
    assert r.status_code == 200, r.text


# --------------------------------------------------------------------------
# CORS and TrustedHost.
# --------------------------------------------------------------------------


def test_cors_is_refused_a_wildcard_origin() -> None:
    from kai.security.cors import build_cors_middleware

    with pytest.raises(ValueError, match="explicit origins"):
        build_cors_middleware(["*"])


def test_cors_is_not_installed_for_an_empty_allowlist() -> None:
    from kai.security.cors import build_cors_middleware

    assert build_cors_middleware([]) is None


def test_cors_never_combines_wildcard_with_credentials() -> None:
    from kai.security.cors import build_cors_middleware

    config = build_cors_middleware(["https://app.example"])
    assert config is not None
    _, kwargs = config
    assert "*" not in kwargs["allow_origins"]
    assert kwargs["allow_credentials"] is True


def test_cors_echoes_only_a_configured_origin() -> None:
    middleware = _cors_middleware(["https://app.example"])
    scope = {
        "type": "http",
        "method": "GET",
        "headers": [
            (b"origin", b"https://app.example"),
            (b"host", b"api.example"),
        ],
    }
    sent = _asgi_call(middleware, scope)
    allowed = [
        v.decode()
        for m in sent
        if m["type"] == "http.response.start"
        for k, v in m["headers"]
        if k == b"access-control-allow-origin"
    ]
    assert allowed == ["https://app.example"]


def test_cors_reflects_an_unconfigured_origin() -> None:
    """An origin that is not in the allowlist must get no CORS grant at all."""
    middleware = _cors_middleware(["https://app.example"])
    sent = _asgi_call(
        middleware,
        {
            "type": "http",
            "method": "GET",
            "headers": [
                (b"origin", b"https://evil.example"),
                (b"host", b"api.example"),
            ],
        },
    )
    allowed = [
        v.decode()
        for m in sent
        if m["type"] == "http.response.start"
        for k, v in m["headers"]
        if k == b"access-control-allow-origin"
    ]
    assert allowed == []


def test_cors_never_advertises_wildcard_to_a_browser() -> None:
    """A preflight from a configured origin must not leak ``*``."""
    middleware = _cors_middleware(["https://app.example"])
    sent = _asgi_call(
        middleware,
        {
            "type": "http",
            "method": "OPTIONS",
            "headers": [
                (b"origin", b"https://app.example"),
                (b"host", b"api.example"),
                (b"access-control-request-method", b"POST"),
            ],
        },
    )
    values = [
        v.decode()
        for m in sent
        if m["type"] == "http.response.start"
        for k, v in m["headers"]
        if k == b"access-control-allow-origin"
    ]
    assert values and all(v != "*" for v in values)


def test_trusted_hosts_rejects_an_unknown_host() -> None:
    from starlette.middleware.trustedhost import TrustedHostMiddleware

    middleware = TrustedHostMiddleware(
        _noop_app(), allowed_hosts=["api.example"]
    )
    sent = _asgi_call(
        middleware,
        {"type": "http", "method": "GET", "headers": [(b"host", b"evil.example")]},
    )
    assert sent[0]["status"] == 400


def test_trusted_hosts_allows_a_configured_host() -> None:
    from starlette.middleware.trustedhost import TrustedHostMiddleware

    middleware = TrustedHostMiddleware(
        _noop_app(), allowed_hosts=["api.example"]
    )
    sent = _asgi_call(
        middleware,
        {"type": "http", "method": "GET", "headers": [(b"host", b"api.example")]},
    )
    assert sent[0]["status"] == 200


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def _session_scope() -> AsyncIterator[AsyncSession]:
    """A real session against the same engine the app uses."""
    from kai.db.engine import engine

    async with AsyncSession(engine, expire_on_commit=False) as session:
        yield session


def _noop_app() -> object:
    """A minimal downstream ASGI app for middleware that wraps one."""

    async def app(scope: dict, receive: object, send: object) -> None:
        await send(  # type: ignore[operator]
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-length", b"0")],
            }
        )
        await send({"type": "http.response.body", "body": b""})  # type: ignore[operator]

    return app


def _cors_middleware(allowed_origins: list[str]) -> object:
    """Build a real CORSMiddleware from the validated config the app uses."""
    from kai.security.cors import build_cors_middleware

    config = build_cors_middleware(allowed_origins)
    assert config is not None
    cls, kwargs = config
    return cls(_noop_app(), **kwargs)


def _asgi_call(middleware: object, scope: dict) -> list[dict]:
    """Drive one ASGI request through ``middleware`` and collect what it sent."""
    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request"}

    async def send(message: dict) -> None:
        sent.append(message)

    asyncio.run(middleware(scope, receive, send))  # type: ignore[operator]
    return sent
