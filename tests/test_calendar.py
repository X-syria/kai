"""Google Calendar vertical slice tests.

Only Google's network is isolated: the app talks to PostgreSQL and decrypts
real stored credentials, while ``oauth2.googleapis.com`` /
``www.googleapis.com`` / ``www.googleapis.com/calendar/*`` are served by
``httpx.MockTransport`` through the app dependency override.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Final
from urllib.parse import parse_qs, parse_qsl, urlparse
from zoneinfo import ZoneInfo

import httpx
import pytest
import pytest_asyncio

from kai.google.deps import get_google_http_client
from kai.main import app

API_PREFIX: Final = "/api/v1"

CALENDAR_EVENTS_SCOPE = "https://www.googleapis.com/auth/calendar.events"
BERLIN = "Europe/Berlin"
TEST_DAY = "2026-06-24"


def _dt(
    day: str = TEST_DAY,
    time_of_day: str = "10:00",
    *,
    zone: str = BERLIN,
) -> datetime:
    y, m, d = (int(part) for part in day.split("-"))
    hour, minute = (int(part) for part in time_of_day.split(":"))
    return datetime(y, m, d, hour, minute, tzinfo=ZoneInfo(zone))


def timed_event(
    *,
    event_id: str,
    summary: str,
    start: datetime,
    end: datetime,
    timezone: str = BERLIN,
    attendees: tuple[str, ...] = (),
) -> dict:
    return {
        "id": event_id,
        "summary": summary,
        "start": {"dateTime": start.isoformat(), "timeZone": timezone},
        "end": {"dateTime": end.isoformat(), "timeZone": timezone},
        "attendees": [{"email": email} for email in attendees],
    }


class CalendarStub:
    """Deterministic stand-in for Google's OAuth + Calendar transport."""

    def __init__(self) -> None:
        self.access_token = "test-access-token"
        self.refresh_token = "test-refresh-token"
        self.sub = "google-sub-calendar"
        self.email = "alice@example.com"
        self.email_verified = True
        self.token_status = 200
        self.userinfo_status = 200
        self.events_status = 200
        self.create_status = 200
        self.read_status = 200
        self.events_payload: dict = {"items": []}
        self.events_text: str | None = None
        self.pages: list[dict] | None = None
        self.readback: dict | None = None
        self.created_id = "new-event-1"
        self.create_payload: dict | None = None
        self.calendar_calls: list[httpx.Request] = []
        self.create_body: object | None = None
        self.last_created: dict | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token" and request.url.host == "oauth2.googleapis.com":
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "invalid_grant"})
            form = dict(parse_qsl(request.content.decode("utf-8")))
            if form.get("grant_type") == "refresh_token":
                return httpx.Response(200, json={"access_token": "refreshed", "expires_in": 3599})
            return httpx.Response(
                200,
                json={
                    "access_token": self.access_token,
                    "refresh_token": self.refresh_token,
                    "expires_in": 3599,
                },
            )
        if (
            request.url.path == "/oauth2/v3/userinfo"
            and request.url.host == "www.googleapis.com"
        ):
            if self.userinfo_status != 200:
                return httpx.Response(self.userinfo_status)
            return httpx.Response(
                200,
                json={
                    "sub": self.sub,
                    "email": self.email,
                    "email_verified": self.email_verified,
                },
            )
        if request.url.host == "www.googleapis.com" and request.url.path.startswith(
            "/calendar/v3/calendars/primary/events"
        ):
            self.calendar_calls.append(request)
            if request.url.path == "/calendar/v3/calendars/primary/events":
                if request.method == "POST":
                    return self._handle_create(request)
                return self._handle_list(request)
            return self._handle_single(request)
        return httpx.Response(404)

    def _handle_list(self, request: httpx.Request) -> httpx.Response:
        if self.events_status != 200:
            return httpx.Response(self.events_status)
        if self.events_text is not None:
            return httpx.Response(200, text=self.events_text)
        if self.pages is not None:
            idx = min(len(self.calendar_calls) - 1, len(self.pages) - 1)
            return httpx.Response(200, json=self.pages[idx])
        return httpx.Response(200, json=self.events_payload)

    def _handle_create(self, request: httpx.Request) -> httpx.Response:
        if self.create_status != 200:
            return httpx.Response(self.create_status)
        body = json.loads(request.content.decode("utf-8"))
        self.create_body = body
        if self.create_payload is not None:
            return httpx.Response(200, json=self.create_payload)
        echo = {
            "id": self.created_id,
            "summary": body["summary"],
            "start": body["start"],
            "end": body["end"],
            "attendees": body.get("attendees", []),
        }
        self.last_created = echo
        return httpx.Response(200, json=echo)

    def _handle_single(self, request: httpx.Request) -> httpx.Response:
        if self.read_status != 200:
            return httpx.Response(self.read_status)
        if self.readback is not None:
            return httpx.Response(200, json=self.readback)
        if self.last_created is not None:
            return httpx.Response(200, json=self.last_created)
        return httpx.Response(404)


@pytest.fixture
def stub() -> CalendarStub:
    return CalendarStub()


@pytest_asyncio.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://testserver"
    ) as test_client:
        yield test_client


@pytest_asyncio.fixture
async def google_transport(stub: CalendarStub) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=httpx.MockTransport(stub.handler)) as transport:
        app.dependency_overrides[get_google_http_client] = lambda: transport
        try:
            yield transport
        finally:
            app.dependency_overrides.pop(get_google_http_client, None)


# --- helpers --------------------------------------------------------------

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


async def _bind(client: httpx.AsyncClient, token: str) -> None:
    """Link the stub's Google identity through the pending-confirmation flow."""
    url = await _connect(client, token)
    started = await client.get(
        f"{API_PREFIX}/auth/google/callback",
        params={"state": _state_from(url), "code": "auth-code-123"},
    )
    assert started.status_code == 303, started.text
    confirmed = await client.post(
        f"{API_PREFIX}/auth/google/pending/confirm",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert confirmed.status_code == 200


async def _account_id(client: httpx.AsyncClient, token: str) -> str:
    resp = await client.get(
        f"{API_PREFIX}/auth/google/accounts",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    accounts = resp.json()["accounts"]
    assert len(accounts) == 1
    return accounts[0]["account_id"]


async def _slots(
    client: httpx.AsyncClient,
    token: str,
    *,
    date: str = TEST_DAY,
    timezone: str = BERLIN,
    day_start: str = "09:00",
    day_end: str = "17:00",
    account_id: str | None = None,
) -> httpx.Response:
    params: dict[str, str] = {
        "date": date,
        "timezone": timezone,
        "day_start": day_start,
        "day_end": day_end,
    }
    if account_id is not None:
        params["account_id"] = account_id
    return await client.get(
        f"{API_PREFIX}/calendar/slots",
        params=params,
        headers={"Authorization": f"Bearer {token}"},
    )


async def _create(
    client: httpx.AsyncClient,
    token: str,
    *,
    title: str = "Team Sync",
    date: str = TEST_DAY,
    start_time: str = "10:00",
    end_time: str = "11:00",
    timezone: str = BERLIN,
    attendees: tuple[str, ...] = (),
    account_id: str | None = None,
) -> httpx.Response:
    body: dict = {
        "title": title,
        "date": date,
        "start_time": start_time,
        "end_time": end_time,
        "timezone": timezone,
        "attendees": list(attendees),
    }
    params = {"account_id": account_id} if account_id is not None else {}
    return await client.post(
        f"{API_PREFIX}/calendar/events",
        params=params,
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )


# --- least-privilege scope -------------------------------------------------

async def test_connect_requests_events_scope_never_full_calendar(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
) -> None:
    token = await _register(client)
    url = await _connect(client, token)
    scopes = parse_qs(urlparse(url).query)["scope"][0].split()
    assert CALENDAR_EVENTS_SCOPE in scopes
    forbidden = (
        "https://www.googleapis.com/auth/calendar",
        "https://www.googleapis.com/auth/calendar.readonly",
        "https://www.googleapis.com/auth/calendar.settings.readonly",
        "https://www.googleapis.com/auth/gmail",
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/contacts",
    )
    for scope in forbidden:
        assert scope not in scopes


# --- authentication / account binding -------------------------------------

async def test_slots_require_authentication(
    client: httpx.AsyncClient, google_settings: None, google_transport: httpx.AsyncClient
) -> None:
    resp = await _slots(client, "invalid-token")
    assert resp.status_code == 401


async def test_public_calendar_write_cannot_bypass_governance(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: CalendarStub,
) -> None:
    """A public endpoint must not be able to create a calendar event unguarded.

    This endpoint used to call ``create_and_verify_event`` directly, which made
    it a second route to the same provider write the runtime already performs
    through ``ToolGateway`` — skipping policy, approval, identity resolution,
    durable evidence, audit and alerting.

    The status code alone proves nothing, so the assertions are on the controls:
    the response names the governed path, and **no request reached Google at
    all**. A handler that wrote first and refused afterwards would still pass a
    status-only test.
    """
    token = await _register(client)
    await _bind(client, token)
    calls_before = len(stub.calendar_calls)

    resp = await _create(client, token)

    assert resp.status_code == 410
    detail = resp.json()["detail"]
    assert detail["error"] == "direct_calendar_write_removed"
    assert detail["use"] == "/runtime/tasks"

    # The actual control: not one provider request was issued.
    assert len(stub.calendar_calls) == calls_before
    assert stub.create_body is None
    assert stub.last_created is None


async def test_public_calendar_write_is_refused_before_any_authorization(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: CalendarStub,
) -> None:
    """Retirement must not become a new information or side-effect surface.

    The refusal happens before authentication, so it cannot be used to probe
    whether an account exists, and it cannot be used to write. A garbage token
    and a valid one must be indistinguishable here.
    """
    calls_before = len(stub.calendar_calls)

    with_garbage = await _create(client, "invalid-token")
    anonymous = await client.post(
        f"{API_PREFIX}/calendar/events",
        json={
            "title": "Team Sync",
            "date": TEST_DAY,
            "start_time": "10:00",
            "end_time": "11:00",
            "timezone": BERLIN,
        },
    )

    assert with_garbage.status_code == 410
    assert anonymous.status_code == 410
    assert with_garbage.json() == anonymous.json()
    assert len(stub.calendar_calls) == calls_before


async def test_slots_without_connected_account_is_404(
    client: httpx.AsyncClient, google_settings: None, google_transport: httpx.AsyncClient
) -> None:
    token = await _register(client)
    assert (await _slots(client, token)).status_code == 404


async def test_slots_never_leak_tokens(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: CalendarStub,
) -> None:
    token = await _register(client)
    await _bind(client, token)
    slot_resp = await _slots(client, token)
    assert slot_resp.status_code == 200
    assert stub.access_token not in slot_resp.text
    assert stub.refresh_token not in slot_resp.text
    assert "credentials_encrypted" not in slot_resp.text
    assert "test-client-secret" not in slot_resp.text
    assert (
        stub.calendar_calls[0].headers["Authorization"]
        == f"Bearer {stub.access_token}"
    )


# --- tenant isolation ------------------------------------------------------

async def test_slots_account_id_is_tenant_scoped(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: CalendarStub,
) -> None:
    token_a = await _register(client, "alice@example.com")
    token_b = await _register(client, "bob@example.com")
    stub.sub = "sub-a"
    await _bind(client, token_a)
    stub.sub = "sub-b"
    await _bind(client, token_b)

    account_a = await _account_id(client, token_a)
    assert (await _slots(client, token_a, account_id=account_a)).status_code == 200
    assert (await _slots(client, token_b, account_id=account_a)).status_code == 404


# --- availability: empty / busy / timezone ---------------------------------

async def test_empty_calendar_yields_whole_window_free(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: CalendarStub,
) -> None:
    token = await _register(client)
    await _bind(client, token)
    stub.events_payload = {"items": []}
    resp = await _slots(client, token)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["free"]) == 1
    assert body["free"][0]["start"] == "2026-06-24T09:00:00+02:00"
    assert body["free"][0]["end"] == "2026-06-24T17:00:00+02:00"
    assert body["busy"] == []


async def test_all_day_busy_yields_no_free_slots(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: CalendarStub,
) -> None:
    token = await _register(client)
    await _bind(client, token)
    stub.events_payload = {
        "items": [
            timed_event(
                event_id="e1",
                summary="All day",
                start=_dt(time_of_day="00:00"),
                end=_dt(time_of_day="23:59"),
            )
        ]
    }
    resp = await _slots(client, token)
    assert resp.status_code == 200
    assert resp.json()["free"] == []


async def test_slots_expose_explicit_offset_timestamps(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: CalendarStub,
) -> None:
    token = await _register(client)
    await _bind(client, token)
    stub.events_payload = {
        "items": [
            timed_event(
                event_id="busy1",
                summary="Standup",
                start=_dt(time_of_day="12:00"),
                end=_dt(time_of_day="13:00"),
            )
        ]
    }
    resp = await _slots(client, token)
    assert resp.status_code == 200
    body = resp.json()
    assert body["timezone"] == BERLIN
    assert body["date"] == TEST_DAY
    assert len(body["free"]) == 2
    assert body["free"] == [
        {"start": "2026-06-24T09:00:00+02:00", "end": "2026-06-24T12:00:00+02:00"},
        {"start": "2026-06-24T13:00:00+02:00", "end": "2026-06-24T17:00:00+02:00"},
    ]
    assert body["busy"] == [
        {"start": "2026-06-24T12:00:00+02:00", "end": "2026-06-24T13:00:00+02:00"}
    ]
    for slot in body["free"] + body["busy"]:
        parsed = datetime.fromisoformat(slot["start"])
        assert parsed.utcoffset() == timedelta(hours=2)


async def test_slots_query_sends_explicit_utc_bounds(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: CalendarStub,
) -> None:
    token = await _register(client)
    await _bind(client, token)
    await _slots(client, token, timezone=BERLIN)
    params = stub.calendar_calls[0].url.params
    assert params["timeMin"] == "2026-06-24T09:00:00+02:00"
    assert params["timeMax"] == "2026-06-24T17:00:00+02:00"
    assert params["singleEvents"] == "true"
    assert params["orderBy"] == "startTime"
    min_dt = datetime.fromisoformat(params["timeMin"])
    assert min_dt.utcoffset() == timedelta(hours=2)


async def test_busy_event_crossing_window_is_clipped(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: CalendarStub,
) -> None:
    token = await _register(client)
    await _bind(client, token)
    stub.events_payload = {
        "items": [
            timed_event(
                event_id="e1",
                summary="Early",
                start=_dt(time_of_day="08:00"),
                end=_dt(time_of_day="10:30"),
            )
        ]
    }
    resp = await _slots(client, token)
    assert resp.status_code == 200
    body = resp.json()
    assert body["free"] == [
        {"start": "2026-06-24T10:30:00+02:00", "end": "2026-06-24T17:00:00+02:00"}
    ]
    assert body["busy"] == [
        {"start": "2026-06-24T09:00:00+02:00", "end": "2026-06-24T10:30:00+02:00"}
    ]


async def test_overlapping_busy_events_are_merged(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: CalendarStub,
) -> None:
    token = await _register(client)
    await _bind(client, token)
    stub.events_payload = {
        "items": [
            timed_event(
                event_id="e1",
                summary="A",
                start=_dt(time_of_day="10:00"),
                end=_dt(time_of_day="12:00"),
            ),
            timed_event(
                event_id="e2",
                summary="B",
                start=_dt(time_of_day="11:30"),
                end=_dt(time_of_day="13:30"),
            ),
        ]
    }
    resp = await _slots(client, token)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["busy"]) == 1
    assert body["busy"][0] == {
        "start": "2026-06-24T10:00:00+02:00",
        "end": "2026-06-24T13:30:00+02:00",
    }


async def test_invalid_timezone_is_400(
    client: httpx.AsyncClient, google_settings: None, google_transport: httpx.AsyncClient
) -> None:
    token = await _register(client)
    await _bind(client, token)
    resp = await _slots(client, token, timezone="Mars/Olympus")
    assert resp.status_code == 400


async def test_invalid_date_is_400(
    client: httpx.AsyncClient, google_settings: None, google_transport: httpx.AsyncClient
) -> None:
    token = await _register(client)
    await _bind(client, token)
    assert (await _slots(client, token, date="2026-13-99")).status_code == 400


async def test_day_end_before_start_is_400(
    client: httpx.AsyncClient, google_settings: None, google_transport: httpx.AsyncClient
) -> None:
    token = await _register(client)
    await _bind(client, token)
    assert (
        await _slots(client, token, day_start="17:00", day_end="09:00")
    ).status_code == 400


# --- malformed external data -----------------------------------------------

async def test_malformed_items_are_skipped_not_fatal(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: CalendarStub,
) -> None:
    token = await _register(client)
    await _bind(client, token)
    stub.events_payload = {
        "items": [
            timed_event(
                event_id="ok",
                summary="OK",
                start=_dt(time_of_day="12:00"),
                end=_dt(time_of_day="13:00"),
            ),
            {"id": "no-times"},  # skipped: no start/end
            "not-a-dict",
            {
                "id": "bad-times",
                "start": {"dateTime": "garbage"},
                "end": {"dateTime": "also-garbage"},
            },
            {
                "id": "reversed",
                "start": {"dateTime": "2026-06-24T13:00:00+02:00"},
                "end": {"dateTime": "2026-06-24T12:00:00+02:00"},
            },
            None,
        ]
    }
    resp = await _slots(client, token)
    assert resp.status_code == 200
    assert len(resp.json()["busy"]) == 1


async def test_missing_items_is_empty_calendar(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: CalendarStub,
) -> None:
    token = await _register(client)
    await _bind(client, token)
    stub.events_payload = {"connections": "not-a-calendar-payload"}
    resp = await _slots(client, token)
    assert resp.status_code == 200
    assert len(resp.json()["free"]) == 1


async def test_non_json_events_list_is_502(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: CalendarStub,
) -> None:
    token = await _register(client)
    await _bind(client, token)
    stub.events_text = "<html>proxy error</html>"
    resp = await _slots(client, token)
    assert resp.status_code == 502


# --- event creation + deterministic verification ---------------------------

# --- verification mismatch ------------------------------------------------

# --- API errors / rate limit ----------------------------------------------

async def test_slots_401_is_401(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: CalendarStub,
) -> None:
    token = await _register(client)
    await _bind(client, token)
    stub.events_status = 401
    resp = await _slots(client, token)
    assert resp.status_code == 401


async def test_slots_403_is_403(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: CalendarStub,
) -> None:
    token = await _register(client)
    await _bind(client, token)
    stub.events_status = 403
    resp = await _slots(client, token)
    assert resp.status_code == 403
    assert "scope" in resp.json()["detail"]


async def test_slots_429_is_429(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: CalendarStub,
) -> None:
    token = await _register(client)
    await _bind(client, token)
    stub.events_status = 429
    resp = await _slots(client, token)
    assert resp.status_code == 429


async def test_slots_5xx_is_502(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: CalendarStub,
) -> None:
    token = await _register(client)
    await _bind(client, token)
    stub.events_status = 500
    resp = await _slots(client, token)
    assert resp.status_code == 502


# --- pagination -----------------------------------------------------------

async def test_pagination_accumulates_busy_events_across_pages(
    client: httpx.AsyncClient,
    google_settings: None,
    google_transport: httpx.AsyncClient,
    stub: CalendarStub,
) -> None:
    token = await _register(client)
    await _bind(client, token)
    stub.pages = [
        {
            "items": [
                timed_event(
                    event_id="e1",
                    summary="A",
                    start=_dt(time_of_day="10:00"),
                    end=_dt(time_of_day="11:00"),
                )
            ],
            "nextPageToken": "p2",
        },
        {
            "items": [
                timed_event(
                    event_id="e2",
                    summary="B",
                    start=_dt(time_of_day="14:00"),
                    end=_dt(time_of_day="15:00"),
                )
            ],
            "nextPageToken": "p3",
        },
        {
            "items": [
                timed_event(
                    event_id="e3",
                    summary="C",
                    start=_dt(time_of_day="16:00"),
                    end=_dt(time_of_day="17:00"),
                )
            ],
        },
    ]
    resp = await _slots(client, token)
    assert resp.status_code == 200
    body = resp.json()
    assert body["free"] == [
        {"start": "2026-06-24T09:00:00+02:00", "end": "2026-06-24T10:00:00+02:00"},
        {"start": "2026-06-24T11:00:00+02:00", "end": "2026-06-24T14:00:00+02:00"},
        {"start": "2026-06-24T15:00:00+02:00", "end": "2026-06-24T16:00:00+02:00"},
    ]
    assert len(stub.calendar_calls) == 3
    assert stub.calendar_calls[1].url.params["pageToken"] == "p2"
    assert stub.calendar_calls[2].url.params["pageToken"] == "p3"