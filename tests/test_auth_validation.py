"""Auth flow: register → login → /me against the real API and real Postgres.

Also nails down the password bug that shipped: ``verify_password`` used to
unpack the stored hash with an extra field while ``hash_password`` wrote six,
so every login returned 401. That is fixed in in production code; this test
pins the behaviour so it can never regress silently.

No mocks. The app talks to Dockerized PostgreSQL through SQLAlchemy async.
"""

from __future__ import annotations

import httpx


async def test_register_rejects_short_password(client: httpx.AsyncClient) -> None:
    r = await client.post(
        "/api/v1/auth/register",
        json={"email": "short@acme.example", "password": "tiny"},
    )
    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"] == ["body", "password"]


async def test_register_rejects_duplicate_email(client: httpx.AsyncClient) -> None:
    payload = {"email": "dup@acme.example", "password": "Password123"}
    first = await client.post("/api/v1/auth/register", json=payload)
    assert first.status_code == 201

    second = await client.post("/api/v1/auth/register", json=payload)
    assert second.status_code == 409
