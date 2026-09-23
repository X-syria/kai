"""Tenant isolation — the property that justifies the entire schema.

Acme and Globex share ONE Dockerized Postgres: one ``tenants`` table, one
``users`` table. What keeps them apart is *not* separate databases or funny
primary keys alone — it's that every row carries a ``tenant_id`` and every
lookup is scoped by ``WHERE tenant_id = ?``. That predicate lives in one
place, ``get_user_for_tenant``, and the whole app funnels through it. The
database enforces it too, via the composite ``(tenant_id, id)`` primary key.

These tests prove that property against the real engine, the real Docker
Postgres, no mocks, no SQLite, no overrides.
"""

from __future__ import annotations

import httpx

from kai.auth.jwt import TokenClaims, decode_access_token
from kai.db.engine import async_session_factory
from kai.db.scoping import get_user_for_tenant


async def _register(
    client: httpx.AsyncClient, *, email: str
) -> tuple[str, TokenClaims]:
    """Register a fresh tenant/user pair, return (raw JWT, decoded claims)."""
    r = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "Password123!"},
    )
    assert r.status_code == 201, r.text
    token = r.json()["access_token"]
    claims = decode_access_token(token)
    return token, claims


async def test_distinct_tenants_get_distinct_principal_tokens(
    client: httpx.AsyncClient,
) -> None:
    """The two companies occupy the same tables but never the same identity."""
    _, acme = await _register(client, email="acme@corp.example")
    _, globex = await _register(client, email="globex@corp.example")

    assert acme.tenant_id != globex.tenant_id
    assert acme.user_id != globex.user_id


async def test_scoping_returns_none_for_a_foreign_tenant_row(
    client: httpx.AsyncClient,
) -> None:
    """A user_id inside the wrong tenant's WHERE clause simply doesn't exist.

    Even though the Globex row physically lives in the shared ``users`` table,
    asking for it through Acme's tenant id must return ``None`` — not a row,
    not an error. That None *is* the isolation.
    """
    _, acme = await _register(client, email="acme@corp.example")
    _, globex = await _register(client, email="globex@corp.example")

    async with async_session_factory() as session:
        found = await get_user_for_tenant(
            session, tenant_id=acme.tenant_id, user_id=globex.user_id
        )
        assert found is None

        # Sanity: the identical id DOES resolve under its own tenant.
        owner = await get_user_for_tenant(
            session, tenant_id=globex.tenant_id, user_id=globex.user_id
        )
        assert owner is not None


async def test_me_never_leaks_a_sibling_tenants_user(
    client: httpx.AsyncClient,
) -> None:
    """/auth/me only ever reports the caller's own tenant and user."""
    token_a, acme = await _register(client, email="acme@corp.example")
    await _register(client, email="globex@corp.example")

    me = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {token_a}"}
    )
    assert me.status_code == 200, me.text
    body = me.json()
    assert body["tenant_id"] == str(acme.tenant_id)
    assert body["user_id"] == str(acme.user_id)
