"""Deferred Google account linking: the pending-transaction authority.

The Google callback proves two things at once: control of a Google identity (a
code exchanged for tokens plus a verified userinfo response) and control of the
browser that started the flow (D33's binding cookie and D35's PKCE verifier).
Neither is the same as *consent by the signed-in KAI user* to attach that Google
identity to their KAI account. Treating the callback as the binding decision is
the consent-linking gap this module closes.

The flow is therefore split:

1. :func:`create_pending_transaction` records the proven identity and its
   freshly issued credentials, encrypted, as a ``pending`` row. Nothing is
   written to ``google_accounts``.
2. The signed-in user reviews the identity on a same-origin confirmation page and
   :func:`confirm_pending_transaction` applies it atomically, or
   :func:`cancel_pending_transaction` discards it locally.

Two bearer values reach the browser and only their digests are stored:

* the raw **confirmation token**, in a ``__Host-`` cookie scoped to this one
  transaction (see :data:`PENDING_COOKIE_NAME_BASE` usage in the API layer);
* the signed OAuth ``state``.

Consequences that callers must respect:

* Nothing here commits a ``google_accounts`` write on its own. ``confirm`` is the
  only path that consumes a pending row, and it is serialised by ``SELECT ...
  FOR UPDATE`` on that row plus the unique constraint on
  ``(tenant_id, google_sub)``.
* A transient failure leaves the row ``pending`` so the user can retry; a
  permanent failure (ownership conflict, no renewable grant, unreadable
  credentials) marks it ``failed`` with a bounded reason and clears the stored
  temporary credentials.
* No raw token, ``state``, verifier, or credential ever appears in a response,
  a log line, or a URL built here.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final
from uuid import UUID

from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from kai.db.engine import async_session_factory
from kai.db.models import GoogleAccount, GoogleOAuthPending
from kai.google.crypto import decrypt_credentials, encrypt_credentials
from kai.google.service import (
    GoogleAccountConflictError,
    GoogleCredentialsError,
    _constraint_name_for,
    bind_or_reconnect_google_account_in_transaction,
    merge_credentials,
)

#: How long a browser may take to review and confirm one transaction. Fixed on
#: purpose: this is one in-memory page transition, not an operator policy, and an
#: environment knob would only widen the window for a stolen confirmation cookie.
PENDING_TTL_SECONDS: Final[int] = 600

#: Terminal rows are useless once their secret is cleared; keeping a day of them
#: is enough for a support question ("did this link fail?") without turning the
#: table into an audit log.
TERMINAL_RETENTION: Final[timedelta] = timedelta(hours=24)

#: Advisory-lock key for the pending-transaction purge. One int8 in the
#: PostgreSQL user-defined range and KAI-specific, so it cannot collide with the
#: revocation purge's key. Changing it would let two purges run concurrently.
PURGE_LOCK_KEY: Final[int] = 0x4B41_495F_474F_4F47

#: Terminal statuses; a row in one of these can never be confirmed again.
TERMINAL_STATUSES: Final[tuple[str, ...]] = (
    "consumed",
    "cancelled",
    "expired",
    "failed",
)


class PendingNotFoundError(LookupError):
    """No pending transaction matches this token/tenant/user.

    Deliberately indistinguishable from "belongs to another user": exposing the
    difference would confirm that some other user has a transaction in flight.
    """


class PendingNotPendingError(RuntimeError):
    """The transaction exists but is no longer ``pending`` (reuse or replay)."""

    def __init__(self, status: str) -> None:
        super().__init__(f"pending transaction is {status}")
        self.status = status


class PendingExpiredError(RuntimeError):
    """The transaction existed but its TTL elapsed before it was confirmed."""


class PendingRefreshRequiredError(RuntimeError):
    """Confirmation would leave the account without a renewable grant.

    ``reason`` is one of the bounded ``google_oauth_pending`` failure reasons
    (``refresh_token_required`` or ``reconnect_required``), never provider text.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class PendingStateReuseError(RuntimeError):
    """This OAuth ``state`` already produced a pending transaction."""


@dataclass(frozen=True)
class PendingView:
    """The safe projection of a pending transaction.

    There is deliberately no credentials field: the only code that reads the
    encrypted blob is the confirmation path, and nothing that builds a response
    can accidentally include it.
    """

    id: UUID
    tenant_id: UUID
    user_id: UUID
    google_sub: str
    google_email: str
    email_verified: bool
    scopes: str
    scopes_from_provider: bool
    status: str
    expires_at: datetime
    binding: str


def hash_token(raw: str) -> str:
    """Return the SHA-256 hex digest of a bearer token held by the browser."""
    if not raw:
        raise ValueError("token must not be empty")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def new_confirmation_token() -> str:
    """Mint one raw confirmation token. Only its digest is persisted."""
    return secrets.token_urlsafe(32)


def _clear_credentials(pending: GoogleOAuthPending) -> None:
    """Drop the temporary bearer material from a row leaving ``pending``."""
    pending.credentials_encrypted = None
    pending.token_expires_at = None


def _lock_pending_by_hash(
    *,
    confirmation_hash: str,
    tenant_id: UUID,
    user_id: UUID,
):
    """Build the one deterministic lookup: digest + tenant + user, locked.

    ``populate_existing`` so a row already in this session's identity map is
    re-read under the lock instead of being served from a stale snapshot.
    """
    return (
        select(GoogleOAuthPending)
        .where(
            GoogleOAuthPending.confirmation_hash == confirmation_hash,
            GoogleOAuthPending.tenant_id == tenant_id,
            GoogleOAuthPending.user_id == user_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def _lock_and_load(
    session: AsyncSession,
    *,
    confirmation_hash: str,
    tenant_id: UUID,
    user_id: UUID,
) -> GoogleOAuthPending | None:
    result = await session.execute(
        _lock_pending_by_hash(
            confirmation_hash=confirmation_hash,
            tenant_id=tenant_id,
            user_id=user_id,
        )
    )
    return result.scalar_one_or_none()


async def _lock_account_by_sub(
    session: AsyncSession, *, tenant_id: UUID, google_sub: str
) -> GoogleAccount | None:
    """Lock the account row this transaction would reconnect, if it exists."""
    result = await session.execute(
        select(GoogleAccount)
        .where(
            GoogleAccount.tenant_id == tenant_id,
            GoogleAccount.google_sub == google_sub,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def expire_pending_if_needed(
    session: AsyncSession,
    *,
    pending: GoogleOAuthPending,
    now: datetime | None = None,
) -> bool:
    """Mark an elapsed pending row ``expired`` and clear its credentials.

    Returns whether the row was transitioned. The caller holds the row lock;
    this function commits, so a caller that sees ``True`` must stop: the row is
    terminal and its temporary credentials are gone.
    """
    current = now or datetime.now(UTC)
    if pending.status != "pending" or pending.expires_at > current:
        return False
    pending.status = "expired"
    pending.failure_reason = "expired"
    _clear_credentials(pending)
    await session.commit()
    return True


def _view(
    pending: GoogleOAuthPending, *, binding: str
) -> PendingView:
    return PendingView(
        id=pending.id,
        tenant_id=pending.tenant_id,
        user_id=pending.user_id,
        google_sub=pending.google_sub,
        google_email=pending.google_email,
        email_verified=pending.email_verified,
        scopes=pending.scopes,
        scopes_from_provider=pending.scopes_from_provider,
        status=pending.status,
        expires_at=pending.expires_at,
        binding=binding,
    )


def pending_view_payload(view: PendingView) -> dict:
    """Serialize a view for the API. The key set is the whole safe contract."""
    return {
        "google_email": view.google_email,
        "google_sub": view.google_sub,
        "email_verified": view.email_verified,
        "scopes": view.scopes.split(),
        "scopes_from_provider": view.scopes_from_provider,
        "binding": view.binding,
        "status": view.status,
        "expires_at": view.expires_at.isoformat(),
    }


async def _binding_kind(
    session: AsyncSession, *, tenant_id: UUID, google_sub: str
) -> str:
    """Return ``"reconnect"`` when this identity is already linked, else ``"new"``."""
    account = await session.execute(
        select(GoogleAccount.id).where(
            GoogleAccount.tenant_id == tenant_id,
            GoogleAccount.google_sub == google_sub,
        )
    )
    return "reconnect" if account.scalar_one_or_none() is not None else "new"


async def create_pending_transaction(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    user_id: UUID,
    state: str,
    google_sub: str,
    google_email: str,
    email_verified: bool,
    scopes: str,
    scopes_from_provider: bool,
    access_token: str,
    refresh_token: str | None,
    expires_in: int,
    now: datetime | None = None,
) -> tuple[GoogleOAuthPending, str]:
    """Persist a proven Google link as pending; never link the account.

    Returns the row and the **raw** confirmation token. The caller must put that
    token in the ``__Host-`` cookie and must not log or store it anywhere else;
    only :func:`hash_token` of it is in the database.
    """
    current = now or datetime.now(UTC)
    raw_confirmation_token = new_confirmation_token()

    credentials: dict[str, str] = {"access_token": access_token}
    if refresh_token:
        credentials["refresh_token"] = refresh_token

    pending = GoogleOAuthPending(
        tenant_id=tenant_id,
        user_id=user_id,
        confirmation_hash=hash_token(raw_confirmation_token),
        state_hash=hash_token(state),
        google_sub=google_sub,
        google_email=google_email,
        email_verified=email_verified,
        scopes=scopes,
        scopes_from_provider=scopes_from_provider,
        credentials_encrypted=encrypt_credentials(credentials),
        token_expires_at=current + timedelta(seconds=expires_in),
        status="pending",
        expires_at=current + timedelta(seconds=PENDING_TTL_SECONDS),
    )
    session.add(pending)
    try:
        await session.commit()
    except IntegrityError as exc:
        # Only the two pending-table unique constraints are expected here: a
        # replayed callback or confirmation token collision. Any other
        # integrity error is a real database fault and must not be dressed up as
        # a user-facing "state reuse" response.
        if _constraint_name_for(exc) not in {
            "uq_google_oauth_pending_state_hash",
            "uq_google_oauth_pending_confirmation_hash",
        }:
            raise
        await session.rollback()
        raise PendingStateReuseError(
            "this authorization request already has a pending transaction"
        ) from exc
    await session.refresh(pending)
    return pending, raw_confirmation_token


async def load_pending_for_user(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    user_id: UUID,
    raw_confirmation_token: str,
    now: datetime | None = None,
) -> PendingView:
    """Return the safe view of ``(tenant, user)``'s pending transaction.

    Raises :class:`PendingNotFoundError` for a missing or foreign transaction,
    :class:`PendingExpiredError` (after marking it expired) when the TTL elapsed,
    and :class:`PendingNotPendingError` once the row is terminal.
    """
    current = now or datetime.now(UTC)
    pending = await _lock_and_load(
        session,
        confirmation_hash=hash_token(raw_confirmation_token),
        tenant_id=tenant_id,
        user_id=user_id,
    )
    if pending is None:
        raise PendingNotFoundError("pending google transaction not found")
    if await expire_pending_if_needed(session, pending=pending, now=current):
        raise PendingExpiredError("pending google transaction expired")
    if pending.status == "expired":
        # Keep the terminal state distinguishable from an unknown token. The
        # view endpoint may have expired this row on the preceding request;
        # confirmation with the same bearer must still return 410, not 404.
        raise PendingExpiredError("pending google transaction expired")
    if pending.status != "pending":
        raise PendingNotPendingError(pending.status)
    binding = await _binding_kind(
        session, tenant_id=pending.tenant_id, google_sub=pending.google_sub
    )
    view = _view(pending, binding=binding)
    # Release the row lock before returning to the request: the view is all the
    # display needs, and a held lock would block a concurrent confirm.
    await session.commit()
    return view


async def _mark_failed(
    session: AsyncSession,
    *,
    confirmation_hash: str,
    tenant_id: UUID,
    user_id: UUID,
    reason: str,
) -> None:
    """Record a permanent failure in a dedicated transaction.

    The caller has already rolled back its own unit of work, so the account row
    is exactly as it was before the failed bind attempt. The cleanup uses a
    fresh session to keep that rollback/commit boundary isolated instead of
    reusing the transaction that is being aborted.
    """
    async with async_session_factory() as failure_session:
        pending = await _lock_and_load(
            failure_session,
            confirmation_hash=confirmation_hash,
            tenant_id=tenant_id,
            user_id=user_id,
        )
        if pending is not None and pending.status == "pending":
            pending.status = "failed"
            pending.failure_reason = reason
            _clear_credentials(pending)
        await failure_session.commit()


async def confirm_pending_transaction(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    user_id: UUID,
    raw_confirmation_token: str,
    now: datetime | None = None,
) -> GoogleAccount:
    """Consume a pending transaction and write the Google account atomically.

    The sequence is the invariant, not an implementation detail:

    1. lock the pending row by digest + tenant + user;
    2. require ``status='pending'`` and an unexpired ``expires_at``;
    3. lock the existing ``(tenant_id, google_sub)`` account row, if any;
    4. re-check ownership: another user is a conflict, the same user is a
       reconnect, no row is an insert;
    5. merge credentials so a new access token always wins and a stored refresh
       token is replaced only by a new one - never by ``NULL``; refuse when the
       result has no refresh token at all;
    6. bind through the transaction-aware service function (savepoint on the
       unique constraint, re-read the winner);
    7. mark the pending row ``consumed`` and clear its temporary credentials;
    8. commit once.

    A permanent failure rolls back, then marks the row ``failed`` with a bounded
    reason and clears the temporary credentials. A transient failure rolls back
    and leaves the row ``pending``, so the user can retry with the same cookie.
    """
    current = now or datetime.now(UTC)
    confirmation_hash = hash_token(raw_confirmation_token)

    try:
        pending = await _lock_and_load(
            session,
            confirmation_hash=confirmation_hash,
            tenant_id=tenant_id,
            user_id=user_id,
        )
        if pending is None:
            raise PendingNotFoundError("pending google transaction not found")
        if await expire_pending_if_needed(session, pending=pending, now=current):
            raise PendingExpiredError("pending google transaction expired")
        if pending.status == "expired":
            raise PendingExpiredError("pending google transaction expired")
        if pending.status != "pending":
            raise PendingNotPendingError(pending.status)

        existing = await _lock_account_by_sub(
            session, tenant_id=tenant_id, google_sub=pending.google_sub
        )
        if existing is not None and existing.user_id != user_id:
            await session.rollback()
            await _mark_failed(
                session,
                confirmation_hash=confirmation_hash,
                tenant_id=tenant_id,
                user_id=user_id,
                reason="ownership_conflict",
            )
            raise GoogleAccountConflictError(
                "google account is already bound to another user in this tenant"
            )

        if pending.credentials_encrypted is None or pending.token_expires_at is None:
            await session.rollback()
            await _mark_failed(
                session,
                confirmation_hash=confirmation_hash,
                tenant_id=tenant_id,
                user_id=user_id,
                reason="credential_error",
            )
            raise GoogleCredentialsError(
                "pending google transaction has no usable credentials"
            )

        try:
            staged = decrypt_credentials(pending.credentials_encrypted)
        except Exception as exc:  # noqa: BLE001 - any decrypt failure is terminal
            await session.rollback()
            await _mark_failed(
                session,
                confirmation_hash=confirmation_hash,
                tenant_id=tenant_id,
                user_id=user_id,
                reason="credential_error",
            )
            raise GoogleCredentialsError(
                "pending google credentials could not be read"
            ) from exc

        stored: dict = {}
        if existing is not None:
            try:
                stored = decrypt_credentials(existing.credentials_encrypted)
            except Exception as exc:  # noqa: BLE001 - any decrypt failure is terminal
                await session.rollback()
                await _mark_failed(
                    session,
                    confirmation_hash=confirmation_hash,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    reason="credential_error",
                )
                raise GoogleCredentialsError(
                    "stored google credentials could not be read"
                ) from exc

        new_access_token = staged.get("access_token")
        if not isinstance(new_access_token, str) or not new_access_token:
            await session.rollback()
            await _mark_failed(
                session,
                confirmation_hash=confirmation_hash,
                tenant_id=tenant_id,
                user_id=user_id,
                reason="credential_error",
            )
            raise GoogleCredentialsError(
                "pending google transaction has no access token"
            )
        new_refresh_token = staged.get("refresh_token")
        if not isinstance(new_refresh_token, str) or not new_refresh_token:
            new_refresh_token = None

        merged = merge_credentials(
            stored,
            access_token=new_access_token,
            refresh_token=new_refresh_token,
        )
        if not merged.get("refresh_token"):
            # No renewable grant would survive: a new account would be unusable
            # after an hour, and a reconnect would have dropped the stored one.
            reason = (
                "reconnect_required" if existing is not None else "refresh_token_required"
            )
            await session.rollback()
            await _mark_failed(
                session,
                confirmation_hash=confirmation_hash,
                tenant_id=tenant_id,
                user_id=user_id,
                reason=reason,
            )
            raise PendingRefreshRequiredError(reason)

        remaining = int((pending.token_expires_at - current).total_seconds())
        try:
            account = await bind_or_reconnect_google_account_in_transaction(
                session,
                tenant_id=tenant_id,
                user_id=user_id,
                google_sub=pending.google_sub,
                email=pending.google_email,
                scopes=pending.scopes,
                access_token=new_access_token,
                refresh_token=new_refresh_token,
                expires_in=max(0, remaining),
            )
        except GoogleAccountConflictError as exc:
            await session.rollback()
            await _mark_failed(
                session,
                confirmation_hash=confirmation_hash,
                tenant_id=tenant_id,
                user_id=user_id,
                reason="ownership_conflict",
            )
            raise GoogleAccountConflictError(
                "google account is already bound to another user in this tenant"
            ) from exc

        pending.status = "consumed"
        pending.consumed_at = current
        pending.failure_reason = None
        _clear_credentials(pending)
        await session.commit()
        return account
    except (
        PendingNotFoundError,
        PendingNotPendingError,
        PendingExpiredError,
        PendingRefreshRequiredError,
        GoogleAccountConflictError,
        GoogleCredentialsError,
    ):
        # Permanent outcomes were already recorded and their transaction
        # committed by the branch above (or there was nothing to write).
        raise
    except Exception:
        # Transient: leave the row pending so the same cookie can retry.
        await session.rollback()
        raise


async def cancel_pending_transaction(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    user_id: UUID,
    raw_confirmation_token: str,
    now: datetime | None = None,
) -> str:
    """Cancel a pending transaction locally and return the resulting status.

    Only a ``pending`` row moves to ``cancelled`` and loses its credentials. A
    terminal row is reported as-is. Cancellation is deliberately local: KAI does
    not revoke anything at Google, because the user has not yet linked (or
    unlinked) anything and a revoke would be a side effect on a grant KAI never
    adopted.
    """
    current = now or datetime.now(UTC)
    pending = await _lock_and_load(
        session,
        confirmation_hash=hash_token(raw_confirmation_token),
        tenant_id=tenant_id,
        user_id=user_id,
    )
    if pending is None:
        raise PendingNotFoundError("pending google transaction not found")
    if pending.status == "pending":
        pending.status = "cancelled"
        pending.cancelled_at = current
        _clear_credentials(pending)
        status = "cancelled"
    else:
        status = pending.status
    await session.commit()
    return status


async def purge_pending_transactions(
    session: AsyncSession, *, now: datetime | None = None
) -> tuple[int, int]:
    """Housekeeping: expire stale pending rows, delete old terminal ones.

    Idempotent and safe to run at any time. It takes the same kind of
    transaction-scoped advisory lock the revocation purge uses, so overlapping
    runs collapse into one instead of racing; a run that loses the lock returns
    ``(0, 0)`` without doing work.

    It returns ``(expired, deleted)``. Expiring first is what makes the
    credentials of an abandoned transaction disappear even if nobody ever opens
    the confirmation page again.
    """
    current = now or datetime.now(UTC)
    acquired = await session.scalar(
        text("SELECT pg_try_advisory_xact_lock(CAST(:key AS BIGINT))"),
        {"key": PURGE_LOCK_KEY},
    )
    if not acquired:
        await session.rollback()
        return 0, 0

    expired = await session.execute(
        update(GoogleOAuthPending)
        .where(
            GoogleOAuthPending.status == "pending",
            GoogleOAuthPending.expires_at <= current,
        )
        .values(
            status="expired",
            failure_reason="expired",
            credentials_encrypted=None,
            token_expires_at=None,
            updated_at=current,
        )
    )
    deleted = await session.execute(
        delete(GoogleOAuthPending).where(
            GoogleOAuthPending.status.in_(TERMINAL_STATUSES),
            GoogleOAuthPending.updated_at < current - TERMINAL_RETENTION,
        )
    )
    await session.commit()
    return (
        int(getattr(expired, "rowcount", 0) or 0),
        int(getattr(deleted, "rowcount", 0) or 0),
    )
