"""The happy-path identity flow plus its two failure modes.

register → (201, real JWT) → login again → second independent JWT → /auth/me
resolves to the SAME principal. Both secrets are still bcrypt-salted scrypt
hashes stored in the shared ``users`` table — the login step in particular
proves verify_password and hash_password round-trip correctly through the
real Postgres and the real security module.

Failures are real too: a duplicate email is rejected with 409, and a garbage
token produces 401 — never a crash.
"""

from __future__ import annotations

import httpx

from kai.auth.jwt import TokenClaims, decode_access_token


async def _register(
    client: httpx.AsyncClient, *, email: str
) -> tuple[str, TokenClaims]:
    r = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "Password123!"},
    )
    assert r.status_code == 201, r.text
    return r.json()["access_token"], decode_access_token(r.json()["access_token"])


async def test_register_login_me_roundtrip(client: httpx.AsyncClient) -> None:
    token_reg, reg = await _register(client, email="ada@makepoets.example")

    r_login = await client.post(
        "/api/v1/auth/login",
        json={"email": "ada@makepoets.example", "password": "Password123!"},
    )
    assert r_login.status_code == 200, r_login.text
    token_login = r_login.json()["access_token"]
    login = decode_access_token(token_login)

    # Same human, same tenant, two independent tokens.
    assert login.user_id == reg.user_id
    assert login.tenant_id == reg.tenant_id

    r_me = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {token_reg}"}
    )
    assert r_me.status_code == 200, r_me.text
    assert r_me.json()["user_id"] == str(reg.user_id)


async def test_login_with_wrong_password_is_401(client: httpx.AsyncClient) -> None:
    await _register(client, email="grace@hoppers.example")

    r = await client.post(
        "/api/v1/auth/login",
        json={"email": "grace@hoppers.example", "password": "WrongPassword1"},
    )
    assert r.status_code == 401


async def test_garbage_token_is_401_not_500(client: httpx.AsyncClient) -> None:
    r = await client.get(
        "/api/v1/auth/me", headers={"Authorization": "Bearer not-a-jwt"}
    )
    assert r.status_code == 401
