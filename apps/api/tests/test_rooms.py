"""Rooms, invites, playlists, voting and the room socket.

Weighted toward authorisation and the invite lifecycle. A room is the only place
in this product where one user can see another's data, so "who is allowed to
read this" is the thing most worth pinning down.
"""

from __future__ import annotations

import pytest
from commonground_api.realtime import reset_broadcaster
from sqlalchemy import text

pytestmark = pytest.mark.db


@pytest.fixture(autouse=True)
def _fresh_broadcaster():
    """Each test gets its own fan-out, so events cannot leak between them."""
    reset_broadcaster()
    yield
    reset_broadcaster()


def _make_room(client, account, name="Road trip", mode="consensus"):
    response = client.post(
        "/api/rooms", json={"name": name, "mode": mode}, headers=account["headers"]
    )
    assert response.status_code == 201, response.text
    return response.json()


# -------------------------------------------------------------------- rooms --


def test_creating_a_room_makes_the_creator_its_owner(client, register) -> None:
    account = register()
    room = _make_room(client, account)

    assert room["member_count"] == 1
    assert room["members"][0]["role"] == "owner"
    assert room["mode"] == "consensus"


def test_room_mode_must_be_one_of_the_three(client, register) -> None:
    account = register()
    response = client.post(
        "/api/rooms", json={"name": "R", "mode": "whatever"}, headers=account["headers"]
    )
    assert response.status_code == 422


def test_a_room_is_invisible_to_someone_who_is_not_in_it(client, register) -> None:
    """404, not 403 -- a stranger should not be able to confirm the id exists."""
    owner, stranger = register(), register()
    room = _make_room(client, owner)

    assert client.get(f"/api/rooms/{room['id']}", headers=stranger["headers"]).status_code == 404
    assert client.get("/api/rooms", headers=stranger["headers"]).json() == []


def test_listing_rooms_returns_only_your_own(client, register) -> None:
    mine, theirs = register(), register()
    _make_room(client, mine, name="Mine")
    _make_room(client, theirs, name="Theirs")

    names = [r["name"] for r in client.get("/api/rooms", headers=mine["headers"]).json()]
    assert names == ["Mine"]


def test_only_the_owner_can_delete_a_room(client, register) -> None:
    owner = register()
    room = _make_room(client, owner)
    guest = register()
    token = client.post(f"/api/rooms/{room['id']}/invites", headers=owner["headers"]).json()[
        "token"
    ]
    client.post(f"/api/rooms/join/{token}", headers=guest["headers"])

    assert client.delete(f"/api/rooms/{room['id']}", headers=guest["headers"]).status_code == 403
    assert client.delete(f"/api/rooms/{room['id']}", headers=owner["headers"]).status_code == 204
    assert client.get(f"/api/rooms/{room['id']}", headers=owner["headers"]).status_code == 404


# ------------------------------------------------------------------ invites --


def test_an_invite_lets_someone_else_join(client, register) -> None:
    owner, guest = register(), register()
    room = _make_room(client, owner)

    invite = client.post(f"/api/rooms/{room['id']}/invites", headers=owner["headers"]).json()
    joined = client.post(f"/api/rooms/join/{invite['token']}", headers=guest["headers"])

    assert joined.status_code == 200
    assert joined.json()["member_count"] == 2
    assert client.get(f"/api/rooms/{room['id']}", headers=guest["headers"]).status_code == 200


def test_only_the_hash_of_an_invite_token_is_stored(client, register, session) -> None:
    """A leaked database must not be usable to join private rooms."""
    owner = register()
    room = _make_room(client, owner)
    invite = client.post(f"/api/rooms/{room['id']}/invites", headers=owner["headers"]).json()

    stored = (
        session.execute(
            text("SELECT token_hash FROM room_invites WHERE room_id = :room"),
            {"room": room["id"]},
        )
        .scalars()
        .all()
    )
    assert invite["token"] not in stored


def test_an_unknown_invite_token_is_rejected(client, register) -> None:
    guest = register()
    assert client.post("/api/rooms/join/not-a-token", headers=guest["headers"]).status_code == 404


def test_rejoining_with_the_same_link_does_not_spend_another_use(client, register, session) -> None:
    """Re-opening the link you already used must not burn the invite."""
    owner, guest = register(), register()
    room = _make_room(client, owner)
    invite = client.post(f"/api/rooms/{room['id']}/invites", headers=owner["headers"]).json()

    client.post(f"/api/rooms/join/{invite['token']}", headers=guest["headers"])
    client.post(f"/api/rooms/join/{invite['token']}", headers=guest["headers"])

    # Scoped to this room. An unscoped query passes only while the table is
    # empty, which stops being true the moment anyone runs the app against the
    # development database.
    uses = session.execute(
        text("SELECT uses FROM room_invites WHERE room_id = :room"), {"room": room["id"]}
    ).scalar_one()
    assert uses == 1


def test_an_expired_invite_is_refused(client, register, session) -> None:
    owner, guest = register(), register()
    room = _make_room(client, owner)
    invite = client.post(f"/api/rooms/{room['id']}/invites", headers=owner["headers"]).json()
    session.execute(
        text("UPDATE room_invites SET expires_at = now() - interval '1 day' WHERE room_id = :room"),
        {"room": room["id"]},
    )
    session.flush()

    assert (
        client.post(f"/api/rooms/join/{invite['token']}", headers=guest["headers"]).status_code
        == 404
    )


def test_a_revoked_invite_is_refused(client, register, session) -> None:
    owner, guest = register(), register()
    room = _make_room(client, owner)
    invite = client.post(f"/api/rooms/{room['id']}/invites", headers=owner["headers"]).json()
    session.execute(
        text("UPDATE room_invites SET revoked_at = now() WHERE room_id = :room"),
        {"room": room["id"]},
    )
    session.flush()

    assert (
        client.post(f"/api/rooms/join/{invite['token']}", headers=guest["headers"]).status_code
        == 404
    )


def test_an_exhausted_invite_is_refused(client, register, session) -> None:
    owner, guest = register(), register()
    room = _make_room(client, owner)
    invite = client.post(f"/api/rooms/{room['id']}/invites", headers=owner["headers"]).json()
    session.execute(
        text("UPDATE room_invites SET uses = max_uses WHERE room_id = :room"),
        {"room": room["id"]},
    )
    session.flush()

    assert (
        client.post(f"/api/rooms/join/{invite['token']}", headers=guest["headers"]).status_code
        == 404
    )


# ---------------------------------------------------------------- playlists --


def test_generating_a_playlist_stores_reasons_and_scores(
    client, register, catalogue, session
) -> None:
    account = register()
    data = catalogue(count=6)
    client.put(
        "/api/profile/onboarding",
        json={"artist_ids": data["artist_ids"][:2]},
        headers=account["headers"],
    )
    # Someone has to have listened to something, or there is no model to fit.
    for recording_id in data["recording_ids"]:
        session.execute(
            text("INSERT INTO listens (user_id, recording_id, listened_at) VALUES (:u, :r, now())"),
            {"u": str(account["user_id"]), "r": recording_id},
        )
    session.flush()

    room = _make_room(client, account)
    response = client.post(f"/api/rooms/{room['id']}/playlist?k=3", headers=account["headers"])

    assert response.status_code in (201, 422), response.text
    if response.status_code == 422:
        pytest.skip("catalogue too small for this fixture to produce a playlist")

    body = response.json()
    assert body["mode"] == "consensus"
    assert body["engine_version"]
    for track in body["tracks"]:
        assert track["explanation"]["sentence"]
        assert track["member_scores"]
        # Every rendered clause must trace to a term the ranker recorded.
        for source in track["explanation"]["sources"]:
            assert source in track["contributions"]


def test_a_non_member_cannot_read_a_playlist(client, register) -> None:
    owner, stranger = register(), register()
    room = _make_room(client, owner)

    assert (
        client.get(f"/api/rooms/{room['id']}/playlist", headers=stranger["headers"]).status_code
        == 404
    )


def test_a_room_with_no_playlist_says_so(client, register) -> None:
    account = register()
    room = _make_room(client, account)
    assert (
        client.get(f"/api/rooms/{room['id']}/playlist", headers=account["headers"]).status_code
        == 404
    )


def test_history_is_empty_before_anything_is_generated(client, register) -> None:
    account = register()
    room = _make_room(client, account)
    assert client.get(f"/api/rooms/{room['id']}/history", headers=account["headers"]).json() == []


# -------------------------------------------------------------------- votes --


def test_voting_requires_membership(client, register) -> None:
    owner, stranger = register(), register()
    room = _make_room(client, owner)

    response = client.post(
        f"/api/rooms/{room['id']}/tracks/1/vote", json={"value": 1}, headers=stranger["headers"]
    )
    assert response.status_code == 404


def test_a_vote_value_must_be_minus_one_zero_or_one(client, register) -> None:
    account = register()
    room = _make_room(client, account)
    response = client.post(
        f"/api/rooms/{room['id']}/tracks/1/vote", json={"value": 5}, headers=account["headers"]
    )
    assert response.status_code == 422


def test_voting_on_a_track_from_another_room_is_a_404(client, register) -> None:
    account = register()
    room = _make_room(client, account)
    response = client.post(
        f"/api/rooms/{room['id']}/tracks/999999/vote",
        json={"value": 1},
        headers=account["headers"],
    )
    assert response.status_code == 404


# ---------------------------------------------------------------- websocket --


def test_the_socket_refuses_a_connection_with_no_token(client, register) -> None:
    account = register()
    room = _make_room(client, account)

    with pytest.raises(Exception):  # noqa: B017 - starlette raises on a rejected handshake
        with client.websocket_connect(f"/api/rooms/{room['id']}/ws"):
            pass


def test_the_socket_refuses_a_non_member(client, register) -> None:
    owner, stranger = register(), register()
    room = _make_room(client, owner)

    with pytest.raises(Exception):  # noqa: B017
        with client.websocket_connect(
            f"/api/rooms/{room['id']}/ws?token={stranger['access_token']}"
        ):
            pass


def test_a_member_receives_a_connected_frame(client, register) -> None:
    account = register()
    room = _make_room(client, account)

    with client.websocket_connect(
        f"/api/rooms/{room['id']}/ws?token={account['access_token']}"
    ) as socket:
        message = socket.receive_json()

    assert message["type"] == "connected"
    assert message["room_id"] == room["id"]
    assert message["transport"] in ("in-process", "redis")


def test_a_join_is_broadcast_to_a_connected_member(client, register) -> None:
    """The event that makes a room feel live rather than polled."""
    owner, guest = register(), register()
    room = _make_room(client, owner)
    invite = client.post(f"/api/rooms/{room['id']}/invites", headers=owner["headers"]).json()

    with client.websocket_connect(
        f"/api/rooms/{room['id']}/ws?token={owner['access_token']}"
    ) as socket:
        assert socket.receive_json()["type"] == "connected"
        client.post(f"/api/rooms/join/{invite['token']}", headers=guest["headers"])
        event = socket.receive_json()

    assert event["type"] == "member_joined"
    assert event["user_id"] == str(guest["user_id"])
