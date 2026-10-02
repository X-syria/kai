"""Google OAuth 2.0 wire contract.

Only the authorization URL is built locally. Every network call (code
exchange, refresh, revoke, userinfo) goes through an injected
``httpx.AsyncClient`` so tests can isolate Google's network behind
``httpx.MockTransport`` while the database and crypto stay real.
"""

import contextlib
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from kai.config import get_settings
from kai.observability import metrics
from kai.observability.providers import provider_call

AUTHORIZATION_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"

DEFAULT_SCOPES = "openid email"


class GoogleOAuthError(RuntimeError):
    """Raised when the Google transport rejects an OAuth operation."""


@dataclass(frozen=True)
class TokenData:
    """Successful token response from the OAuth token endpoint."""

    access_token: str
    refresh_token: str | None
    expires_in: int


@dataclass(frozen=True)
class UserInfo:
    """Verified identity from the Google userinfo endpoint."""

    sub: str
    email: str
    email_verified: bool


def build_authorization_url(*, scopes: list[str], redirect_uri: str, state: str) -> str:
    """Build the browser-facing authorization URL.

    ``access_type=offline`` is required for Google to issue a refresh token;
    identity-only scopes are always enforced by the caller, never widened here.
    """
    settings = get_settings()
    if not settings.google_oauth_client_id:
        raise GoogleOAuthError("google oauth client id is not configured")
    params = {
        "client_id": settings.google_oauth_client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "access_type": "offline",
        "state": state,
    }
    return f"{AUTHORIZATION_URL}?{urlencode(params)}"


def _parse_token_response(resp: httpx.Response) -> TokenData:
    if resp.status_code != 200:
        error_code = "unknown"
        with contextlib.suppress(ValueError):
            error_code = str(resp.json().get("error") or "unknown")
        raise GoogleOAuthError(
            f"google token request failed ({resp.status_code}: {error_code})"
        )
    payload = resp.json()
    access_token = payload.get("access_token")
    if not access_token:
        raise GoogleOAuthError("google token response is missing access_token")
    refresh_token = payload.get("refresh_token")
    return TokenData(
        access_token=access_token,
        refresh_token=str(refresh_token) if refresh_token else None,
        expires_in=int(payload.get("expires_in") or 0),
    )


async def exchange_code(
    transport: httpx.AsyncClient, *, code: str, redirect_uri: str
) -> TokenData:
    """Exchange an authorization code for tokens."""
    settings = get_settings()
    with provider_call(provider=metrics.PROVIDER_GOOGLE, operation="oauth_exchange_code"):
        resp = await transport.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": settings.google_oauth_client_id,
                "client_secret": settings.google_oauth_client_secret,
                "redirect_uri": redirect_uri,
            },
        )
        return _parse_token_response(resp)


async def refresh_access_token(
    transport: httpx.AsyncClient, *, refresh_token: str
) -> TokenData:
    """Exchange a refresh token for a fresh access token."""
    settings = get_settings()
    with provider_call(provider=metrics.PROVIDER_GOOGLE, operation="oauth_refresh"):
        resp = await transport.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": settings.google_oauth_client_id,
                "client_secret": settings.google_oauth_client_secret,
            },
        )
        return _parse_token_response(resp)


async def fetch_userinfo(
    transport: httpx.AsyncClient, *, access_token: str
) -> UserInfo:
    """Fetch the identity attached to ``access_token`` from Google.

    ``sub`` is the stable Google subject id used to bind accounts.
    """
    with provider_call(provider=metrics.PROVIDER_GOOGLE, operation="oauth_userinfo"):
        resp = await transport.get(
            USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}
        )
        if resp.status_code != 200:
            raise GoogleOAuthError(
                f"google userinfo request failed ({resp.status_code})"
            )
        payload = resp.json()
        sub = payload.get("sub")
        email = payload.get("email")
        if not sub or not email:
            raise GoogleOAuthError("google userinfo response is missing sub or email")
        return UserInfo(
            sub=str(sub),
            email=str(email),
            email_verified=bool(payload.get("email_verified")),
        )


async def revoke_access_token(
    transport: httpx.AsyncClient, *, access_token: str
) -> None:
    """Revoke an access token at Google.

    Google answers 200 on success and 400/404 for already-invalid or unknown
    tokens; all three mean the token can no longer be used, so they are not
    treated as errors.
    """
    with provider_call(provider=metrics.PROVIDER_GOOGLE, operation="oauth_revoke"):
        resp = await transport.post(
            REVOKE_URL,
            params={"token": access_token},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if resp.status_code not in (200, 400, 404):
            raise GoogleOAuthError(
                f"google revoke request failed ({resp.status_code})"
            )