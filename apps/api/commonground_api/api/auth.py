"""Signup, login, refresh and logout."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..deps import CurrentUser, SessionDep, SettingsDep
from ..models import RefreshToken, User
from ..schemas import (
    LoginRequest,
    RefreshRequest,
    SignupRequest,
    TokenResponse,
    UserResponse,
)
from ..security import (
    create_access_token,
    dummy_verify,
    hash_password,
    hash_token,
    new_refresh_token,
    verify_password,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])

_BAD_CREDENTIALS = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    # Deliberately identical for "no such account" and "wrong password". Telling
    # them apart is a free account-enumeration oracle.
    detail="incorrect email or password",
)


def _issue_tokens(session, settings, user: User) -> TokenResponse:
    access_token, expires_in = create_access_token(settings, user.id)
    refresh, refresh_hash = new_refresh_token()
    session.add(
        RefreshToken(
            user_id=user.id,
            token_hash=refresh_hash,
            expires_at=datetime.now(UTC) + timedelta(days=settings.refresh_token_days),
        )
    )
    session.commit()
    return TokenResponse(access_token=access_token, refresh_token=refresh, expires_in=expires_in)


@router.post("/signup", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
def signup(body: SignupRequest, session: SessionDep, settings: SettingsDep) -> TokenResponse:
    user = User(
        email=body.email,
        password_hash=hash_password(body.password),
        display_name=body.display_name,
    )
    session.add(user)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        # Signup cannot hide that an address is taken -- the user has to be told
        # to log in instead -- so this one endpoint does leak existence. Login
        # and password reset do not, which is where it would actually matter.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="an account with that email already exists",
        ) from None
    return _issue_tokens(session, settings, user)


@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest, session: SessionDep, settings: SettingsDep) -> TokenResponse:
    user = session.scalar(select(User).where(User.email == body.email))
    if user is None:
        # Spend the same Argon2 time as a real verification, so a missing
        # account and a wrong password cannot be told apart with a stopwatch.
        dummy_verify()
        raise _BAD_CREDENTIALS
    if not verify_password(body.password, user.password_hash):
        raise _BAD_CREDENTIALS
    return _issue_tokens(session, settings, user)


@router.post("/refresh", response_model=TokenResponse)
def refresh(body: RefreshRequest, session: SessionDep, settings: SettingsDep) -> TokenResponse:
    stored = session.scalar(
        select(RefreshToken).where(RefreshToken.token_hash == hash_token(body.refresh_token))
    )
    now = datetime.now(UTC)
    if stored is None or stored.revoked_at is not None or stored.expires_at <= now:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid or expired refresh token"
        )

    user = session.get(User, stored.user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="account no longer exists"
        )

    # Rotate: the presented token is spent. A refresh token that stays valid
    # after use means a stolen one works forever alongside the real client's.
    stored.revoked_at = now
    return _issue_tokens(session, settings, user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(body: RefreshRequest, session: SessionDep) -> None:
    stored = session.scalar(
        select(RefreshToken).where(RefreshToken.token_hash == hash_token(body.refresh_token))
    )
    # Always 204. Reporting whether the token existed tells an attacker holding
    # a guess whether it was real.
    if stored is not None and stored.revoked_at is None:
        stored.revoked_at = datetime.now(UTC)
        session.commit()


@router.get("/me", response_model=UserResponse)
def me(user: CurrentUser) -> User:
    return user
