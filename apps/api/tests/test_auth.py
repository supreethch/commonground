"""Authentication tests.

Weighted toward the security properties rather than the happy path, because the
happy path fails loudly and the security properties fail silently.
"""

from __future__ import annotations

import pytest
from commonground_api.config import DEV_JWT_SECRET, Settings
from commonground_api.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)

pytestmark = pytest.mark.db


def test_signup_returns_tokens_and_lowercases_the_email(client) -> None:
    response = client.post(
        "/api/auth/signup",
        json={
            "email": "MiXeD@Example.COM",
            "password": "correct horse battery staple",
            "display_name": "  Spaced  ",
        },
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] > 0

    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"})
    # The database has a CHECK that email = lower(email); normalising in the
    # schema layer is what stops a mixed-case signup 500ing on that check.
    assert me.json()["email"] == "mixed@example.com"
    assert me.json()["display_name"] == "Spaced"


def test_signup_rejects_a_duplicate_email(client, register) -> None:
    account = register()
    response = client.post(
        "/api/auth/signup",
        json={"email": account["email"], "password": "another password here", "display_name": "X"},
    )
    assert response.status_code == 409


def test_signup_rejects_a_short_password(client) -> None:
    response = client.post(
        "/api/auth/signup",
        json={"email": "short@example.com", "password": "abc", "display_name": "X"},
    )
    assert response.status_code == 422


def test_login_succeeds_and_wrong_password_does_not(client, register) -> None:
    account = register()

    good = client.post(
        "/api/auth/login", json={"email": account["email"], "password": account["password"]}
    )
    assert good.status_code == 200

    bad = client.post(
        "/api/auth/login", json={"email": account["email"], "password": "not the password"}
    )
    assert bad.status_code == 401


def test_login_gives_the_same_answer_for_a_missing_account_and_a_wrong_password(
    client, register
) -> None:
    """Different responses here are a free account-enumeration oracle."""
    account = register()

    wrong_password = client.post(
        "/api/auth/login", json={"email": account["email"], "password": "not the password"}
    )
    no_such_account = client.post(
        "/api/auth/login", json={"email": "nobody@example.com", "password": "not the password"}
    )

    assert wrong_password.status_code == no_such_account.status_code == 401
    assert wrong_password.json() == no_such_account.json()


def test_refresh_rotates_and_spends_the_old_token(client, register) -> None:
    account = register()

    first = client.post("/api/auth/refresh", json={"refresh_token": account["refresh_token"]})
    assert first.status_code == 200
    rotated = first.json()["refresh_token"]
    assert rotated != account["refresh_token"]

    # A refresh token that still works after use means a stolen one works
    # forever alongside the real client's.
    replay = client.post("/api/auth/refresh", json={"refresh_token": account["refresh_token"]})
    assert replay.status_code == 401

    assert client.post("/api/auth/refresh", json={"refresh_token": rotated}).status_code == 200


def test_logout_revokes_the_refresh_token(client, register) -> None:
    account = register()

    assert (
        client.post(
            "/api/auth/logout", json={"refresh_token": account["refresh_token"]}
        ).status_code
        == 204
    )
    assert (
        client.post(
            "/api/auth/refresh", json={"refresh_token": account["refresh_token"]}
        ).status_code
        == 401
    )


def test_logout_of_an_unknown_token_is_still_204(client) -> None:
    """Reporting whether a token existed tells an attacker their guess was real."""
    response = client.post("/api/auth/logout", json={"refresh_token": "not-a-real-token"})
    assert response.status_code == 204


@pytest.mark.parametrize(
    "header",
    [None, "", "Bearer ", "Bearer not.a.jwt", "Basic dXNlcjpwYXNz"],
)
def test_me_rejects_every_flavour_of_missing_or_broken_credential(client, header) -> None:
    headers = {"Authorization": header} if header is not None else {}
    assert client.get("/api/auth/me", headers=headers).status_code == 401


def test_a_refresh_token_cannot_be_used_as_an_access_token(client, register) -> None:
    """They are different credentials with different lifetimes; mixing them up
    would let a 30-day token act as a 30-minute one."""
    account = register()
    response = client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {account['refresh_token']}"}
    )
    assert response.status_code == 401


# --------------------------------------------------------- unit-level bits --


def test_password_hashing_round_trips_and_salts() -> None:
    first, second = hash_password("same password"), hash_password("same password")
    assert first != second, "identical hashes mean the salt is not being applied"
    assert verify_password("same password", first)
    assert not verify_password("different password", first)


def test_verify_password_returns_false_on_a_corrupt_hash() -> None:
    """A malformed stored hash must fail closed, not raise a 500."""
    assert not verify_password("anything", "not-an-argon2-hash")


def test_access_token_signed_with_another_key_is_rejected() -> None:
    import uuid

    mine = Settings(jwt_secret="a" * 40, environment="test")
    theirs = Settings(jwt_secret="b" * 40, environment="test")
    user_id = uuid.uuid4()

    token, _ = create_access_token(theirs, user_id)

    assert decode_access_token(theirs, token) == user_id
    assert decode_access_token(mine, token) is None


def test_production_refuses_to_start_with_the_example_secret() -> None:
    """A predictable signing key is a complete auth bypass."""
    with pytest.raises(ValueError, match="JWT_SECRET"):
        Settings(environment="production", jwt_secret=DEV_JWT_SECRET)

    with pytest.raises(ValueError, match="JWT_SECRET"):
        Settings(environment="production", jwt_secret="too-short")

    # A real secret is accepted.
    assert Settings(environment="production", jwt_secret="x" * 48)
