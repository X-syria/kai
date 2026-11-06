"""Pure RFC 7636 S256 tests: no database, network, or ASGI client."""

import secrets

import pytest

from kai.google.pkce import code_challenge, is_valid_code_verifier

pytestmark = pytest.mark.no_db_unit

#: RFC 7636 Appendix B test vector.
RFC_VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
RFC_CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_rfc7636_appendix_b_vector() -> None:
    assert code_challenge(RFC_VERIFIER) == RFC_CHALLENGE


def test_challenge_has_no_base64_padding() -> None:
    challenge = code_challenge(RFC_VERIFIER)
    assert "=" not in challenge
    assert len(challenge) == 43


def test_token_urlsafe_secret_is_a_valid_verifier() -> None:
    """D33's binding secret is 43 base64url characters, so it is RFC-valid."""
    verifier = secrets.token_urlsafe(32)
    assert len(verifier) == 43
    assert is_valid_code_verifier(verifier)
    assert len(code_challenge(verifier)) == 43


def test_dot_and_tilde_are_allowed_but_other_punctuation_is_not() -> None:
    assert is_valid_code_verifier("a" * 42 + ".")
    assert is_valid_code_verifier("a" * 42 + "~")
    assert not is_valid_code_verifier("a" * 42 + "+")
    assert not is_valid_code_verifier("a" * 42 + "/")


@pytest.mark.parametrize(
    "bad_verifier",
    [
        "",
        "a" * 42,
        "a" * 129,
        "a" * 42 + "!",
        "a" * 42 + " ",
        "a" * 21 + " " * 1 + "a" * 21,
    ],
)
def test_invalid_verifiers_are_rejected(bad_verifier: str) -> None:
    assert not is_valid_code_verifier(bad_verifier)
    with pytest.raises(ValueError, match="code_verifier"):
        code_challenge(bad_verifier)
