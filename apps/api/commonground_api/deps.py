"""Shared FastAPI dependencies."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from .config import Settings, get_settings
from .db import get_session
from .models import User
from .security import decode_access_token

# auto_error=False so a missing header produces our own 401 with a useful body,
# rather than FastAPI's bare 403.
_bearer = HTTPBearer(auto_error=False)

SessionDep = Annotated[Session, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]

_UNAUTHENTICATED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="not authenticated",
    headers={"WWW-Authenticate": "Bearer"},
)


def current_user(
    session: SessionDep,
    settings: SettingsDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
) -> User:
    if credentials is None or not credentials.credentials:
        raise _UNAUTHENTICATED

    user_id = decode_access_token(settings, credentials.credentials)
    if user_id is None:
        raise _UNAUTHENTICATED

    user = session.get(User, user_id)
    if user is None:
        # A valid signature for a user that no longer exists: the account was
        # deleted while a token was still live. Same 401, no hint either way.
        raise _UNAUTHENTICATED
    return user


CurrentUser = Annotated[User, Depends(current_user)]


def writable_user(user: CurrentUser) -> User:
    """Reject writes from the seeded read-only demo accounts.

    Recruiters share one demo login. Without this, the first visitor to click
    "dislike" rewrites the taste profile every later visitor sees.
    """
    if user.is_demo:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="the demo account is read-only; sign up for a free account to change a profile",
        )
    return user


WritableUser = Annotated[User, Depends(writable_user)]
