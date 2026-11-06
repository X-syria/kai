"""The approval demo page is an exposure decision: mounted only when opted in.

``/demo`` is unauthenticated HTML that drives the ordinary authenticated API
from a browser. These tests prove the gate itself, without a database, a
service, or a browser: the route is absent by default, present only when
``DEMO_UI_ENABLED`` is true, and the documented API surface is unchanged.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from kai.config import Settings, get_settings
from kai.main import create_app

pytestmark = pytest.mark.no_db_unit

DEMO_PATH = "/demo"
RUNTIME_TASK_PATH = "/api/v1/runtime/tasks/00000000-0000-4000-8000-000000000000"
GOOGLE_LINKING_PATH = "/api/v1/auth/google/connect"


@contextlib.contextmanager
def demo_ui(value: str | None) -> Iterator[None]:
    """Set ``DEMO_UI_ENABLED`` for one block and restore the cached settings.

    ``create_app`` reads settings at call time, so the cache is cleared on both
    sides of the block: entering makes the new value visible to the app under
    construction, leaving prevents a stale cached ``Settings`` from leaking the
    demo flag into every later test.
    """
    previous = os.environ.get("DEMO_UI_ENABLED")
    if value is None:
        os.environ.pop("DEMO_UI_ENABLED", None)
    else:
        os.environ["DEMO_UI_ENABLED"] = value
    get_settings.cache_clear()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("DEMO_UI_ENABLED", None)
        else:
            os.environ["DEMO_UI_ENABLED"] = previous
        get_settings.cache_clear()


def _mounted_paths(routes: Any) -> set[str]:
    """Every mounted path, through FastAPI's included-router wrappers.

    Recent FastAPI versions keep an included router as a wrapper in
    ``app.routes`` instead of flattening its routes; older versions expose the
    routes directly. Both shapes are handled, so the assertion does not depend
    on a private layout that may change again.
    """
    paths: set[str] = set()
    for route in routes:
        path = getattr(route, "path", None)
        if isinstance(path, str) and path:
            paths.add(path)
        inner = getattr(route, "original_router", None)
        if inner is not None:
            paths |= _mounted_paths(inner.routes)
    return paths


async def _get(app: FastAPI, path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        return await client.get(path)


def test_demo_ui_is_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deployment that says nothing about the demo must not serve it."""
    monkeypatch.delenv("DEMO_UI_ENABLED", raising=False)
    get_settings.cache_clear()
    try:
        assert Settings().demo_ui_enabled is False
    finally:
        get_settings.cache_clear()


async def test_demo_route_is_not_registered_when_disabled() -> None:
    """Disabled means absent, not mounted-and-refusing."""
    with demo_ui(None):
        app = create_app()
        assert DEMO_PATH not in _mounted_paths(app.routes)
        response = await _get(app, DEMO_PATH)

    assert response.status_code == 404


async def test_demo_route_serves_the_page_when_enabled() -> None:
    """The explicit opt-in keeps the page exactly as it was."""
    with demo_ui("true"):
        app = create_app()
        assert DEMO_PATH in _mounted_paths(app.routes)
        response = await _get(app, DEMO_PATH)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-store"


async def test_the_gate_changes_no_documented_route() -> None:
    """Only ``/demo`` may appear; the documented API surface is untouched."""
    with demo_ui(None):
        disabled_app = create_app()
        disabled_paths = set(disabled_app.openapi()["paths"])
        disabled_mounted = _mounted_paths(disabled_app.routes)
    with demo_ui("true"):
        enabled_app = create_app()
        enabled_paths = set(enabled_app.openapi()["paths"])
        enabled_mounted = _mounted_paths(enabled_app.routes)

    assert enabled_paths == disabled_paths
    assert enabled_mounted - disabled_mounted == {DEMO_PATH}
    assert disabled_mounted - enabled_mounted == set()
    # The demo route is deliberately hidden from OpenAPI in both states.
    assert DEMO_PATH not in disabled_paths
    assert DEMO_PATH not in enabled_paths


async def test_runtime_still_requires_authentication_either_way() -> None:
    """The gate must not become an authentication bypass for the demo page."""
    with demo_ui(None):
        disabled_response = await _get(create_app(), RUNTIME_TASK_PATH)
    with demo_ui("true"):
        enabled_response = await _get(create_app(), RUNTIME_TASK_PATH)

    assert disabled_response.status_code == 401
    assert enabled_response.status_code == 401


def test_the_demo_gate_does_not_touch_the_google_linking_guard() -> None:
    """D37 stays fail-closed; the demo flag is not a second switch for it."""
    from kai.api.google import google_oauth_linking_guard

    with pytest.raises(HTTPException) as excinfo:
        google_oauth_linking_guard()

    assert excinfo.value.status_code == 503
