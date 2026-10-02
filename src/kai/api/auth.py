from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from kai.auth.deps import get_current_user
from kai.auth.jwt import InvalidTokenError, create_access_token, decode_access_token
from kai.auth.revocation import revoke_token
from kai.auth.security import hash_password, verify_password
from kai.db.engine import get_session
from kai.db.models import Tenant, User

router = APIRouter(prefix="/auth", tags=["auth"])

_bearer = HTTPBearer(auto_error=False)

#: A real scrypt hash of a value nobody can supply, used to spend the same
#: CPU on a login attempt for an unregistered email as for a registered one.
#: Generated at import so the cost is paid once, not per request.
_DUMMY_HASH = hash_password("kai-timing-equalizer-not-a-real-password")


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class LoginRequest(BaseModel):
    # Bounded exactly like RegisterRequest. An unbounded password on this route
    # is a denial-of-service primitive: password verification is deliberately
    # expensive (scrypt), so a single unauthenticated request carrying a
    # multi-megabyte string makes the server do megabytes of hashing work that
    # it has no basis to refuse.
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: UUID


class LogoutResponse(BaseModel):
    revoked: bool = True


@router.post("/register", response_model=TokenResponse, status_code=201)
async def register(
    req: RegisterRequest,
    session: AsyncSession = Depends(get_session),
) -> TokenResponse:
    # MVP: 1 tenant == 1 user -> registering creates both at once.
    tenant = Tenant(name=req.email)
    session.add(tenant)
    try:
        await session.flush()
    except IntegrityError as exc:
        # The ``(tenant_id, email)``-style unique constraint owns this: a
        # duplicate email must surface as 409 Conflict, never as a 500.
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="an account with this email already exists",
        ) from exc

    user = User(
        tenant_id=tenant.id,
        email=req.email,
        password_hash=hash_password(req.password),
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)

    token = create_access_token(user_id=user.id, tenant_id=user.tenant_id)
    return TokenResponse(access_token=token, user_id=user.id)


@router.post("/login", response_model=TokenResponse)
async def login(req: LoginRequest, session: AsyncSession = Depends(get_session)) -> TokenResponse:
    # Password verification runs unconditionally, including when no such account
    # exists, so response time does not reveal whether an email is registered.
    # The previous version short-circuited on a missing user and skipped the
    # scrypt call entirely, which turned /auth/login into a free
    # user-enumeration oracle for anyone able to time a request.
    result = await session.execute(select(User).where(User.email == req.email))
    user = result.scalar_one_or_none()

    password_ok = verify_password(
        req.password, _DUMMY_HASH if user is None else user.password_hash
    )
    if user is None or not password_ok:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # A deactivated account must not be able to mint a fresh token. Login is
    # the only place a new token is issued, so this is where an existing
    # suspension has to be honoured.
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = create_access_token(user_id=user.id, tenant_id=user.tenant_id)
    return TokenResponse(access_token=token, user_id=user.id)


@router.post("/logout", response_model=LogoutResponse)
async def logout(
    current: User = Depends(get_current_user),
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: AsyncSession = Depends(get_session),
) -> LogoutResponse:
    """Revoke the presented token so it stops working immediately.

    Revocation is recorded against the token's own ``jti`` rather than against
    the user, so signing out on one device does not sign the account out
    everywhere: a personal agent holds long-lived tokens per client, and
    revoking the whole principal on every logout would silently break every
    other signed-in client. The record outlives nothing — it is only meaningful
    until the token's own expiry, after which it can be purged.
    """
    if credentials is None:  # pragma: no cover - get_current_user required it
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="missing bearer token"
        )
    try:
        claims = decode_access_token(credentials.credentials)
    except InvalidTokenError as exc:  # pragma: no cover - validated upstream
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="token is invalid or expired",
        ) from exc

    await revoke_token(session, claims=claims)
    return LogoutResponse(revoked=True)


@router.get("/me")
async def me(current: User = Depends(get_current_user)) -> dict:
    return {
        "user_id": current.id,
        "tenant_id": current.tenant_id,
        "email": current.email,
        "is_active": current.is_active,
    }
