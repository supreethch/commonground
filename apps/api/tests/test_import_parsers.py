"""Tests for the listening-history import parsers.

Every fixture here is a shape a real export actually produces. The parsers are
pure functions over bytes, so none of this needs a database, a network or an
upload -- which is why the awkward cases (podcasts, BOMs, naive timestamps,
two-second skips) can be covered properly instead of hand-waved.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from commonground_api.imports import (
    UnknownFormatError,
    detect_format,
    parse,
    parse_lastfm_csv,
    parse_listenbrainz,
    parse_spotify_gdpr,
)

# ------------------------------------------------------------------ Spotify --


def _spotify_extended(**overrides) -> dict:
    row = {
        "ts": "2026-03-01T18:04:12Z",
        "master_metadata_track_name": "Two Weeks",
        "master_metadata_album_artist_name": "FKA twigs",
        "master_metadata_album_album_name": "LP1",
        "ms_played": 241000,
    }
    row.update(overrides)
    return row


def test_spotify_extended_format() -> None:
    payload = json.dumps([_spotify_extended()]).encode()

    result = parse_spotify_gdpr(payload)

    assert len(result.listens) == 1
    listen = result.listens[0]
    assert listen.artist_name == "FKA twigs"
    assert listen.track_name == "Two Weeks"
    assert listen.album_name == "LP1"
    assert listen.ms_played == 241000
    assert listen.listened_at == datetime(2026, 3, 1, 18, 4, 12, tzinfo=UTC)


def test_spotify_legacy_streaminghistory_format() -> None:
    """The older export: different keys, and a naive timestamp."""
    payload = json.dumps(
        [
            {
                "endTime": "2026-03-01 18:04",
                "artistName": "Nujabes",
                "trackName": "Aruarian Dance",
                "msPlayed": 200000,
            }
        ]
    ).encode()

    result = parse_spotify_gdpr(payload)

    assert len(result.listens) == 1
    listen = result.listens[0]
    assert listen.artist_name == "Nujabes"
    # The naive timestamp must come back tz-aware, or the same listen lands in
    # a different hour depending on where the server runs.
    assert listen.listened_at.tzinfo is not None
    assert listen.listened_at == datetime(2026, 3, 1, 18, 4, tzinfo=UTC)


def test_podcast_rows_are_skipped_and_counted_as_podcasts() -> None:
    """Podcasts have null track metadata. They are not taste."""
    payload = json.dumps(
        [
            _spotify_extended(),
            {
                "ts": "2026-03-01T19:00:00Z",
                "master_metadata_track_name": None,
                "master_metadata_album_artist_name": None,
                "episode_name": "Some Podcast Episode",
                "spotify_episode_uri": "spotify:episode:abc",
                "ms_played": 1800000,
            },
        ]
    ).encode()

    result = parse_spotify_gdpr(payload)

    assert len(result.listens) == 1
    assert result.skipped == {"podcast or audiobook": 1}


def test_short_plays_are_skipped_at_the_thirty_second_boundary() -> None:
    """An export is full of two-second skips; counting them invents taste."""
    payload = json.dumps(
        [
            _spotify_extended(ms_played=2000),
            _spotify_extended(ms_played=29_999),
            _spotify_extended(ms_played=30_000),
        ]
    ).encode()

    result = parse_spotify_gdpr(payload)

    assert len(result.listens) == 1, "30_000ms is a play; anything under it is not"
    assert result.skipped == {"played under 30s": 2}


def test_short_play_threshold_is_configurable() -> None:
    payload = json.dumps([_spotify_extended(ms_played=5000)]).encode()

    assert parse_spotify_gdpr(payload, min_ms_played=0).listens
    assert not parse_spotify_gdpr(payload, min_ms_played=10_000).listens


def test_spotify_rows_missing_a_timestamp_are_skipped_not_crashed() -> None:
    payload = json.dumps(
        [
            _spotify_extended(ts=None),
            _spotify_extended(ts="not a date"),
            "a bare string where an object should be",
        ]
    ).encode()

    result = parse_spotify_gdpr(payload)

    assert result.listens == []
    assert result.skipped["missing timestamp"] == 1
    assert result.skipped["unparseable timestamp"] == 1
    assert result.skipped["malformed row"] == 1


def test_spotify_rejects_a_json_object_where_an_array_belongs() -> None:
    with pytest.raises(UnknownFormatError):
        parse_spotify_gdpr(b'{"not": "an array"}')


def test_invalid_json_raises_a_useful_error() -> None:
    with pytest.raises(UnknownFormatError, match="not valid JSON"):
        parse_spotify_gdpr(b"{definitely not json")


# ------------------------------------------------------------ ListenBrainz --


def test_listenbrainz_export_keeps_mbids() -> None:
    """The reason a ListenBrainz import resolves better than a CSV."""
    payload = json.dumps(
        [
            {
                "listened_at": 1772000000,
                "track_metadata": {
                    "artist_name": "Radiohead",
                    "track_name": "Idioteque",
                    "release_name": "Kid A",
                    "mbid_mapping": {
                        "recording_mbid": "d8f2f5f6-0000-4000-8000-000000000001",
                        "artist_mbids": ["a74b1b7f-71a5-4011-9441-d0b5e4122711"],
                    },
                },
            }
        ]
    ).encode()

    result = parse_listenbrainz(payload)

    listen = result.listens[0]
    assert listen.recording_mbid == "d8f2f5f6-0000-4000-8000-000000000001"
    assert listen.artist_mbid == "a74b1b7f-71a5-4011-9441-d0b5e4122711"
    assert listen.album_name == "Kid A"
    assert listen.listened_at == datetime.fromtimestamp(1772000000, tz=UTC)


def test_listenbrainz_reads_mbids_from_additional_info_too() -> None:
    """Older exports put them there instead of in mbid_mapping."""
    payload = json.dumps(
        [
            {
                "listened_at": 1772000000,
                "track_metadata": {
                    "artist_name": "Aphex Twin",
                    "track_name": "Xtal",
                    "additional_info": {"recording_mbid": "d8f2f5f6-0000-4000-8000-000000000002"},
                },
            }
        ]
    ).encode()

    result = parse_listenbrainz(payload)

    assert result.listens[0].recording_mbid == "d8f2f5f6-0000-4000-8000-000000000002"


def test_listenbrainz_accepts_the_api_response_wrapper() -> None:
    """People save /1/user/x/listens and upload that, not only an export."""
    payload = json.dumps(
        {
            "payload": {
                "listens": [
                    {
                        "listened_at": 1772000000,
                        "track_metadata": {
                            "artist_name": "Boards of Canada",
                            "track_name": "Roygbiv",
                        },
                    }
                ]
            }
        }
    ).encode()

    result = parse_listenbrainz(payload)

    assert len(result.listens) == 1
    assert result.listens[0].track_name == "Roygbiv"


def test_listenbrainz_skips_rows_without_a_usable_timestamp() -> None:
    payload = json.dumps(
        [
            {"track_metadata": {"artist_name": "A", "track_name": "B"}},
            {
                "listened_at": "not an int",
                "track_metadata": {"artist_name": "A", "track_name": "B"},
            },
            {"listened_at": 1772000000, "track_metadata": {"artist_name": "", "track_name": "B"}},
            {"listened_at": 1772000000},
        ]
    ).encode()

    result = parse_listenbrainz(payload)

    assert result.listens == []
    assert result.skipped["missing listened_at"] == 2
    assert result.skipped["missing artist or track"] == 1
    assert result.skipped["missing track_metadata"] == 1


# ----------------------------------------------------------------- Last.fm --


def test_lastfm_csv_with_a_header() -> None:
    payload = (
        b"artist,album,track,date\n"
        b'"Phoebe Bridgers","Stranger in the Alps","Motion Sickness","06 Sep 2026 14:23"\n'
    )

    result = parse_lastfm_csv(payload)

    listen = result.listens[0]
    assert listen.artist_name == "Phoebe Bridgers"
    assert listen.track_name == "Motion Sickness"
    assert listen.album_name == "Stranger in the Alps"
    assert listen.listened_at == datetime(2026, 9, 6, 14, 23, tzinfo=UTC)


def test_lastfm_csv_without_a_header_uses_the_conventional_column_order() -> None:
    """The first row must not be silently eaten as a header."""
    payload = b'"MGMT","Oracular Spectacular","Kids","06 Sep 2026 14:23"\n'

    result = parse_lastfm_csv(payload)

    assert len(result.listens) == 1, "the first data row was consumed as a header"
    assert result.listens[0].track_name == "Kids"


def test_lastfm_csv_tolerates_an_excel_bom() -> None:
    """Most exports have been opened in Excel at least once."""
    payload = "﻿artist,album,track,date\nBjörk,Post,Army of Me,06 Sep 2026 14:23\n".encode()

    result = parse_lastfm_csv(payload)

    assert result.listens[0].artist_name == "Björk", "a BOM leaked into the first column"


def test_lastfm_csv_accepts_alternative_column_spellings_and_unix_dates() -> None:
    payload = b"Artist Name,Track Name,UTC time\nCaribou,Odessa,1772000000\n"

    result = parse_lastfm_csv(payload)

    assert result.listens[0].artist_name == "Caribou"
    assert result.listens[0].listened_at == datetime.fromtimestamp(1772000000, tz=UTC)


def test_lastfm_rows_without_a_parseable_date_are_dropped_and_counted() -> None:
    """The evaluation split is time-ordered, so an unorderable row is useless."""
    payload = b"artist,album,track,date\nA,B,C,\nD,E,F,gibberish\nG,H,I,06 Sep 2026 14:23\n"

    result = parse_lastfm_csv(payload)

    assert len(result.listens) == 1
    assert result.skipped["missing or unparseable date"] == 2


def test_lastfm_blank_lines_are_ignored_not_counted_as_skips() -> None:
    payload = b"artist,album,track,date\n\nA,B,C,06 Sep 2026 14:23\n\n"

    result = parse_lastfm_csv(payload)

    assert len(result.listens) == 1
    assert result.total_skipped == 0


def test_lastfm_empty_file_raises() -> None:
    with pytest.raises(UnknownFormatError, match="empty"):
        parse_lastfm_csv(b"   \n  \n")


# ---------------------------------------------------------------- dispatch --


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (b'[{"ts":"2026-03-01T18:04:12Z","master_metadata_track_name":"x"}]', "spotify_gdpr"),
        (b'[{"endTime":"2026-03-01 18:04","artistName":"x"}]', "spotify_gdpr"),
        (b'[{"listened_at":1772000000,"track_metadata":{}}]', "listenbrainz"),
        (b"artist,album,track,date\nA,B,C,D\n", "lastfm_csv"),
    ],
)
def test_detect_format(payload: bytes, expected: str) -> None:
    assert detect_format(payload) == expected


def test_detect_format_refuses_to_guess_at_unrecognised_json() -> None:
    with pytest.raises(UnknownFormatError):
        detect_format(b'[{"something": "else"}]')


def test_detect_format_rejects_an_empty_upload() -> None:
    with pytest.raises(UnknownFormatError):
        detect_format(b"")


def test_parse_rejects_an_unsupported_format_name() -> None:
    with pytest.raises(UnknownFormatError, match="unsupported format"):
        parse(b"[]", "apple_music")


def test_every_parser_returns_utc_aware_timestamps() -> None:
    """A naive datetime anywhere here becomes a wrong hour in the database."""
    payloads = {
        "spotify_gdpr": json.dumps([_spotify_extended()]).encode(),
        "listenbrainz": json.dumps(
            [{"listened_at": 1772000000, "track_metadata": {"artist_name": "A", "track_name": "B"}}]
        ).encode(),
        "lastfm_csv": b"artist,album,track,date\nA,B,C,06 Sep 2026 14:23\n",
    }
    for export_format, payload in payloads.items():
        result = parse(payload, export_format)
        assert result.listens, export_format
        for listen in result.listens:
            assert listen.listened_at.tzinfo is not None, export_format
            assert listen.listened_at.utcoffset().total_seconds() == 0, export_format
