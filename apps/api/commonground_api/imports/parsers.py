"""Parse user-supplied listening history into a common shape.

Three formats, all things a user can export themselves without an OAuth flow --
which is the whole point of the Spotify-free onboarding promise. See
docs/data-sources.md.

Pure functions over bytes: no database, no network, no filesystem. Every quirk
handled below came from a real export shape, and each one has a fixture in
apps/api/tests/test_import_parsers.py.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

FORMATS = ("spotify_gdpr", "listenbrainz", "lastfm_csv")

# Spotify counts a stream as a play at 30 seconds, and an export is full of
# two-second skips while someone hunts for a track. Counting those as taste
# signal is how an importer decides you love the first song on every album.
DEFAULT_MIN_MS_PLAYED = 30_000

# A guard against a very large upload turning into unbounded memory. The API
# enforces a byte limit before it gets here; this is the second line, in terms
# the parser can express.
MAX_ROWS = 500_000


class UnknownFormatError(ValueError):
    """Raised when a payload matches none of the supported export formats."""


@dataclass(frozen=True)
class ParsedListen:
    """One play, normalised. Entity resolution to MBIDs happens later."""

    artist_name: str
    track_name: str
    listened_at: datetime
    album_name: str | None = None
    # ListenBrainz exports already carry MBIDs. Keeping them skips a fuzzy match
    # and is why a ListenBrainz import resolves far better than a CSV.
    recording_mbid: str | None = None
    artist_mbid: str | None = None
    ms_played: int | None = None


@dataclass
class ParseResult:
    listens: list[ParsedListen] = field(default_factory=list)
    # Counted by reason rather than totalled. "We dropped 412 rows" invites no
    # action; "412 were podcast episodes, 88 were skips under 30s" explains
    # itself, and the UI shows it.
    skipped: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1

    @property
    def total_skipped(self) -> int:
        return sum(self.skipped.values())


def _decode(payload: bytes) -> str:
    """Decode a user upload without dying on the usual encoding sins.

    utf-8-sig strips the BOM that Excel adds to any CSV it has touched, which is
    most Last.fm exports that have been opened once.
    """
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    # latin-1 maps every byte, so reaching here is not possible; be explicit
    # rather than relying on that.
    return payload.decode("utf-8", errors="replace")


def _utc(moment: datetime) -> datetime:
    """Force a timezone-aware UTC datetime.

    Naive timestamps appear in older Spotify exports, which are documented as
    UTC. Storing them naive would put a listen in the wrong hour depending on
    where the server runs.
    """
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


# ------------------------------------------------------------------ Spotify --


def _parse_spotify_timestamp(value: str) -> datetime | None:
    # Extended history uses ISO 8601 with a Z; the older StreamingHistory files
    # use "YYYY-MM-DD HH:MM" with no seconds and no zone.
    for parse_attempt in (
        lambda v: datetime.fromisoformat(v.replace("Z", "+00:00")),
        lambda v: datetime.strptime(v, "%Y-%m-%d %H:%M"),
        lambda v: datetime.strptime(v, "%Y-%m-%d %H:%M:%S"),
    ):
        try:
            return _utc(parse_attempt(value))
        except (ValueError, TypeError):
            continue
    return None


def parse_spotify_gdpr(payload: bytes, min_ms_played: int = DEFAULT_MIN_MS_PLAYED) -> ParseResult:
    """Parse a Spotify GDPR data export.

    Handles both shapes Spotify ships: the older `StreamingHistory*.json`
    (endTime / artistName / trackName / msPlayed) and the extended
    `Streaming_History_Audio_*.json` (ts / master_metadata_* / ms_played).

    This is the user requesting their own data under GDPR, not an API call. None
    of Spotify's restricted recommendation or audio-feature endpoints are
    involved.
    """
    result = ParseResult()
    try:
        rows = json.loads(_decode(payload))
    except json.JSONDecodeError as exc:
        raise UnknownFormatError(f"not valid JSON: {exc}") from exc

    if not isinstance(rows, list):
        raise UnknownFormatError("expected a JSON array of plays")

    for row in rows[:MAX_ROWS]:
        if not isinstance(row, dict):
            result.skip("malformed row")
            continue

        # Extended format first: it carries more, including podcast markers.
        track = row.get("master_metadata_track_name", row.get("trackName"))
        artist = row.get("master_metadata_album_artist_name", row.get("artistName"))
        album = row.get("master_metadata_album_album_name", row.get("albumName"))
        stamp = row.get("ts", row.get("endTime"))
        played = row.get("ms_played", row.get("msPlayed"))

        # Podcast and audiobook rows have null track metadata and an
        # episode_name instead. They are not music and must not become taste.
        if track is None or artist is None:
            if row.get("episode_name") or row.get("spotify_episode_uri"):
                result.skip("podcast or audiobook")
            else:
                result.skip("missing artist or track")
            continue

        if not isinstance(stamp, str):
            result.skip("missing timestamp")
            continue
        listened_at = _parse_spotify_timestamp(stamp)
        if listened_at is None:
            result.skip("unparseable timestamp")
            continue

        if isinstance(played, int) and played < min_ms_played:
            result.skip(f"played under {min_ms_played // 1000}s")
            continue

        result.listens.append(
            ParsedListen(
                artist_name=str(artist).strip(),
                track_name=str(track).strip(),
                album_name=album.strip() if isinstance(album, str) else None,
                listened_at=listened_at,
                ms_played=played if isinstance(played, int) else None,
            )
        )

    if len(rows) > MAX_ROWS:
        result.warnings.append(f"file had {len(rows)} rows; only the first {MAX_ROWS} were read")
    return result


# ------------------------------------------------------------ ListenBrainz --


def _listenbrainz_rows(document: Any) -> Iterator[dict]:
    """Yield listen objects from either export shape.

    An export is a bare JSON array, but the API's /1/user/x/listens response --
    which people also save and upload -- wraps them in {"payload": {"listens":
    [...]}}. Accepting both costs four lines and saves a confusing failure.
    """
    if isinstance(document, list):
        yield from (row for row in document if isinstance(row, dict))
    elif isinstance(document, dict):
        payload = document.get("payload", document)
        if isinstance(payload, dict):
            yield from (row for row in payload.get("listens", []) if isinstance(row, dict))


def parse_listenbrainz(payload: bytes, min_ms_played: int = DEFAULT_MIN_MS_PLAYED) -> ParseResult:
    """Parse a ListenBrainz export.

    The best of the three formats, because listens often already carry
    MusicBrainz IDs -- so they resolve exactly instead of by fuzzy name match.
    """
    result = ParseResult()
    try:
        document = json.loads(_decode(payload))
    except json.JSONDecodeError as exc:
        raise UnknownFormatError(f"not valid JSON: {exc}") from exc

    count = 0
    for row in _listenbrainz_rows(document):
        count += 1
        if count > MAX_ROWS:
            result.warnings.append(f"only the first {MAX_ROWS} listens were read")
            break

        metadata = row.get("track_metadata")
        if not isinstance(metadata, dict):
            result.skip("missing track_metadata")
            continue

        artist = metadata.get("artist_name")
        track = metadata.get("track_name")
        if not artist or not track:
            result.skip("missing artist or track")
            continue

        stamp = row.get("listened_at")
        if not isinstance(stamp, int) or isinstance(stamp, bool):
            result.skip("missing listened_at")
            continue
        try:
            listened_at = datetime.fromtimestamp(stamp, tz=UTC)
        except (OSError, OverflowError, ValueError):
            result.skip("unparseable listened_at")
            continue

        # MBIDs live in either place depending on the export's age, and
        # mbid_mapping is the one ListenBrainz resolved itself.
        extra = metadata.get("additional_info") or {}
        mapping = metadata.get("mbid_mapping") or {}
        recording_mbid = mapping.get("recording_mbid") or extra.get("recording_mbid")
        artist_mbids = mapping.get("artist_mbids") or extra.get("artist_mbids") or []

        result.listens.append(
            ParsedListen(
                artist_name=str(artist).strip(),
                track_name=str(track).strip(),
                album_name=(metadata.get("release_name") or None),
                listened_at=listened_at,
                recording_mbid=recording_mbid,
                artist_mbid=artist_mbids[0] if artist_mbids else None,
            )
        )
    return result


# ----------------------------------------------------------------- Last.fm --

# Exporters disagree about column names and about whether there is a header at
# all. Map every spelling seen in the wild onto one field.
_LASTFM_COLUMNS = {
    "artist": "artist",
    "artist_name": "artist",
    "album": "album",
    "album_name": "album",
    "track": "track",
    "track_name": "track",
    "title": "track",
    "song": "track",
    "date": "when",
    "utc_time": "when",
    "timestamp": "when",
    "uts": "when",
}

_LASTFM_DATE_FORMATS = (
    "%d %b %Y %H:%M",  # "06 Sep 2026 14:23" -- the common exporter's format
    "%d %b %Y, %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
)


def _parse_lastfm_date(value: str) -> datetime | None:
    value = value.strip()
    if not value:
        return None
    # Some exporters emit a Unix timestamp in the same column.
    if value.isdigit():
        try:
            return datetime.fromtimestamp(int(value), tz=UTC)
        except (OSError, OverflowError, ValueError):
            return None
    for fmt in _LASTFM_DATE_FORMATS:
        try:
            return _utc(datetime.strptime(value, fmt))
        except ValueError:
            continue
    try:
        return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def parse_lastfm_csv(payload: bytes, min_ms_played: int = DEFAULT_MIN_MS_PLAYED) -> ParseResult:
    """Parse a Last.fm scrobble export CSV.

    Scrobbles carry no play duration, so `min_ms_played` cannot apply -- Last.fm
    already only scrobbles a track that played long enough. It is accepted so
    every parser has one signature.
    """
    result = ParseResult()
    text = _decode(payload)
    if not text.strip():
        raise UnknownFormatError("file is empty")

    reader = csv.reader(io.StringIO(text))
    try:
        first = next(reader)
    except StopIteration as exc:
        raise UnknownFormatError("file is empty") from exc

    header = [_LASTFM_COLUMNS.get(cell.strip().lower().replace(" ", "_")) for cell in first]
    if header.count("artist") == 1 and header.count("track") == 1:
        columns: list[str | None] = header
    else:
        # No recognisable header: assume the common headerless ordering and
        # replay the row we just consumed.
        columns = ["artist", "album", "track", "when"]
        reader = csv.reader(io.StringIO(text))

    def index_of(name: str) -> int | None:
        return columns.index(name) if name in columns else None

    artist_at, track_at = index_of("artist"), index_of("track")
    album_at, when_at = index_of("album"), index_of("when")
    if artist_at is None or track_at is None:
        raise UnknownFormatError("no artist and track columns found")

    def cell(row: list[str], at: int | None) -> str | None:
        if at is None or at >= len(row):
            return None
        value = row[at].strip()
        return value or None

    for count, row in enumerate(reader):
        if count >= MAX_ROWS:
            result.warnings.append(f"only the first {MAX_ROWS} rows were read")
            break
        if not any(cell_value.strip() for cell_value in row):
            continue

        artist, track = cell(row, artist_at), cell(row, track_at)
        if not artist or not track:
            result.skip("missing artist or track")
            continue

        when = cell(row, when_at)
        listened_at = _parse_lastfm_date(when) if when else None
        if listened_at is None:
            # A scrobble without a usable timestamp is still evidence of taste,
            # but it cannot be ordered, and the recommender's split is
            # time-based. Dropping it is the honest choice.
            result.skip("missing or unparseable date")
            continue

        result.listens.append(
            ParsedListen(
                artist_name=artist,
                track_name=track,
                album_name=cell(row, album_at),
                listened_at=listened_at,
            )
        )
    return result


# ---------------------------------------------------------------- dispatch --

_PARSERS = {
    "spotify_gdpr": parse_spotify_gdpr,
    "listenbrainz": parse_listenbrainz,
    "lastfm_csv": parse_lastfm_csv,
}


def detect_format(payload: bytes) -> str:
    """Guess the export format from the payload itself.

    The upload form asks the user which format it is, so this exists to catch
    the case where they pick wrong -- which is common, because "my Spotify data"
    and "my Last.fm data" both arrive as a folder of files.
    """
    head = _decode(payload[:8192]).lstrip()
    if not head:
        raise UnknownFormatError("file is empty")

    if head[0] in "[{":
        # Both JSON formats. ListenBrainz nests under track_metadata; Spotify
        # has neither that nor a listened_at integer.
        if '"track_metadata"' in head or '"listened_at"' in head:
            return "listenbrainz"
        if '"ts"' in head or '"endTime"' in head or '"master_metadata_' in head:
            return "spotify_gdpr"
        raise UnknownFormatError("JSON, but not a recognised export shape")

    first_line = head.splitlines()[0] if head.splitlines() else ""
    if "," in first_line:
        return "lastfm_csv"
    raise UnknownFormatError("not JSON and not a comma-separated file")


def parse(
    payload: bytes,
    export_format: str,
    min_ms_played: int = DEFAULT_MIN_MS_PLAYED,
) -> ParseResult:
    if export_format not in _PARSERS:
        raise UnknownFormatError(f"unsupported format {export_format!r}; expected one of {FORMATS}")
    return _PARSERS[export_format](payload, min_ms_played)
