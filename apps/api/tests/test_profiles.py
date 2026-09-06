"""Onboarding and profile tests."""

from __future__ import annotations

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.db


def test_artists_endpoint_defaults_to_the_most_listened(client, catalogue) -> None:
    """An empty search box asking someone to recall an artist from nothing is
    the worst possible first onboarding screen, so browse must work unqueried."""
    catalogue(count=3)

    response = client.get("/api/artists?limit=50")

    assert response.status_code == 200
    names = [a["name"] for a in response.json()]
    assert "Test Artist 0" in names
    counts = [a["listen_count"] for a in response.json()]
    assert counts == sorted(counts, reverse=True)


def test_artist_search_is_case_insensitive(client, catalogue) -> None:
    catalogue(count=2)

    response = client.get("/api/artists", params={"q": "test artist 1"})

    assert response.status_code == 200
    assert [a["name"] for a in response.json()] == ["Test Artist 1"]


def test_onboarding_stores_picks_and_marks_the_user_onboarded(client, register, catalogue) -> None:
    account = register()
    data = catalogue(count=2)

    response = client.put(
        "/api/profile/onboarding",
        json={"artist_ids": data["artist_ids"], "tag_ids": [data["tag_id"]]},
        headers=account["headers"],
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert {a["id"] for a in body["artists"]} == set(data["artist_ids"])
    assert [t["id"] for t in body["tags"]] == [data["tag_id"]]
    assert body["onboarded_at"] is not None

    assert client.get("/api/auth/me", headers=account["headers"]).json()["onboarded_at"]


def test_onboarding_requires_at_least_one_pick(client, register) -> None:
    account = register()

    response = client.put(
        "/api/profile/onboarding",
        json={"artist_ids": [], "tag_ids": []},
        headers=account["headers"],
    )

    assert response.status_code == 422


def test_onboarding_rejects_unknown_ids_rather_than_silently_dropping_them(
    client, register, catalogue
) -> None:
    """A silently ignored id looks like a saved preference that was never saved."""
    account = register()
    data = catalogue(count=1)

    response = client.put(
        "/api/profile/onboarding",
        json={"artist_ids": [*data["artist_ids"], 99_999_999]},
        headers=account["headers"],
    )

    assert response.status_code == 422
    assert "99999999" in response.text.replace(" ", "")


def test_re_running_onboarding_replaces_picks_but_keeps_imported_taste(
    client, register, catalogue, session
) -> None:
    """Onboarding must not silently erase a history the user uploaded."""
    account = register()
    data = catalogue(count=3)
    user_id = client.get("/api/auth/me", headers=account["headers"]).json()["id"]

    # Something learned from an import, not chosen at onboarding.
    session.execute(
        text(
            "INSERT INTO profile_artists (user_id, artist_id, weight, source) "
            "VALUES (:u, :a, 0.9, 'import')"
        ),
        {"u": user_id, "a": data["artist_ids"][2]},
    )
    session.flush()

    client.put(
        "/api/profile/onboarding",
        json={"artist_ids": [data["artist_ids"][0]]},
        headers=account["headers"],
    )
    second = client.put(
        "/api/profile/onboarding",
        json={"artist_ids": [data["artist_ids"][1]]},
        headers=account["headers"],
    )

    ids = {a["id"] for a in second.json()["artists"]}
    assert data["artist_ids"][0] not in ids, "the first onboarding pick should be replaced"
    assert data["artist_ids"][1] in ids
    assert data["artist_ids"][2] in ids, "the imported artist was wrongly erased"


def test_onboarding_deduplicates_repeated_ids(client, register, catalogue) -> None:
    account = register()
    data = catalogue(count=1)
    artist_id = data["artist_ids"][0]

    response = client.put(
        "/api/profile/onboarding",
        json={"artist_ids": [artist_id, artist_id, artist_id]},
        headers=account["headers"],
    )

    assert response.status_code == 200, response.text
    assert len(response.json()["artists"]) == 1


def test_profile_and_onboarding_require_authentication(client) -> None:
    assert client.get("/api/profile").status_code == 401
    assert client.put("/api/profile/onboarding", json={"artist_ids": [1]}).status_code == 401


def test_demo_accounts_cannot_change_their_profile(client, register, catalogue, session) -> None:
    """Recruiters share one demo login. Without this the first visitor to click
    anything rewrites the profile every later visitor sees."""
    account = register()
    data = catalogue(count=1)
    user_id = client.get("/api/auth/me", headers=account["headers"]).json()["id"]

    session.execute(text("UPDATE users SET is_demo = true WHERE id = :u"), {"u": user_id})
    session.flush()

    response = client.put(
        "/api/profile/onboarding",
        json={"artist_ids": data["artist_ids"]},
        headers=account["headers"],
    )

    assert response.status_code == 403
    # Reading is still allowed, or the demo would be useless.
    assert client.get("/api/profile", headers=account["headers"]).status_code == 200
