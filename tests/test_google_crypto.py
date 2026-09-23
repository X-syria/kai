"""Unit tests for Fernet credential encryption at rest."""

import pytest
from cryptography.fernet import InvalidToken

from kai.google.crypto import decrypt_credentials, encrypt_credentials


def test_encrypt_decrypt_roundtrip() -> None:
    credentials = {
        "access_token": "secret-access-token",
        "refresh_token": "supersecret-refresh-token",
    }
    encoded = encrypt_credentials(credentials)
    assert decrypt_credentials(encoded) == credentials


def test_ciphertext_never_leaks_plaintext() -> None:
    credentials = {
        "access_token": "secret-access-token",
        "refresh_token": "supersecret-refresh-token",
    }
    encoded = encrypt_credentials(credentials)
    assert "secret-access-token" not in encoded
    assert "supersecret-refresh-token" not in encoded
    assert "access_token" not in encoded


def test_tampered_ciphertext_is_rejected() -> None:
    encoded = encrypt_credentials({"access_token": "x"})
    tampered = ("A" if encoded[0] != "A" else "B") + encoded[1:]
    with pytest.raises(InvalidToken):
        decrypt_credentials(tampered)