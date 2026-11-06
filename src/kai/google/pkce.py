"""PKCE (RFC 7636) S256 helpers for the browser Google OAuth flow.

The verifier is the same random secret D33 already puts in the browser-binding
cookie. Reusing it is deliberate: the callback already proves it holds that
secret, so the token exchange can prove the same thing to Google without
introducing a second secret with its own lifecycle.

Only the challenge travels in the authorization URL. The verifier stays in the
``HttpOnly`` cookie and is sent from the server to the token endpoint; it must
never appear in a URL, a ``state`` payload, an API response, or a log line.
"""

import base64
import hashlib
import re

#: RFC 7636 section 4.1: 43 to 128 characters from the unreserved set.
_VERIFIER_RE = re.compile(r"[A-Za-z0-9\-._~]{43,128}")


def is_valid_code_verifier(value: str) -> bool:
    """Return whether ``value`` is a syntactically valid PKCE verifier."""
    return _VERIFIER_RE.fullmatch(value) is not None


def code_challenge(verifier: str) -> str:
    """Return the S256 challenge for ``verifier``, without Base64 padding.

    The encoding is ``BASE64URL(SHA256(ASCII(verifier)))`` with ``=`` removed,
    exactly as RFC 7636 section 4.2 defines it. It is not the same encoding as
    D33's ``binding_hash`` (which is lowercase hex); the two values serve
    different protocols and are intentionally derived separately.
    """
    if not is_valid_code_verifier(verifier):
        raise ValueError(
            "code_verifier must be 43-128 characters from [A-Za-z0-9-._~]"
        )
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
