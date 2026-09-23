"""Unit tests for the signed OAuth ``state`` (crypto helper, no network)."""

import base64
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from kai.google.state import InvalidStateError, create_state, validate_state


def test_valid_state_roundtrip_carries_binding_claims() -> None:
    user_id = uuid4()
    tenant_id = uuid4()
    claims = validate_state(create_state(user_id=user_id, tenant_id=tenant_id))
    assert claims.user_id == user_id
    assert claims.tenant_id == tenant_id


def test_tampered_body_is_rejected() -> None:
    state = create_state(user_id=uuid4(), tenant_id=uuid4())
    body, signature = state.split(".", 1)
    payload = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode("utf-8")
    forged_payload = payload[:-1] + ("0" if payload[-1] != "0" else "1")
    forged_body = (
        base64.urlsafe_b64encode(forged_payload.encode("utf-8"))
        .decode("ascii")
        .rstrip("=")
    )
    with pytest.raises(InvalidStateError):
        validate_state(f"{forged_body}.{signature}")


def test_tampered_signature_is_rejected() -> None:
    state = create_state(user_id=uuid4(), tenant_id=uuid4())
    body, signature = state.split(".", 1)
    forged_signature = ("0" if signature[-1] != "0" else "1") + signature[1:]
    with pytest.raises(InvalidStateError):
        validate_state(f"{body}.{forged_signature}")


def test_expired_state_is_rejected() -> None:
    state = create_state(
        user_id=uuid4(),
        tenant_id=uuid4(),
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    with pytest.raises(InvalidStateError):
        validate_state(state)


def test_malformed_state_is_rejected() -> None:
    for bad_state in ("", "abc", "a.b.c", "not-base64..", "123"):
        with pytest.raises(InvalidStateError):
            validate_state(bad_state)


def test_expiry_is_time_bound() -> None:
    state = create_state(user_id=uuid4(), tenant_id=uuid4())
    validate_state(state)
    with pytest.raises(InvalidStateError, match="expired"):
        validate_state(state, now=datetime.now(UTC) + timedelta(hours=1))