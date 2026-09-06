"""Build the local catalogue from CC0 sources.

    python scripts/build_catalog.py                 # full build
    python scripts/build_catalog.py --skip-tags     # fast, no MusicBrainz pass
    python scripts/build_catalog.py --max-artists 50

Two stages, two upstreams:

  1. **ListenBrainz sitewide stats** (CC0) gives recordings, their artists and a
     real global listen count -- the popularity signal the recommender needs.
     Fast: a handful of requests.

  2. **MusicBrainz web service** (genres/tags are CC BY-NC-SA, see
     docs/data-sources.md) gives each artist's genres, which are the content
     features. Rate-limited to one request per second by MusicBrainz, so this is
     the slow stage. Responses are cached under data/cache/, making re-runs
     nearly free and the whole script resumable after an interruption.

Nothing written here is committed: `data/` is git-ignored, and the derived
artist-genre data carries a ShareAlike obligation that must not attach to an MIT
repository.

The sitewide stats endpoint returns at most the top 1000 entries per time range,
so unioning the ranges yields roughly 1,600 recordings. That is enough for
onboarding and a demo, and far too popularity-skewed for real collaborative
filtering -- M3 builds the larger slice from the listen dumps instead.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import psycopg

REPO_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = REPO_ROOT / "data" / "cache"

LB_STATS = "https://api.listenbrainz.org/1/stats/sitewide/recordings"
MB_ARTIST = "https://musicbrainz.org/ws/2/artist/{mbid}"

# Each range exposes its own top 1000; the union is what makes the catalogue
# bigger than any single call can return.
LB_RANGES = ("all_time", "year", "month", "week", "quarter")

# MusicBrainz asks for roughly one request per second. Going faster gets an
# application blocked, and this is a one-time build.
MB_DELAY_SECONDS = 1.1

USER_AGENT = os.environ.get(
    "USER_AGENT", "CommonGround/0.1 (set USER_AGENT to a real contact address)"
)


@dataclass
class Counts:
    artists: int = 0
    recordings: int = 0
    tags: int = 0
    artist_tags: int = 0
    links: int = 0
    tag_lookups_cached: int = 0
    tag_lookups_fetched: int = 0
    tag_lookups_failed: int = 0


def fetch_json(url: str, timeout: int = 30) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def fetch_json_with_backoff(url: str, attempts: int = 4, timeout: int = 30) -> dict:
    """Fetch, retrying when MusicBrainz throttles.

    Measured behaviour: MusicBrainz serves a short burst at ~0.5s per request,
    then rate-limits by stalling the connection for ~20s and returning 503. The
    first version of this script treated that as a permanent failure and dropped
    the artist, which silently produced a catalogue with missing genres and no
    indication anything had gone wrong -- the worst possible outcome, because
    the content features would just be quietly worse.

    Retry-After is honoured when present; otherwise the wait doubles.
    """
    delay = 2.0
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return fetch_json(url, timeout=timeout)
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code not in (429, 503):
                raise
            wait = delay
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            if retry_after and retry_after.isdigit():
                wait = float(retry_after)
            if attempt < attempts - 1:
                time.sleep(wait)
                delay *= 2
        except (urllib.error.URLError, OSError) as exc:
            last = exc
            if attempt < attempts - 1:
                time.sleep(delay)
                delay *= 2
    raise last if last else RuntimeError("unreachable")


# ---------------------------------------------------------------- stage 1 --


def collect_recordings(ranges: tuple[str, ...] = LB_RANGES) -> dict[str, dict]:
    """Union the sitewide top recordings across time ranges, keyed by MBID."""
    found: dict[str, dict] = {}
    for time_range in ranges:
        url = f"{LB_STATS}?count=1000&range={urllib.parse.quote(time_range)}"
        try:
            payload = fetch_json(url)["payload"]
        except (urllib.error.URLError, OSError, KeyError, ValueError) as exc:
            print(f"  {time_range:10} unavailable ({type(exc).__name__}); continuing")
            continue

        new = 0
        for row in payload.get("recordings", []):
            mbid = row.get("recording_mbid")
            artist_mbids = row.get("artist_mbids") or []
            # A recording with no MBID cannot be joined to anything later, and a
            # recording with no artist cannot carry content features.
            if not mbid or not artist_mbids:
                continue
            existing = found.get(mbid)
            if existing is None:
                found[mbid] = row
                new += 1
            else:
                # Ranges overlap. Keep the largest observed count so popularity
                # reflects all-time rather than whichever range came last.
                existing["listen_count"] = max(
                    existing.get("listen_count", 0), row.get("listen_count", 0)
                )
        print(f"  {time_range:10} +{new:4} new, running total {len(found)}")
        time.sleep(0.3)
    return found


def rank_artists(recordings: dict[str, dict]) -> list[str]:
    """Artist MBIDs, most-listened first.

    Ordering matters because the genre pass is the slow stage and is routinely
    interrupted or capped with --max-artists. Sorted by MBID -- which is what
    this did first -- a partial run tags an effectively random subset, so the
    catalogue ends up with genres for obscure artists and none for the ones
    every onboarding screen shows. Popularity order makes any prefix of the work
    the most useful prefix available.

    Ties break on MBID so the order is deterministic.
    """
    totals: dict[str, int] = {}
    for row in recordings.values():
        for mbid in row.get("artist_mbids") or []:
            totals[mbid] = totals.get(mbid, 0) + row.get("listen_count", 0)
    return sorted(totals, key=lambda mbid: (-totals[mbid], mbid))


# ---------------------------------------------------------------- stage 2 --


def artist_genres(mbid: str) -> dict | None:
    """Fetch one artist's genres and tags, cached on disk.

    Returns None when the lookup failed, which is different from an artist that
    genuinely has no tags -- the caller counts those separately so a bad network
    run is not mistaken for a sparse catalogue.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cached = CACHE_DIR / f"artist-{mbid}.json"
    if cached.exists():
        try:
            return json.loads(cached.read_text())
        except json.JSONDecodeError:
            cached.unlink()

    url = MB_ARTIST.format(mbid=mbid) + "?inc=tags+genres&fmt=json"
    try:
        document = fetch_json_with_backoff(url)
    except (urllib.error.URLError, OSError, ValueError):
        return None
    finally:
        time.sleep(MB_DELAY_SECONDS)

    record = {
        "name": document.get("name"),
        "country": document.get("country"),
        "sort_name": document.get("sort-name"),
        "genres": [
            {"name": g["name"], "count": g.get("count", 0)}
            for g in document.get("genres", [])
            if g.get("name")
        ],
        "tags": [
            {"name": t["name"], "count": t.get("count", 0)}
            for t in document.get("tags", [])
            if t.get("name")
        ],
    }
    cached.write_text(json.dumps(record))
    return record


# ------------------------------------------------------------------ write --


def search_url(artist: str, title: str) -> str:
    """A playback link that always resolves.

    MusicBrainz recording-level streaming relations turned out to be sparse
    (docs/data-sources.md), so this is the primary path. No audio is hosted; a
    real relation, where one exists, is preferred over this at read time.
    """
    query = urllib.parse.quote_plus(f"{artist} {title}")
    return f"https://www.youtube.com/results?search_query={query}"


def write_catalogue(
    conn: psycopg.Connection, recordings: dict[str, dict], genres_by_artist: dict[str, dict]
) -> Counts:
    counts = Counts()
    with conn.cursor() as cur:
        # --- artists -------------------------------------------------------
        artists: dict[str, dict] = {}
        for row in recordings.values():
            for mbid in row.get("artist_mbids") or []:
                artists.setdefault(
                    mbid, {"name": row.get("artist_name") or "Unknown", "listens": 0}
                )
                artists[mbid]["listens"] += row.get("listen_count", 0)

        for mbid, info in artists.items():
            detail = genres_by_artist.get(mbid) or {}
            cur.execute(
                """
                INSERT INTO artists (mbid, name, sort_name, country, listen_count)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (mbid) DO UPDATE SET
                    name = EXCLUDED.name,
                    sort_name = COALESCE(EXCLUDED.sort_name, artists.sort_name),
                    country = COALESCE(EXCLUDED.country, artists.country),
                    listen_count = GREATEST(artists.listen_count, EXCLUDED.listen_count)
                """,
                (
                    mbid,
                    detail.get("name") or info["name"],
                    detail.get("sort_name"),
                    (detail.get("country") or None),
                    info["listens"],
                ),
            )
            counts.artists += 1

        cur.execute("SELECT mbid, id FROM artists")
        artist_ids = {str(mbid): pk for mbid, pk in cur.fetchall()}

        # --- tags and artist_tags -------------------------------------------
        for mbid, detail in genres_by_artist.items():
            artist_id = artist_ids.get(mbid)
            if artist_id is None:
                continue
            # Genres are the curated vocabulary and the stronger signal; free
            # tags are kept but flagged, so the engine can weight them apart.
            entries = [(g["name"], g["count"], True) for g in detail.get("genres", [])]
            known = {name for name, _, _ in entries}
            entries += [
                (t["name"], t["count"], False)
                for t in detail.get("tags", [])
                if t["name"] not in known
            ]
            for name, count, is_genre in entries:
                cur.execute(
                    "INSERT INTO tags (name, is_genre) VALUES (%s, %s) "
                    "ON CONFLICT (name) DO UPDATE SET "
                    "is_genre = tags.is_genre OR EXCLUDED.is_genre "
                    "RETURNING id",
                    (name.strip().lower(), is_genre),
                )
                tag_id = cur.fetchone()[0]
                cur.execute(
                    "INSERT INTO artist_tags (artist_id, tag_id, weight) VALUES (%s, %s, %s) "
                    "ON CONFLICT (artist_id, tag_id) DO UPDATE SET weight = EXCLUDED.weight",
                    (artist_id, tag_id, float(max(count, 0))),
                )
                counts.artist_tags += 1

        cur.execute("SELECT count(*) FROM tags")
        counts.tags = cur.fetchone()[0]

        # --- recordings -----------------------------------------------------
        for mbid, row in recordings.items():
            cur.execute(
                """
                INSERT INTO recordings (mbid, title, listen_count)
                VALUES (%s, %s, %s)
                ON CONFLICT (mbid) DO UPDATE SET
                    title = EXCLUDED.title,
                    listen_count = GREATEST(recordings.listen_count, EXCLUDED.listen_count)
                RETURNING id
                """,
                (mbid, (row.get("track_name") or "Unknown").strip(), row.get("listen_count", 0)),
            )
            recording_id = cur.fetchone()[0]
            counts.recordings += 1

            for position, artist_mbid in enumerate(row.get("artist_mbids") or []):
                artist_id = artist_ids.get(artist_mbid)
                if artist_id is None:
                    continue
                cur.execute(
                    "INSERT INTO recording_artists (recording_id, artist_id, position) "
                    "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                    (recording_id, artist_id, position),
                )

            cur.execute(
                "INSERT INTO recording_links (recording_id, url, provider, source) "
                "VALUES (%s, %s, 'youtube', 'search_url') ON CONFLICT DO NOTHING",
                (
                    recording_id,
                    search_url(row.get("artist_name") or "", row.get("track_name") or ""),
                ),
            )
            counts.links += 1

        # A recording inherits its artists' tags. Cheap, and it is what lets a
        # track be scored on content before anyone has interacted with it.
        cur.execute(
            """
            INSERT INTO recording_tags (recording_id, tag_id, weight)
            SELECT ra.recording_id, at.tag_id, max(at.weight)
            FROM recording_artists ra
            JOIN artist_tags at ON at.artist_id = ra.artist_id
            GROUP BY ra.recording_id, at.tag_id
            ON CONFLICT (recording_id, tag_id) DO UPDATE SET weight = EXCLUDED.weight
            """
        )
    conn.commit()
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-tags", action="store_true", help="skip the MusicBrainz genre pass")
    parser.add_argument(
        "--max-artists", type=int, default=None, help="cap the number of artists tagged"
    )
    args = parser.parse_args(argv)

    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2

    print("stage 1: ListenBrainz sitewide recordings (CC0)")
    recordings = collect_recordings()
    if not recordings:
        print("no recordings collected; is the network up?", file=sys.stderr)
        return 1

    artist_mbids = rank_artists(recordings)
    print(f"  {len(recordings)} recordings, {len(artist_mbids)} distinct artists")

    genres_by_artist: dict[str, dict] = {}
    counts = Counts()
    if not args.skip_tags:
        targets = artist_mbids[: args.max_artists] if args.max_artists else artist_mbids
        cached_count = sum(1 for m in targets if (CACHE_DIR / f"artist-{m}.json").exists())
        print(
            f"\nstage 2: MusicBrainz genres for {len(targets)} artists "
            f"({cached_count} already cached, ~1 req/s for the rest)"
        )
        for index, mbid in enumerate(targets, start=1):
            was_cached = (CACHE_DIR / f"artist-{mbid}.json").exists()
            detail = artist_genres(mbid)
            if detail is None:
                counts.tag_lookups_failed += 1
            else:
                genres_by_artist[mbid] = detail
                if was_cached:
                    counts.tag_lookups_cached += 1
                else:
                    counts.tag_lookups_fetched += 1
            if index % 50 == 0 or index == len(targets):
                print(f"  {index}/{len(targets)}")

    print("\nstage 3: writing to Postgres")
    with psycopg.connect(url) as conn:
        written = write_catalogue(conn, recordings, genres_by_artist)

    written.tag_lookups_cached = counts.tag_lookups_cached
    written.tag_lookups_fetched = counts.tag_lookups_fetched
    written.tag_lookups_failed = counts.tag_lookups_failed

    print(
        f"\nartists            {written.artists}\n"
        f"recordings         {written.recordings}\n"
        f"tags               {written.tags}\n"
        f"artist_tags        {written.artist_tags}\n"
        f"playback links     {written.links}\n"
        f"genre lookups      {written.tag_lookups_fetched} fetched, "
        f"{written.tag_lookups_cached} cached, {written.tag_lookups_failed} failed"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
