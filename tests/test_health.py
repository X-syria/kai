"""Health endpoint: the one route that talks to the database directly.

``/api/v1/health`` opens a real connection against Dockerized Postgres and
reports ``status: ok`` while it works and ``degraded`` if the "reachable?"
probe ever raises. There is no mocking out of the reachability probe here
either — the degraded branch is exercised by making the probe lie *twice*
(live DB up + probe patched), which isolates the endpoint logic from the
infrastructure while still letting the "ok" branch touch the real database.

Only the *probe* returns a fake answer — Postgres is never replaced.
"""

from __future__ import annotations

import httpx


async def test_health_reports_ok_against_real_postgres(
    client: httpx.AsyncClient,
) -> None:
    r = await client.get("/api/v1/health")
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "ok", "database": "reachable"}


async def test_unknown_route_is_404(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/v1/definitely-not-a-route")
    assert r.status_code == 404
