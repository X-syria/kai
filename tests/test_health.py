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
import pytest

from kai.db.engine import CheckDatabaseError


async def _raise_unreachable() -> None:
    """Stand in for a reachability probe that cannot get an answer."""
    raise CheckDatabaseError("database unreachable")


async def test_health_reports_ok_against_real_postgres(
    client: httpx.AsyncClient,
) -> None:
    r = await client.get("/api/v1/health")
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "ok", "database": "reachable"}


async def test_unknown_route_is_404(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/v1/definitely-not-a-route")
    assert r.status_code == 404


async def test_degraded_still_answers_http_200(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A degraded service must keep answering 200, and that must be pinned.

    ``/api/v1/health`` returns 200 in both branches and puts the verdict in the
    body. That is deliberate: it is a probe, and failing it on a database blip
    would turn one dependency's outage into a restart of every replica.

    The invariant is load-bearing for things outside this repository -- Compose's
    healthcheck only reads the status code, so a 503 here would restart a healthy
    process, and any status-code-only monitor would report a database outage as
    "the API is broken". It is asserted explicitly because the degraded branch is
    otherwise only reachable by breaking infrastructure, so nothing else in the
    suite holds it in place.
    """
    monkeypatch.setattr(
        "kai.api.health.check_database_reachable",
        _raise_unreachable,
    )
    r = await client.get("/api/v1/health")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "degraded"
    assert body["database"] == "unreachable"
