"""Pure unit tests for the Google consent scope builder.

No database, no network, no ASGI client: these tests call
``build_connect_scopes`` directly. They are marked ``no_db_unit`` so the
autouse ``_clean_state`` fixture yields without checking or truncating any
database, exactly like the guard's own unit tests, while keeping the
``db_guard_unit`` marker meaning what it says.
"""

from __future__ import annotations

import pytest

from kai.api.google import (
    build_connect_scopes,
    granted_requested_scopes,
    missing_core_scopes,
)
from kai.config import Settings

pytestmark = pytest.mark.no_db_unit

IDENTITY: list[str] = ["openid", "email"]
CONTACTS = "https://www.googleapis.com/auth/contacts.readonly"
CALENDAR = "https://www.googleapis.com/auth/calendar.events"
GMAIL_READ = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_SEND = "https://www.googleapis.com/auth/gmail.send"


def _settings(**overrides: object) -> Settings:
    """A deterministic settings object; no ``.env`` and no ambient values."""
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


def test_default_consent_is_exactly_the_four_slice_scopes() -> None:
    assert build_connect_scopes(settings=_settings()) == [
        *IDENTITY,
        CONTACTS,
        CALENDAR,
    ]


def test_default_consent_never_requests_gmail() -> None:
    scopes = build_connect_scopes(settings=_settings())
    assert GMAIL_READ not in scopes
    assert GMAIL_SEND not in scopes


def test_gmail_opt_in_adds_exactly_the_two_gmail_scopes() -> None:
    defaults = build_connect_scopes(settings=_settings())
    opted_in = build_connect_scopes(include_gmail=True, settings=_settings())
    assert opted_in == [*defaults, GMAIL_READ, GMAIL_SEND]


def test_over_broad_scope_setting_cannot_widen_the_request() -> None:
    over_broad = " ".join(
        [
            "openid",
            "email",
            GMAIL_READ,
            GMAIL_SEND,
            "https://www.googleapis.com/auth/gmail.modify",
            "https://www.googleapis.com/auth/calendar",
            "https://www.googleapis.com/auth/contacts",
            "https://www.googleapis.com/auth/drive",
        ]
    )
    settings = _settings(google_oauth_scopes=over_broad)
    assert build_connect_scopes(settings=settings) == [*IDENTITY, CONTACTS, CALENDAR]
    assert build_connect_scopes(include_gmail=True, settings=settings) == [
        *IDENTITY,
        CONTACTS,
        CALENDAR,
        GMAIL_READ,
        GMAIL_SEND,
    ]


def test_scope_setting_can_narrow_identity_order_is_preserved() -> None:
    settings = _settings(google_oauth_scopes="email openid")
    assert build_connect_scopes(settings=settings) == [
        "email",
        "openid",
        CONTACTS,
        CALENDAR,
    ]


def test_duplicate_scopes_are_removed_across_sources() -> None:
    settings = _settings(
        google_oauth_scopes=f"openid email {CONTACTS} {CALENDAR} openid email"
    )
    assert build_connect_scopes(settings=settings) == [*IDENTITY, CONTACTS, CALENDAR]


def test_slice_scopes_come_from_settings_not_hardcoded_duplicates() -> None:
    settings = _settings(
        google_contacts_scope="scope:contacts",
        google_calendar_scope="scope:calendar",
        google_gmail_read_scope="scope:gmail-read",
        google_gmail_send_scope="scope:gmail-send",
    )
    assert build_connect_scopes(include_gmail=True, settings=settings) == [
        *IDENTITY,
        "scope:contacts",
        "scope:calendar",
        "scope:gmail-read",
        "scope:gmail-send",
    ]


# --- granted-vs-requested reconciliation (pure) ----------------------------


def test_email_alias_in_granted_matches_requested_short_name() -> None:
    requested = build_connect_scopes(settings=_settings())
    granted = " ".join(
        [
            "openid",
            "https://www.googleapis.com/auth/userinfo.email",
            CONTACTS,
            CALENDAR,
        ]
    )
    assert granted_requested_scopes(requested, granted) == [
        *IDENTITY,
        CONTACTS,
        CALENDAR,
    ]


def test_granted_subset_excludes_withheld_scope_and_reports_it_missing() -> None:
    settings = _settings()
    requested = build_connect_scopes(settings=settings)
    granted = " ".join(["openid", "email", CONTACTS])
    effective = granted_requested_scopes(requested, granted)
    assert effective == [*IDENTITY, CONTACTS]
    assert missing_core_scopes(effective, settings=settings) == [CALENDAR]


def test_gmail_absent_from_grant_is_never_returned() -> None:
    settings = _settings()
    requested = build_connect_scopes(include_gmail=True, settings=settings)
    granted = " ".join(["openid", "email", CONTACTS, CALENDAR])
    effective = granted_requested_scopes(requested, granted)
    assert GMAIL_READ not in effective
    assert GMAIL_SEND not in effective
    assert effective == [*IDENTITY, CONTACTS, CALENDAR]


def test_missing_core_scopes_ignores_identity() -> None:
    settings = _settings()
    assert missing_core_scopes(["openid", "email"], settings=settings) == [
        CONTACTS,
        CALENDAR,
    ]


def test_granted_with_unrequested_extra_is_not_stored() -> None:
    requested = build_connect_scopes(settings=_settings())
    granted = " ".join(
        [
            "openid",
            "email",
            "https://www.googleapis.com/auth/userinfo.profile",
            CONTACTS,
            CALENDAR,
        ]
    )
    assert granted_requested_scopes(requested, granted) == [
        *IDENTITY,
        CONTACTS,
        CALENDAR,
    ]


@pytest.mark.parametrize("include_gmail", [False, True])
def test_stored_scope_string_fits_the_255_character_column(
    include_gmail: bool,
) -> None:
    settings = _settings()
    requested = build_connect_scopes(include_gmail=include_gmail, settings=settings)
    granted_all = " ".join(
        [
            "openid",
            "https://www.googleapis.com/auth/userinfo.email",
            "https://www.googleapis.com/auth/userinfo.profile",
            *requested[2:],
        ]
    )
    stored = " ".join(granted_requested_scopes(requested, granted_all))
    assert stored == " ".join(requested)
    assert len(stored) <= 255
