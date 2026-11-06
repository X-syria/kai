"""Unit tests for the signed OAuth ``state`` (crypto helper, no network)."""

import base64
import hashlib
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from kai.google.state import (
    InvalidStateError,
    _sign,
    create_state,
    hash_binding,
    validate_state,
    verify_binding,
)

pytestmark = pytest.mark.no_db_unit

#: A fixed digest. The real value is derived from a random per-transaction
#: secret; these tests only need a syntactically valid one.
BINDING: str = "a" * 64


def _payload(state: str) -> str:
    body = state.split(".", 1)[0]
    return base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode("utf-8")


def _signed(payload: str) -> str:
    body = (
        base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")
    )
    return f"{body}.{_sign(payload)}"


def test_valid_state_roundtrip_carries_binding_claims() -> None:
    user_id = uuid4()
    tenant_id = uuid4()
    claims = validate_state(
        create_state(
            user_id=user_id,
            tenant_id=tenant_id,
            scopes=["openid", "email"],
            binding_hash=BINDING,
        )
    )
    assert claims.user_id == user_id
    assert claims.tenant_id == tenant_id
    assert claims.scopes == ("openid", "email")
    assert claims.binding_hash == BINDING


def test_scopes_roundtrip_preserves_order() -> None:
    scopes = ["openid", "email", "https://www.googleapis.com/auth/calendar.events"]
    claims = validate_state(
        create_state(
            user_id=uuid4(), tenant_id=uuid4(), scopes=scopes, binding_hash=BINDING
        )
    )
    assert claims.scopes == tuple(scopes)


def test_tampered_scope_field_invalidates_signature() -> None:
    state = create_state(
        user_id=uuid4(), tenant_id=uuid4(), scopes=["openid"], binding_hash=BINDING
    )
    body, signature = state.split(".", 1)
    payload = _payload(state)
    assert payload.rsplit(":", 1)[1] == "openid"
    tampered_payload = payload.rsplit(":", 1)[0] + ":calendar.events"
    tampered_body = (
        base64.urlsafe_b64encode(tampered_payload.encode("utf-8"))
        .decode("ascii")
        .rstrip("=")
    )
    with pytest.raises(InvalidStateError, match="signature"):
        validate_state(f"{tampered_body}.{signature}")


def test_three_part_legacy_payload_is_rejected() -> None:
    """States minted before scopes were bound expire within 600s; they are not
    accepted, because a state that cannot name its requested scopes cannot be
    checked against the token response."""
    exp = int((datetime.now(UTC) + timedelta(seconds=60)).timestamp())
    state = _signed(f"{uuid4()}:{uuid4()}:{exp}")
    with pytest.raises(InvalidStateError, match="malformed"):
        validate_state(state)


def test_four_part_state_without_browser_binding_is_rejected() -> None:
    """A correctly signed identity+scopes state (the pre-binding form) carries
    no browser digest. Accepting it would let a forwarded link complete the
    flow in a browser that never called ``/connect``, so it must fail closed."""
    exp = int((datetime.now(UTC) + timedelta(seconds=600)).timestamp())
    state = _signed(f"{uuid4()}:{uuid4()}:{exp}:openid email")
    with pytest.raises(InvalidStateError, match="malformed"):
        validate_state(state)


def test_empty_scope_field_is_rejected() -> None:
    exp = int((datetime.now(UTC) + timedelta(seconds=60)).timestamp())
    state = _signed(f"{uuid4()}:{uuid4()}:{exp}:{BINDING}:")
    with pytest.raises(InvalidStateError, match="malformed"):
        validate_state(state)


def test_tampered_body_is_rejected() -> None:
    state = create_state(
        user_id=uuid4(),
        tenant_id=uuid4(),
        scopes=["openid", "email"],
        binding_hash=BINDING,
    )
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
    state = create_state(
        user_id=uuid4(),
        tenant_id=uuid4(),
        scopes=["openid", "email"],
        binding_hash=BINDING,
    )
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
        scopes=["openid", "email"],
        binding_hash=BINDING,
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    with pytest.raises(InvalidStateError):
        validate_state(state)


def test_malformed_state_is_rejected() -> None:
    for bad_state in ("", "abc", "a.b.c", "not-base64..", "123"):
        with pytest.raises(InvalidStateError):
            validate_state(bad_state)


def test_expiry_is_time_bound() -> None:
    state = create_state(
        user_id=uuid4(),
        tenant_id=uuid4(),
        scopes=["openid", "email"],
        binding_hash=BINDING,
    )
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
        signature = create_state(
            user_id=uuid4(), tenant_id=uuid4(), scopes=["openid"], binding_hash=BINDING
        ).split(".", 1)[1]
        forged = forge(signature)
        assert len(forged) == len(signature)
        if forged == signature:
            unchanged += 1
    assert unchanged == 0


# --- browser binding digest ------------------------------------------------


def test_hash_binding_is_the_sha256_hex_digest() -> None:
    assert hash_binding("secret-value") == hashlib.sha256(
        b"secret-value"
    ).hexdigest()
    assert hash_binding("secret-value") != hash_binding("other-secret")


def test_binding_digest_roundtrips_through_the_signed_state() -> None:
    digest = hash_binding("browser-secret")
    claims = validate_state(
        create_state(
            user_id=uuid4(), tenant_id=uuid4(), scopes=["openid"], binding_hash=digest
        )
    )
    assert claims.binding_hash == digest
    assert verify_binding(claims, "browser-secret") is True


def test_verify_binding_rejects_a_different_secret() -> None:
    claims = validate_state(
        create_state(
            user_id=uuid4(),
            tenant_id=uuid4(),
            scopes=["openid"],
            binding_hash=hash_binding("browser-secret"),
        )
    )
    assert verify_binding(claims, "not-the-secret") is False


def test_verify_binding_rejects_absent_or_empty_secret() -> None:
    """A browser with no cookie at all must not be compared against an empty
    digest: it never started the transaction."""
    claims = validate_state(
        create_state(
            user_id=uuid4(),
            tenant_id=uuid4(),
            scopes=["openid"],
            binding_hash=hash_binding("browser-secret"),
        )
    )
    assert verify_binding(claims, "") is False
    assert verify_binding(claims, None) is False


def test_raw_binding_secret_never_appears_in_the_state() -> None:
    secret = "super-secret-browser-value"
    state = create_state(
        user_id=uuid4(),
        tenant_id=uuid4(),
        scopes=["openid"],
        binding_hash=hash_binding(secret),
    )
    assert secret not in state
    assert secret not in _payload(state)
    assert hash_binding(secret) in _payload(state)


@pytest.mark.parametrize(
    "bad_digest",
    ["", "a" * 63, "a" * 65, "A" * 64, "g" * 64, "a" * 62 + "::"],
)
def test_create_state_rejects_a_malformed_binding_digest(bad_digest: str) -> None:
    with pytest.raises(ValueError, match="binding_hash"):
        create_state(
            user_id=uuid4(),
            tenant_id=uuid4(),
            scopes=["openid"],
            binding_hash=bad_digest,
        )


@pytest.mark.parametrize(
    "bad_digest",
    ["", "a" * 63, "a" * 65, "A" * 64, "g" * 64],
)
def test_validate_state_rejects_a_malformed_binding_digest(
    bad_digest: str,
) -> None:
    """The digest is signed, so a bad one means a buggy or hostile mint, not a
    tamper; either way the payload is refused before the cookie is compared."""
    exp = int((datetime.now(UTC) + timedelta(seconds=600)).timestamp())
    state = _signed(f"{uuid4()}:{uuid4()}:{exp}:{bad_digest}:openid")
    with pytest.raises(InvalidStateError, match="malformed"):
        validate_state(state)


def test_scope_field_with_url_separators_survives_the_split() -> None:
    """``://`` in the scope list must not shift the field boundaries.

    The digest sits before the scopes and the scope field is the only one
    carrying colons, so ``split(":", 4)`` keeps every URL intact and in order.
    """
    scopes = [
        "openid",
        "email",
        "https://www.googleapis.com/auth/contacts.readonly",
        "https://www.googleapis.com/auth/calendar.events",
    ]
    claims = validate_state(
        create_state(
            user_id=uuid4(), tenant_id=uuid4(), scopes=scopes, binding_hash=BINDING
        )
    )
    assert claims.scopes == tuple(scopes)
    assert claims.binding_hash == BINDING
    payload = _payload(
        create_state(
            user_id=uuid4(), tenant_id=uuid4(), scopes=scopes, binding_hash=BINDING
        )
    )
    assert payload.split(":", 4)[3] == BINDING
    assert payload.split(":", 4)[4] == " ".join(scopes)
