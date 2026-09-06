"""Password hashing, token minting and token verification.

Two token types with deliberately different properties:

  * The **access token** is a short-lived signed JWT. It is not stored, so it
    cannot be revoked -- which is why it is short-lived.
  * The **refresh token** is opaque random bytes. Only its SHA-256 hash is
    stored, so a leaked database hands out no live sessions, and revoking one
    is a row update.

Refresh tokens are hashed with SHA-256 rather than Argon2 on purpose: they are
128 bits of uniform randomness, not a guessable human password, so the slow
hash buys nothing and would put a deliberate delay on every token refresh.
Passwords get Argon2id, because those *are* guessable.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from .config import Settings

_hasher = PasswordHasher()

# 32 bytes, URL-safe. Long enough that guessing is not a threat model.
_TOKEN_BYTES = 32


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError, ValueError):
        return False


def dummy_verify() -> None:
    """Burn one Argon2 verification against a throwaway hash.

    Called when a login names an address with no account. Without it, a missing
    user returns in microseconds and a real user takes ~50ms, which is a usable
    account-enumeration oracle for anyone with a stopwatch.
    """
    try:
        _hasher.verify(_DUMMY_HASH, "not the password")
    except (VerifyMismatchError, InvalidHashError, ValueError):
        pass


_DUMMY_HASH = _hasher.hash("dummy password for constant-time login")


def create_access_token(settings: Settings, user_id: uuid.UUID) -> tuple[str, int]:
    """Return (token, seconds until it expires)."""
    now = datetime.now(UTC)
    expires_at = now + timedelta(minutes=settings.access_token_minutes)
    payload = {
        "sub": str(user_id),
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        "typ": "access",
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, settings.access_token_minutes * 60


def decode_access_token(settings: Settings, token: str) -> uuid.UUID | None:
    """Return the user id, or None if the token is unusable for any reason."""
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            # Pinning the algorithm is what stops an attacker presenting a token
            # signed with "none", or an HS256 token against an RS256 public key.
            algorithms=[settings.jwt_algorithm],
            options={"require": ["exp", "sub"]},
        )
    except jwt.InvalidTokenError:
        return None
    if payload.get("typ") != "access":
        # A refresh token must not be usable as an access token.
        return None
    try:
        return uuid.UUID(payload["sub"])
    except (KeyError, ValueError):
        return None


def new_refresh_token() -> tuple[str, str]:
    """Return (token to give the client, hash to store)."""
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    return token, hash_token(token)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_invite_token() -> tuple[str, str]:
    """Same construction as a refresh token; it is also a bearer credential."""
    return new_refresh_token()
