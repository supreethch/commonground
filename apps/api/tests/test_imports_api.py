"""End-to-end import tests: upload, resolve against the catalogue, build taste.

The parser unit tests cover format quirks. These cover the part that can only
break against a database -- matching, deduplication on re-upload, and the
promotion of played artists into a taste profile.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.db


def _listenbrainz(rows: list[dict]) -> bytes:
    return json.dumps(rows).encode()


def _lb_row(artist: str, track: str, when: int, mbid: str | None = None) -> dict:
    metadata: dict = {"artist_name": artist, "track_name": track}
    if mbid:
        metadata["mbid_mapping"] = {"recording_mbid": mbid}
    return {"listened_at": when, "track_metadata": metadata}


def _upload(client, account, payload: bytes, name: str = "history.json", fmt: str = "auto"):
    return client.post(
        "/api/imports",
        files={"file": (name, payload, "application/json")},
        data={"export_format": fmt},
        headers=account["headers"],
    )


def test_upload_matches_by_name_and_reports_the_rate(client, register, catalogue) -> None:
    account = register()
    catalogue(count=2)

    payload = _listenbrainz(
        [
            _lb_row("Test Artist 0", "Test Track 0", 1772000000),
            _lb_row("Test Artist 1", "Test Track 1", 1772000100),
            _lb_row("Nobody At All", "Nothing Here", 1772000200),
        ]
    )

    response = _upload(client, account, payload)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "done"
    assert body["rows_matched"] == 2
    assert body["match_rate"] == pytest.approx(2 / 3, abs=0.001)
    assert client.get("/api/profile", headers=account["headers"]).json()["listen_count"] == 2


def test_matching_survives_remaster_suffixes_and_accents(client, register, session) -> None:
    """The difference between a Spotify title and a MusicBrainz one is exactly
    this noise, and not handling it loses most of a real import."""
    account = register()
    artist_id = session.execute(
        text(
            "INSERT INTO artists (mbid, name, listen_count) "
            "VALUES (gen_random_uuid(), 'Bjork', 10) RETURNING id"
        )
    ).scalar_one()
    recording_id = session.execute(
        text(
            "INSERT INTO recordings (mbid, title, listen_count) "
            "VALUES (gen_random_uuid(), 'Army of Me', 10) RETURNING id"
        )
    ).scalar_one()
    session.execute(
        text("INSERT INTO recording_artists (recording_id, artist_id) VALUES (:r, :a)"),
        {"r": recording_id, "a": artist_id},
    )
    session.flush()

    payload = _listenbrainz([_lb_row("Björk", "Army of Me - 2011 Remaster", 1772000000)])

    response = _upload(client, account, payload)

    assert response.json()["rows_matched"] == 1


def test_mbid_matching_beats_a_name_that_does_not_agree(client, register, session) -> None:
    account = register()
    row = session.execute(
        text(
            "INSERT INTO recordings (mbid, title) "
            "VALUES ('11111111-1111-4111-8111-111111111111', 'Canonical Title') RETURNING id"
        )
    ).scalar_one()
    assert row
    session.flush()

    payload = _listenbrainz(
        [
            _lb_row(
                "Whatever The Export Called It",
                "A Completely Different Title",
                1772000000,
                mbid="11111111-1111-4111-8111-111111111111",
            )
        ]
    )

    assert _upload(client, account, payload).json()["rows_matched"] == 1


def test_re_uploading_an_overlapping_export_does_not_double_count(
    client, register, catalogue
) -> None:
    """Users re-upload overlapping exports. This is the normal case, not abuse."""
    account = register()
    catalogue(count=1)
    payload = _listenbrainz([_lb_row("Test Artist 0", "Test Track 0", 1772000000)])

    _upload(client, account, payload)
    _upload(client, account, payload)

    assert client.get("/api/profile", headers=account["headers"]).json()["listen_count"] == 1


def test_repeated_plays_build_a_taste_profile(client, register, catalogue) -> None:
    """One play is a shuffle; several is evidence. The threshold is 3."""
    account = register()
    data = catalogue(count=2)

    rows = [_lb_row("Test Artist 0", "Test Track 0", 1772000000 + n * 100) for n in range(5)]
    rows += [_lb_row("Test Artist 1", "Test Track 1", 1772100000)]

    _upload(client, account, _listenbrainz(rows))

    profile = client.get("/api/profile", headers=account["headers"]).json()
    artist_ids = {a["id"] for a in profile["artists"]}
    assert data["artist_ids"][0] in artist_ids, "5 plays should enter the profile"
    assert data["artist_ids"][1] not in artist_ids, "1 play should not"


def test_format_is_detected_when_not_declared(client, register, catalogue) -> None:
    account = register()
    catalogue(count=1)

    response = _upload(
        client, account, _listenbrainz([_lb_row("Test Artist 0", "Test Track 0", 1)])
    )

    assert response.status_code == 201
    assert response.json()["format"] == "listenbrainz"


def test_an_unrecognisable_file_is_rejected_with_a_reason(client, register) -> None:
    response = _upload(client, register(), b"this is not an export of anything", name="notes.txt")

    assert response.status_code == 422
    assert "recognise" in response.json()["detail"]


def test_an_empty_upload_is_rejected(client, register) -> None:
    assert _upload(client, register(), b"").status_code == 422


def test_an_oversized_upload_is_refused_before_parsing(client, register, settings) -> None:
    settings.max_import_bytes = 512
    response = _upload(client, register(), b"[" + b" " * 1024 + b"]")

    assert response.status_code == 413


def test_imports_are_listed_for_their_owner_only(client, register, catalogue) -> None:
    catalogue(count=1)
    mine, theirs = register(), register()

    _upload(client, mine, _listenbrainz([_lb_row("Test Artist 0", "Test Track 0", 1)]))

    assert len(client.get("/api/imports", headers=mine["headers"]).json()) == 1
    assert client.get("/api/imports", headers=theirs["headers"]).json() == []


def test_upload_requires_authentication(client) -> None:
    response = client.post("/api/imports", files={"file": ("h.json", b"[]", "application/json")})
    assert response.status_code == 401


def test_skip_reasons_are_reported_by_reason(client, register, catalogue) -> None:
    """'We dropped 412 rows' invites no action; naming the reasons does."""
    account = register()
    catalogue(count=1)

    payload = json.dumps(
        [
            {
                "ts": "2026-03-01T18:04:12Z",
                "master_metadata_track_name": "Test Track 0",
                "master_metadata_album_artist_name": "Test Artist 0",
                "ms_played": 120000,
            },
            {
                "ts": "2026-03-01T18:10:00Z",
                "master_metadata_track_name": None,
                "master_metadata_album_artist_name": None,
                "episode_name": "A Podcast",
                "ms_played": 900000,
            },
        ]
    ).encode()

    response = _upload(client, account, payload, fmt="spotify_gdpr")

    assert response.json()["skipped"] == {"podcast or audiobook": 1}
