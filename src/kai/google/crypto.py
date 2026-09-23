"""Fernet encryption of Google credentials stored at rest.

The ciphertext is the only place refresh/access tokens live in the database:
never the plaintext. The key is derived from
``Settings.google_creds_encryption_secret`` (32+ random bytes) via SHA-256, so
the Fernet key is never the client secret itself.
"""

import base64
import hashlib
import json

from cryptography.fernet import Fernet

from kai.config import get_settings


def _fernet() -> Fernet:
    secret = get_settings().google_creds_encryption_secret
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


def encrypt_credentials(credentials: dict) -> str:
    """Encrypt a credentials dictionary into an ASCII Fernet token.

    Tampering or a wrong ``google_creds_encryption_secret`` makes
    :func:`decrypt_credentials` raise and the stored record undecryptable.
    """
    plaintext = json.dumps(credentials, sort_keys=True).encode("utf-8")
    return _fernet().encrypt(plaintext).decode("ascii")


def decrypt_credentials(encoded: str) -> dict:
    """Decrypt a :func:`encrypt_credentials` payload back to a dictionary."""
    plaintext = _fernet().decrypt(encoded.encode("ascii")).decode("utf-8")
    return json.loads(plaintext)