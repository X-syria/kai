"""Google OAuth endpoints: connect, callback, confirmation, and account listing.

``/connect`` is behind auth and returns the Google authorization URL carrying a
signed ``state`` bound to the caller's ``(user_id, tenant_id)``. ``/callback``
restores that binding from the ``state`` alone (the browser that returns from
Google has no Bearer header), so a callback can only ever reach the user/tenant
that started it. ``/accounts`` never surfaces tokens or encrypted blobs.

The ``state`` alone is not enough to authorize the exchange, because it reaches
the browser through Google and the address bar and can therefore be forwarded.
``/connect`` also sets ``__Host-kai-oauth-binding``: a random, ``HttpOnly``,
``Secure``, ``SameSite=Lax`` cookie whose SHA-256 digest is embedded in the
signed ``state``. ``/callback`` refuses - before looking up the user or calling
Google - unless the request presents a cookie whose digest matches. Consequences
that operators and clients must respect:

* ``/connect`` has to be called *by the browser* that will return from Google,
  with credentials enabled (``fetch(url, {credentials: "include"})``). A server-
  to-server call returns a URL whose callback another browser cannot complete.
* Cross-origin clients need an explicit origin in the CORS allowlist, because
  a credentialed request is never allowed from ``*`` (see ``kai.security.cors``);
  cookies are not sent on a cross-site ``SameSite=Lax`` GET either.
* One browser holds one in-flight transaction: the cookie name is fixed, so a
  second ``/connect`` overwrites the first binding and only the newest ``state``
  can complete.
* The flow is also PKCE S256: the cookie secret is the ``code_verifier`` and
  its SHA-256 challenge travels in the authorization URL. The verifier is sent
  only from the server to the token endpoint, so a code obtained from a
  different authorization request cannot be exchanged here. As with any PKCE
  deployment, live compatibility with the configured Google client type needs
  staging verification.
* The cookie is cleared on every terminal callback path *after* the binding is
  verified. A failed binding check deliberately leaves the existing cookie
  untouched, so a tampered or forwarded link cannot cancel a legitimate
  transaction the browser still has in flight.
* ``code`` and ``state`` are credentials for the duration of the flow: they must
  not be logged (access logs included), and neither is accepted in a body or a
  header that a proxy would record.

**Deferred linking (D41):** the callback no longer binds the account. It proves
the Google identity and the browser, stores the issued credentials as a pending
transaction (``google_oauth_pending``, encrypted, digest-addressed), and
redirects the browser to a same-origin confirmation page. Only an authenticated
KAI user who holds the ``__Host-kai-google-pending`` cookie can confirm or
cancel; confirmation consumes the pending row and writes ``google_accounts``
atomically. This separates "this browser controls a Google identity" from "this
KAI user consents to link it", which is the consent-linking gap.

**Containment (D37):** the whole linking surface (``/connect``, ``/callback``,
``/confirm``, ``/pending``, ``/pending/confirm``, ``/pending/cancel``) is behind
:func:`google_oauth_linking_guard`, which always rejects and has no settings or
environment switch. Tests that use the deterministic Google stub override that
dependency explicitly; nothing in a deployment can enable linking by inheriting
an environment variable. Existing linked accounts, account listing, and the
Calendar/Gmail/Contacts token paths are not guarded.
"""

import hmac
import secrets
from collections.abc import Sequence
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kai.auth.deps import get_current_user
from kai.config import Settings, get_settings
from kai.db.engine import get_session
from kai.db.models import GoogleAccount, User
from kai.db.scoping import get_user_for_tenant
from kai.google import client
from kai.google.client import GoogleOAuthError, build_authorization_url
from kai.google.deps import get_google_http_client
from kai.google.pending import (
    PENDING_TTL_SECONDS,
    PendingExpiredError,
    PendingNotFoundError,
    PendingNotPendingError,
    PendingRefreshRequiredError,
    PendingStateReuseError,
    cancel_pending_transaction,
    confirm_pending_transaction,
    create_pending_transaction,
    load_pending_for_user,
    pending_view_payload,
)
from kai.google.pkce import code_challenge
from kai.google.service import (
    GoogleAccountConflictError,
    GoogleCredentialsError,
)
from kai.google.state import (
    STATE_TTL_SECONDS,
    InvalidStateError,
    create_state,
    hash_binding,
    validate_state,
    verify_binding,
)

router = APIRouter(prefix="/auth/google", tags=["google-oauth"])

#: Per-transaction browser secret. The ``__Host-`` prefix makes browsers drop
#: the cookie unless it is ``Secure``, has ``Path=/``, and carries no ``Domain``;
#: ``HttpOnly`` keeps it away from scripts, so an XSS cannot read the value that
#: completes an OAuth transaction.
OAUTH_BINDING_COOKIE = "__Host-kai-oauth-binding"

#: The confirmation secret of the *second* half of the flow. It is minted by the
#: callback, sent only to the browser, and stored server-side as a SHA-256 digest
#: on the pending row. A stolen ``state``/``code`` alone cannot confirm a link.
PENDING_COOKIE = "__Host-kai-google-pending"

#: Fixed, self-contained policy for the confirmation page. No external origin is
#: reachable from it: no fonts, no CDN, no images. ``frame-ancestors 'none'``
#: plus ``X-Frame-Options`` means it cannot be framed for clickjacking.
CONFIRM_PAGE_CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline';"
    " connect-src 'self'; base-uri 'none'; form-action 'none';"
    " frame-ancestors 'none'"
)


def google_oauth_linking_guard() -> None:
    """Fail closed: live Google account linking is temporarily disabled.

    There is deliberately no setting, feature flag, or environment variable for
    this guard. An inherited ``GOOGLE_OAUTH_LINKING_ENABLED``-style value must
    never be able to re-open a flow whose consent-linking risk is still open.
    Tests that exercise the deterministic Google stub override this FastAPI
    dependency explicitly; production code has no switch to turn it off.

    Re-enable only after the identity-confirmation design (D36/B+C) is reviewed,
    implemented, and tested. This guard is not an H1 closure.
    """
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="google account linking is temporarily disabled",
    )


#: Scopes that may appear in ``GOOGLE_OAUTH_SCOPES`` as the identity base. The
#: setting stays operator-configurable, but only downwards: it can narrow the
#: request (e.g. ``openid`` alone) and can never widen it.
_IDENTITY_SCOPES: tuple[str, ...] = ("openid", "email")

#: Google's short scope names and the equivalent OAuth2 userinfo URLs. A grant
#: may echo either spelling, so matching is identity-alias aware.
_SCOPE_ALIASES: tuple[tuple[str, ...], ...] = (
    ("email", "https://www.googleapis.com/auth/userinfo.email"),
    ("profile", "https://www.googleapis.com/auth/userinfo.profile"),
)


def _scope_identity(scope: str) -> str:
    """Return the canonical identity used to compare one scope to another."""
    for group in _SCOPE_ALIASES:
        if scope in group:
            return group[0]
    return scope


def build_connect_scopes(
    *, include_gmail: bool = False, settings: Settings | None = None
) -> list[str]:
    """Return the exact scope list the Google consent screen must carry.

    The Calendar vertical slice needs identity, read-only Contacts (a guest
    arrives as a name and is resolved through ``contacts.match``) and
    ``calendar.events``. It has no Gmail capability at all: the sealed slice
    contract lists no Gmail tool, so mailbox scopes would widen consent without
    any code path ever using them. They are therefore opt-in only, through the
    explicit ``include_gmail=true`` request parameter.

    ``GOOGLE_OAUTH_SCOPES`` is deliberately *filtered* against the identity
    allowlist instead of being trusted as-is: a stale or over-broad value
    (Gmail, Drive, full ``calendar``/``contacts``) must not re-enter the
    request through configuration. The resulting list keeps a stable order and
    is de-duplicated.
    """
    settings = settings or get_settings()
    scopes = [
        scope
        for scope in settings.google_oauth_scopes.split()
        if scope in _IDENTITY_SCOPES
    ]
    scopes += [settings.google_contacts_scope, settings.google_calendar_scope]
    if include_gmail:
        scopes += [
            settings.google_gmail_read_scope,
            settings.google_gmail_send_scope,
        ]
    return list(dict.fromkeys(scopes))


def granted_requested_scopes(requested: Sequence[str], granted: str) -> list[str]:
    """Return the requested scopes Google actually granted.

    The token response's ``scope`` field is authoritative: a consent screen can
    withhold scopes (the user unticks a permission, or an incremental grant
    returns only the new ones). The stored value must therefore be the
    intersection of what KAI asked for and what Google granted, kept in the
    canonical requested spelling and order, de-duplicated.

    Matching is identity-alias aware: ``email`` and
    ``https://www.googleapis.com/auth/userinfo.email`` (likewise ``profile`` and
    its userinfo URL) are the same grant under two spellings.

    Only requested scopes are stored. Extras Google may add on its own (for
    example ``userinfo.profile``) are not part of the app's consent contract and
    are deliberately dropped. This also keeps the value inside the
    ``String(255)`` column: the 4-scope default serializes to 110 characters and
    the 6-scope Gmail opt-in to 200.
    """
    granted_ids = {_scope_identity(scope) for scope in granted.split()}
    matched: list[str] = []
    seen: set[str] = set()
    for scope in requested:
        identity = _scope_identity(scope)
        if identity in granted_ids and identity not in seen:
            seen.add(identity)
            matched.append(scope)
    return matched


def missing_core_scopes(
    granted: Sequence[str], *, settings: Settings | None = None
) -> list[str]:
    """Return the required Contacts/Calendar scopes absent from ``granted``.

    Identity scopes are intentionally excluded: identity is proven separately by
    the userinfo call (``sub`` + ``email`` + ``email_verified``), while a missing
    Contacts or Calendar grant means the slice cannot work at all and the
    account must not be bound. Matching is identity-alias aware so a grant
    spelling the same scope through a userinfo URL still counts.
    """
    settings = settings or get_settings()
    granted_ids = {_scope_identity(scope) for scope in granted}
    required = [settings.google_contacts_scope, settings.google_calendar_scope]
    return [scope for scope in required if _scope_identity(scope) not in granted_ids]


@router.get("/connect", dependencies=[Depends(google_oauth_linking_guard)])
async def google_connect(
    response: Response,
    include_gmail: bool = False,
    current: User = Depends(get_current_user),
) -> dict:
    settings = get_settings()
    scopes = build_connect_scopes(include_gmail=include_gmail, settings=settings)
    binding = secrets.token_urlsafe(32)
    state = create_state(
        user_id=current.id,
        tenant_id=current.tenant_id,
        scopes=scopes,
        binding_hash=hash_binding(binding),
    )
    authorization_url = build_authorization_url(
        scopes=scopes,
        redirect_uri=settings.google_oauth_redirect_uri,
        state=state,
        code_challenge=code_challenge(binding),
    )
    # The raw secret goes only into the cookie. It is never part of ``state``,
    # the authorization URL, or a log line; the URL carries its digest and its
    # PKCE challenge. The same secret is the ``code_verifier`` the callback
    # sends to Google's token endpoint.
    response.set_cookie(
        OAUTH_BINDING_COOKIE,
        binding,
        max_age=STATE_TTL_SECONDS,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )
    return {"authorization_url": authorization_url}


def _expired_cookie(name: str) -> str:
    """Return a ``Set-Cookie`` value that deletes one hardened cookie."""
    response = Response()
    response.delete_cookie(name, path="/", secure=True, httponly=True, samesite="lax")
    return response.headers["set-cookie"]


def _expired_binding_cookie() -> str:
    """Return a ``Set-Cookie`` value that deletes the browser-binding cookie."""
    return _expired_cookie(OAUTH_BINDING_COOKIE)


def _expired_pending_cookie() -> str:
    """Return a ``Set-Cookie`` value that deletes the pending-confirmation cookie."""
    return _expired_cookie(PENDING_COOKIE)


def _binding_failure(status_code: int, detail: str) -> HTTPException:
    """Build a terminal callback failure that also clears the binding cookie.

    Only used *after* the binding was verified: every path that ends the
    transaction must retire the secret, or a stale cookie would keep a replay
    window open.
    """
    return HTTPException(
        status_code=status_code,
        detail=detail,
        headers={"set-cookie": _expired_binding_cookie()},
    )


def _pending_failure(status_code: int, detail: str) -> HTTPException:
    """Build a permanent pending-transaction failure that clears its cookie."""
    return HTTPException(
        status_code=status_code,
        detail=detail,
        headers={"set-cookie": _expired_pending_cookie()},
    )


@router.get("/callback", dependencies=[Depends(google_oauth_linking_guard)])
async def google_callback(
    request: Request,
    state: str | None = None,
    code: str | None = None,
    error: str | None = None,
    binding: str | None = Cookie(default=None, alias=OAUTH_BINDING_COOKIE),
    session: AsyncSession = Depends(get_session),
    transport: httpx.AsyncClient = Depends(get_google_http_client),
) -> Response:
    """Prove the Google identity and start a pending confirmation.

    This endpoint deliberately does **not** write ``google_accounts``. It ends
    with a ``303`` to the same-origin confirmation page and a fresh
    ``__Host-kai-google-pending`` cookie, and the link only happens when the
    signed-in user confirms there.
    """
    if state is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="missing oauth state"
        )
    try:
        claims = validate_state(state)
    except InvalidStateError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid or expired oauth state",
        ) from exc

    # Before any database lookup and before the code is exchanged: a browser
    # that never started this transaction cannot complete it. The cookie is
    # deliberately NOT deleted here, so a link opened in the wrong browser
    # cannot cancel the transaction the right one still has in flight.
    if binding is None or not verify_binding(claims, binding):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid or expired oauth state",
        )

    user = await get_user_for_tenant(
        session, tenant_id=claims.tenant_id, user_id=claims.user_id
    )
    if user is None:
        raise _binding_failure(
            status.HTTP_400_BAD_REQUEST, "account not found for this tenant"
        )

    # Google's own error redirect is only meaningful once the state and the
    # browser binding are proven; before that it is just attacker-controlled
    # text. It ends the transaction without creating anything.
    if error is not None:
        raise _binding_failure(
            status.HTTP_400_BAD_REQUEST,
            "google authorization was not completed",
        )

    if code is None:
        raise _binding_failure(
            status.HTTP_400_BAD_REQUEST, "missing authorization code"
        )

    requested = list(claims.scopes)
    if not requested:
        raise _binding_failure(
            status.HTTP_400_BAD_REQUEST, "oauth state has no requested scopes"
        )

    settings = get_settings()
    try:
        token = await client.exchange_code(
            transport,
            code=code,
            redirect_uri=settings.google_oauth_redirect_uri,
            code_verifier=binding,
        )
    except GoogleOAuthError as exc:
        # The provider message is never reflected: it can carry request details
        # and is not useful to a legitimate client.
        raise _binding_failure(
            status.HTTP_401_UNAUTHORIZED,
            "google authorization code exchange failed",
        ) from exc

    try:
        info = await client.fetch_userinfo(transport, access_token=token.access_token)
    except GoogleOAuthError as exc:
        raise _binding_failure(
            status.HTTP_401_UNAUTHORIZED, "google userinfo request failed"
        ) from exc

    if not info.email_verified:
        raise _binding_failure(
            status.HTTP_400_BAD_REQUEST, "google email is not verified"
        )

    # D28: an absent ``scope`` field is not evidence of a grant. The requested
    # list is stored as a defensive fallback, and ``scopes_from_provider``
    # records that Google did not confirm it, so nothing downstream claims a
    # confirmation the token response never carried.
    if token.scope is not None:
        effective = granted_requested_scopes(requested, token.scope)
        scopes_from_provider = True
    else:
        effective = requested
        scopes_from_provider = False

    if not effective:
        raise _binding_failure(
            status.HTTP_400_BAD_REQUEST,
            "google did not grant required permissions: []",
        )
    missing = missing_core_scopes(effective, settings=settings)
    if missing:
        raise _binding_failure(
            status.HTTP_400_BAD_REQUEST,
            f"google did not grant required permissions: {missing}",
        )

    owner = (
        await session.execute(
            select(GoogleAccount.user_id).where(
                GoogleAccount.tenant_id == user.tenant_id,
                GoogleAccount.google_sub == info.sub,
            )
        )
    ).scalar_one_or_none()
    if owner is not None and owner != user.id:
        # The identity exists under this tenant but belongs to somebody else.
        # Refuse before anything is stored, and never revoke: the account is
        # not ours to touch.
        raise _binding_failure(
            status.HTTP_409_CONFLICT,
            "google account is already linked to another user",
        )
    if owner is None and not token.refresh_token:
        # A brand-new link with no refresh token would die with its access
        # token and could never be renewed.
        raise _binding_failure(
            status.HTTP_400_BAD_REQUEST, "refresh_token_required"
        )

    try:
        _pending, raw_confirmation_token = await create_pending_transaction(
            session,
            tenant_id=user.tenant_id,
            user_id=user.id,
            state=state,
            google_sub=info.sub,
            google_email=info.email,
            email_verified=info.email_verified,
            scopes=" ".join(effective),
            scopes_from_provider=scopes_from_provider,
            access_token=token.access_token,
            # A reconnect whose response omits the refresh token is allowed to
            # start a pending transaction: confirmation merges the stored one.
            refresh_token=token.refresh_token,
            expires_in=token.expires_in,
        )
    except PendingStateReuseError as exc:
        raise _binding_failure(
            status.HTTP_400_BAD_REQUEST,
            "this authorization request has already been processed",
        ) from exc

    redirect = RedirectResponse(
        url=request.url_for("google_confirm_page").path,
        status_code=status.HTTP_303_SEE_OTHER,
    )
    redirect.headers["Cache-Control"] = "no-store"
    redirect.headers["Referrer-Policy"] = "no-referrer"
    # The OAuth binding has served its purpose; the confirmation cookie is the
    # only bearer value the browser keeps, and it addresses one pending row.
    redirect.delete_cookie(
        OAUTH_BINDING_COOKIE, path="/", secure=True, httponly=True, samesite="lax"
    )
    redirect.set_cookie(
        PENDING_COOKIE,
        raw_confirmation_token,
        max_age=PENDING_TTL_SECONDS,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )
    return redirect


def _require_same_origin(request: Request) -> None:
    """Reject a state-changing request whose declared ``Origin`` is foreign.

    Only an explicitly supplied ``Origin`` is checked. Browsers send it on
    cross-origin and same-origin ``POST`` alike, so a present-but-different value
    is never legitimate here. ``X-Forwarded-*`` is ignored: the expected origin is
    derived from the request the application actually received.

    Non-ASCII or scheme-less values are rejected before ``hmac.compare_digest``
    sees them. An invalid header is not a server error: it is a refused
    cross-origin request.
    """
    origin = request.headers.get("origin")
    if origin is None:
        return
    host = request.headers.get("host")
    request_url = request.url
    if (
        (host is not None and (not host or not host.isascii()))
        or request_url.scheme not in {"http", "https"}
        or not request_url.netloc
        or not request_url.netloc.isascii()
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="cross-origin request refused",
        )
    if host is not None and host != request_url.netloc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="cross-origin request refused",
        )
    if not origin or not origin.isascii():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="cross-origin request refused",
        )
    expected = f"{request_url.scheme}://{request_url.netloc}"
    try:
        origin_url = urlsplit(origin)
        expected_url = urlsplit(expected)
        origin_port = origin_url.port
        expected_port = expected_url.port
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="cross-origin request refused",
        ) from None
    if (
        origin_url.scheme not in {"http", "https"}
        or not origin_url.netloc
        or not origin_url.netloc.isascii()
        or origin_url.hostname is None
        or origin_url.path
        or origin_url.query
        or origin_url.fragment
        or origin_url.username is not None
        or origin_url.password is not None
        or origin_port != expected_port
        or expected_url.scheme not in {"http", "https"}
        or not expected_url.netloc
        or expected_url.hostname is None
        or expected_url.username is not None
        or expected_url.password is not None
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="cross-origin request refused",
        )
    if not hmac.compare_digest(origin, expected):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="cross-origin request refused",
        )


@router.get("/pending", dependencies=[Depends(google_oauth_linking_guard)])
async def google_pending(
    current: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    pending_token: str | None = Cookie(default=None, alias=PENDING_COOKIE),
) -> dict:
    """Return the safe projection of this user's pending Google transaction."""
    if pending_token is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="pending google authorization not found",
        )
    try:
        view = await load_pending_for_user(
            session,
            tenant_id=current.tenant_id,
            user_id=current.id,
            raw_confirmation_token=pending_token,
        )
    except (PendingNotFoundError, PendingNotPendingError) as exc:
        # Same answer for "does not exist" and "belongs to somebody else", so a
        # caller cannot probe for another user's transaction.
        raise _pending_failure(
            status.HTTP_404_NOT_FOUND, "pending google authorization not found"
        ) from exc
    except PendingExpiredError as exc:
        raise _pending_failure(
            status.HTTP_410_GONE, "google authorization expired"
        ) from exc
    payload = pending_view_payload(view)
    # The KAI identity is the other half of "what am I about to link"; it comes
    # from the authenticated request, never from the pending row.
    payload["kai_email"] = current.email
    return payload


@router.post("/pending/confirm", dependencies=[Depends(google_oauth_linking_guard)])
async def google_pending_confirm(
    request: Request,
    response: Response,
    current: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    pending_token: str | None = Cookie(default=None, alias=PENDING_COOKIE),
) -> dict:
    """Consume the pending transaction and link the Google account."""
    _require_same_origin(request)
    if pending_token is None:
        raise _pending_failure(
            status.HTTP_404_NOT_FOUND, "pending google authorization not found"
        )
    try:
        account = await confirm_pending_transaction(
            session,
            tenant_id=current.tenant_id,
            user_id=current.id,
            raw_confirmation_token=pending_token,
        )
    except (PendingNotFoundError, PendingNotPendingError) as exc:
        raise _pending_failure(
            status.HTTP_404_NOT_FOUND, "pending google authorization not found"
        ) from exc
    except PendingExpiredError as exc:
        raise _pending_failure(
            status.HTTP_410_GONE, "google authorization expired"
        ) from exc
    except GoogleAccountConflictError as exc:
        raise _pending_failure(
            status.HTTP_409_CONFLICT,
            "google account is already linked to another user",
        ) from exc
    except PendingRefreshRequiredError as exc:
        # ``reason`` is a bounded identifier from the pending row's own CHECK
        # set, never provider text.
        raise _pending_failure(status.HTTP_400_BAD_REQUEST, exc.reason) from exc
    except GoogleCredentialsError as exc:
        raise _pending_failure(
            status.HTTP_400_BAD_REQUEST, "credential_error"
        ) from exc
    except Exception as exc:
        # Transient (database/provider): the pending transaction is still
        # usable, so the browser keeps its cookie and may retry.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="google account linking is temporarily unavailable",
        ) from exc

    response.delete_cookie(
        PENDING_COOKIE, path="/", secure=True, httponly=True, samesite="lax"
    )
    response.headers["Cache-Control"] = "no-store"
    return {
        "connected": True,
        "account_id": str(account.id),
        "email": account.email,
        "google_sub": account.google_sub,
        "scopes": account.scopes.split(),
    }


@router.post("/pending/cancel", dependencies=[Depends(google_oauth_linking_guard)])
async def google_pending_cancel(
    request: Request,
    response: Response,
    current: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    pending_token: str | None = Cookie(default=None, alias=PENDING_COOKIE),
) -> dict:
    """Cancel locally. Nothing is revoked at Google."""
    _require_same_origin(request)
    if pending_token is None:
        raise _pending_failure(
            status.HTTP_404_NOT_FOUND, "pending google authorization not found"
        )
    try:
        pending_status = await cancel_pending_transaction(
            session,
            tenant_id=current.tenant_id,
            user_id=current.id,
            raw_confirmation_token=pending_token,
        )
    except PendingNotFoundError as exc:
        raise _pending_failure(
            status.HTTP_404_NOT_FOUND, "pending google authorization not found"
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="google account linking is temporarily unavailable",
        ) from exc

    response.delete_cookie(
        PENDING_COOKIE, path="/", secure=True, httponly=True, samesite="lax"
    )
    return {"cancelled": pending_status == "cancelled", "status": pending_status}


@router.get("/accounts")
async def google_accounts(
    current: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    result = await session.execute(
        select(GoogleAccount)
        .where(
            GoogleAccount.tenant_id == current.tenant_id,
            GoogleAccount.user_id == current.id,
        )
        .order_by(GoogleAccount.created_at, GoogleAccount.id)
    )
    accounts = result.scalars().all()
    return {
        "accounts": [
            {
                "account_id": str(account.id),
                "email": account.email,
                "google_sub": account.google_sub,
                "scopes": account.scopes,
                "token_expires_at": (
                    account.token_expires_at.isoformat()
                    if account.token_expires_at is not None
                    else None
                ),
            }
            for account in accounts
        ]
    }


#: The confirmation page. Deliberately one static document: no framework, no
#: build step, and no resource from another origin. Every value that comes from
#: the database or from Google is written with ``textContent``, never as markup.
#: The access token lives in this closure only - a reload asks for a new sign-in.
CONFIRM_PAGE_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Confirm Google link</title>
<style>
body { font-family: system-ui, sans-serif; margin: 2rem auto; max-width: 42rem;
       padding: 0 1rem; line-height: 1.45; }
dl { display: grid; grid-template-columns: max-content 1fr; gap: .35rem 1rem; }
dt { font-weight: 600; }
dd { margin: 0; overflow-wrap: anywhere; }
.hidden { display: none; }
label { display: block; margin: .5rem 0; }
input { padding: .35rem; width: 100%; box-sizing: border-box; }
button { margin: .5rem .5rem 0 0; padding: .45rem .9rem; }
</style>
</head>
<body>
<h1>Confirm Google account link</h1>
<p id="status" role="status">Loading&hellip;</p>

<section id="login-panel" class="hidden">
  <h2>Sign in to KAI</h2>
  <form id="login-form">
    <label>KAI email <input id="login-email" type="email"
      autocomplete="username" required></label>
    <label>Password <input id="login-password" type="password"
      autocomplete="current-password" required></label>
    <button type="submit">Sign in</button>
  </form>
</section>

<section id="review-panel" class="hidden">
  <h2>Review the link</h2>
  <dl>
    <dt>KAI account</dt><dd id="kai-email"></dd>
    <dt>Google account</dt><dd id="google-email"></dd>
    <dt>Google subject</dt><dd id="google-sub"></dd>
    <dt>Email verified</dt><dd id="email-verified"></dd>
    <dt>Permissions</dt><dd id="scopes"></dd>
    <dt>Permissions source</dt><dd id="scopes-source"></dd>
    <dt>Link status</dt><dd id="binding"></dd>
    <dt>Expires</dt><dd id="expires"></dd>
  </dl>
  <button id="confirm-button" type="button">Confirm link</button>
  <button id="cancel-button" type="button">Cancel</button>
</section>

<p id="result" role="status"></p>

<script>
(function () {
  "use strict";
  var API = "/api/v1";
  var token = null;

  function el(id) { return document.getElementById(id); }
  function setText(id, value) {
    el(id).textContent = value === null || value === undefined ? "" : String(value);
  }
  function show(id, visible) { el(id).classList.toggle("hidden", !visible); }
  function authHeaders() {
    var headers = {"Accept": "application/json"};
    if (token) { headers["Authorization"] = "Bearer " + token; }
    return headers;
  }
  function setStatus(message) { setText("status", message); }

  function request(path, options) {
    var opts = options || {};
    opts.credentials = "include";
    opts.headers = authHeaders();
    return fetch(API + path, opts).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (body) {
        return {ok: response.ok, status: response.status, body: body};
      });
    });
  }

  function renderPending(view) {
    setText("google-email", view.google_email);
    setText("google-sub", view.google_sub);
    setText("email-verified", view.email_verified ? "Yes" : "No");
    setText("scopes", (view.scopes || []).join(", "));
    setText("scopes-source", view.scopes_from_provider
      ? "Reported by Google"
      : "Requested fallback (Google did not report a grant)");
    setText("binding", view.binding === "reconnect"
      ? "Replaces the existing link"
      : "New link");
    setText("expires", view.expires_at);
    show("review-panel", true);
    setStatus("Review the details, then confirm or cancel.");
  }

  function loadIdentity() {
    return request("/auth/me", {method: "GET"}).then(function (result) {
      if (!result.ok) { throw new Error("identity"); }
      setText("kai-email", result.body.email);
    });
  }

  function loadPending() {
    return request("/auth/google/pending", {method: "GET"}).then(function (result) {
      if (result.status === 404) {
        setStatus("This link request is no longer available. Start again from KAI.");
        return;
      }
      if (result.status === 410) {
        setStatus("This link request expired. Start again from KAI.");
        return;
      }
      if (!result.ok) { setStatus("Could not load the link request."); return; }
      renderPending(result.body);
    });
  }

  function start() {
    setStatus("Sign in to review this Google link.");
    show("login-panel", true);
  }

  el("login-form").addEventListener("submit", function (event) {
    event.preventDefault();
    var email = el("login-email").value;
    var password = el("login-password").value;
    fetch(API + "/auth/login", {
      method: "POST",
      credentials: "include",
      headers: {"Content-Type": "application/json", "Accept": "application/json"},
      body: JSON.stringify({email: email, password: password})
    }).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (body) {
        if (!response.ok || !body.access_token) { throw new Error("login"); }
        token = body.access_token;
        show("login-panel", false);
        return loadIdentity().then(loadPending);
      });
    }).catch(function () {
      setStatus("Sign-in failed.");
    });
  });

  el("confirm-button").addEventListener("click", function () {
    request("/auth/google/pending/confirm", {method: "POST"}).then(function (result) {
      if (result.ok) {
        show("review-panel", false);
        setStatus("Google account linked.");
        return;
      }
      if (result.body && result.body.detail) {
        setText("result", String(result.body.detail));
      } else {
        setText("result", "Confirmation failed.");
      }
      if (result.status === 503) { setStatus("Temporary failure. Try confirming again."); return; }
      show("review-panel", false);
      setStatus("This link request can no longer be confirmed.");
    });
  });

  el("cancel-button").addEventListener("click", function () {
    request("/auth/google/pending/cancel", {method: "POST"}).then(function () {
      show("review-panel", false);
      setStatus("Cancelled. No Google account was linked.");
    });
  });

  start();
})();
</script>
</body>
</html>
"""


@router.get(
    "/confirm",
    response_class=HTMLResponse,
    dependencies=[Depends(google_oauth_linking_guard)],
    name="google_confirm_page",
)
async def google_confirm_page() -> HTMLResponse:
    """Serve the same-origin confirmation page.

    Unauthenticated on purpose: it carries no data and reads nothing. The page
    itself asks for a sign-in, keeps the Bearer token in memory only, and then
    calls the authenticated JSON endpoints.
    """
    return HTMLResponse(
        content=CONFIRM_PAGE_HTML,
        headers={
            "Content-Security-Policy": CONFIRM_PAGE_CSP,
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Frame-Options": "DENY",
            "X-Content-Type-Options": "nosniff",
        },
    )
