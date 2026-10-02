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
    # Replacing the FIRST character is chosen so the forgery provably differs
    # from the original. The previous form derived the replacement from the
    # last character while replacing the first, so whenever the signature began
    # with the character it substituted the "forged" value was byte-identical to
    # the valid signature and this test failed intermittently (~1 run in 56)
    # while the implementation was correct.
    forged_signature = ("A" if signature[0] != "A" else "B") + signature[1:]
    assert forged_signature != signature
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


def test_forgery_helper_always_changes_the_signature() -> None:
    """A tamper that produces the original signature is not a tamper.

    The signing key is HMAC over fresh uuid4s, so the signature's leading
    character is uniform over its base64url alphabet. A tamper helper that can
    substitute a character already present leaves itself untouched roughly once
    in 64 runs, which is an intermittently failing test rather than a caught
    defect. This asserts the property the helper must have, over many draws,
    so the suite's green is a property of the code and not of the clock.
    """
    def forge(signature: str) -> str:
        return ("A" if signature[0] != "A" else "B") + signature[1:]

    unchanged = 0
    for _ in range(512):
        signature = create_state(user_id=uuid4(), tenant_id=uuid4()).split(".", 1)[1]
        forged = forge(signature)
        assert len(forged) == len(signature)
        if forged == signature:
            unchanged += 1
    assert unchanged == 0