"""Build the production catalogue from the ListenBrainz dump dataset.

    python scripts/build_catalog_from_dataset.py --reset

This replaces the earlier two-step path (sitewide API for the catalogue, then a
separate listener import) with one build from the data the recommender is
actually evaluated on.

## Why the sitewide API was the wrong source

`build_catalog.py` reads ListenBrainz's *sitewide top N per time range*, which
caps at 1,000 rows per range -- so unioning every range yields 1,637 recordings
and no more. That is a picker, not a catalogue: with 1,637 tracks a room of six
runs out of candidates almost immediately, and the demo looked thin because it
was thin.

The dump dataset already holds **12,273 genuine tracks by 3,554 artists**, and
`enrich_tags.py` has genres for 91% of them. It was being used for offline
evaluation and ignored by the product, which is exactly backwards.

## The per-artist cap, and why it is not cosmetic

One day of ListenBrainz reflects one day's release cycle. On the day this slice
was taken, **BTS alone was 19.3% of all plays across 284 tracks**, and the wider
BTS ecosystem about 28%. A catalogue in those proportions makes every room --
whatever its members like -- return K-pop, and the recommender looks broken when
it is faithfully reporting a skewed sample.

Capping tracks per artist keeps the most-played `--max-per-artist` of each, so no
single release can own the catalogue. Interactions for the kept tracks are
preserved in full, so collaborative filtering loses nothing it could have used.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import sys
import urllib.parse
import uuid
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import psycopg

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "apps" / "api"))

from commonground_api.imports.matching import normalise  # noqa: E402

NO_LOGIN = "synthetic-listener-no-login"

# Fixed namespaces so an artist or recording keeps the same id across rebuilds
# -- and, more usefully here, so rows can be bulk-loaded with COPY and joined
# back by id afterwards instead of INSERT ... RETURNING one row at a time.
#
# That matters for the deploy: against a managed database on another continent,
# ~30,000 round trips is twenty minutes, while a COPY of the same data is a
# handful of statements.
ARTIST_NAMESPACE = uuid.UUID("6f9b1e2c-0a3d-4f7b-9c1e-5d8a2b4c6e70")
RECORDING_NAMESPACE = uuid.UUID("2c7e4a91-8b3f-4d6a-91e0-7f2b5c8d1a34")


def search_url(artist: str, title: str) -> str:
    """A playback link that always resolves. No audio is hosted."""
    return "https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(
        f"{artist} {title}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", default="data/datasets/lb-day")
    parser.add_argument(
        "--max-per-artist",
        type=int,
        default=8,
        help="cap tracks per artist so one release cannot own the catalogue",
    )
    parser.add_argument("--min-listeners", type=int, default=3)
    parser.add_argument("--max-listeners", type=int, default=1500)
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args(argv)

    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2

    directory = REPO_ROOT / args.dataset
    items = json.loads((directory / "items.json").read_text())
    arrays = np.load(directory / "interactions.npz")
    rows, cols, stamps = arrays["rows"], arrays["cols"], arrays["timestamps"]

    tags_path = directory / "artist_tags.json"
    artist_tags: dict[str, dict] = {}
    if tags_path.exists():
        artist_tags = json.loads(tags_path.read_text())["artists"]
    print(f"dataset  {len(items):,} tracks, genres for {len(artist_tags):,} artists")

    listeners_per_item = np.bincount(cols, minlength=len(items))

    # --- cap per artist -------------------------------------------------
    # Grouped on the *normalised* name, matching how artist rows are keyed.
    # Grouping on the raw string gave "Alice In Chains" and "alice in chains"
    # separate allowances, so the 8-track cap let 16 through before the two
    # merged into one artist row.
    by_artist: dict[str, list[int]] = collections.defaultdict(list)
    for index, item in enumerate(items):
        by_artist[normalise(item["artist"])].append(index)

    keep: set[int] = set()
    for indexes in by_artist.values():
        # Most-listened first, ties on index so the selection is deterministic.
        ordered = sorted(indexes, key=lambda i: (-listeners_per_item[i], i))
        keep.update(ordered[: args.max_per_artist])

    keep = {i for i in keep if listeners_per_item[i] >= args.min_listeners}
    print(
        f"catalogue {len(keep):,} tracks after a {args.max_per_artist}/artist cap "
        f"and a {args.min_listeners}-listener floor"
    )

    with psycopg.connect(url) as conn, conn.cursor() as cur:
        if args.reset:
            # Order matters only for readability; the foreign keys cascade.
            cur.execute("DELETE FROM users WHERE is_synthetic")
            cur.execute("DELETE FROM recordings")
            cur.execute("DELETE FROM artists")
            cur.execute("DELETE FROM tags")
            # Playlists outlive the recordings they point at, because
            # playlist_tracks cascades with the recording but the playlist row
            # does not. That leaves a playlist with no tracks -- which the room
            # then renders as an empty list rather than as "nothing generated
            # yet". A rebuilt catalogue invalidates them all.
            cur.execute("DELETE FROM playlists")
            print("  cleared the previous catalogue and any playlists built on it")

        # --- tags ---------------------------------------------------------
        tag_ids: dict[str, int] = {}
        for entry in artist_tags.values():
            for name in entry.get("genres", []):
                clean = name.strip().lower()
                if clean and clean not in tag_ids:
                    cur.execute(
                        "INSERT INTO tags (name, is_genre) VALUES (%s, true) "
                        "ON CONFLICT (name) DO UPDATE SET is_genre = true RETURNING id",
                        (clean,),
                    )
                    tag_ids[clean] = cur.fetchone()[0]

        # --- artists ------------------------------------------------------
        artist_plays: collections.Counter[str] = collections.Counter()
        for index in keep:
            artist_plays[items[index]["artist"]] += int(listeners_per_item[index])

        artist_ids: dict[str, int] = {}
        for name, plays in artist_plays.items():
            entry = artist_tags.get(normalise(name))

            # Identity is derived from the *name*, not from the MusicBrainz MBID
            # the enrichment found.
            #
            # enrich_tags.py matches aliases, so one MusicBrainz artist can be
            # the best match for several different names in the dump. Keying on
            # its MBID therefore collapsed distinct artists into one row -- the
            # catalogue came out with "Zero 7" owning 91 tracks and "RM" 48,
            # despite an 8-per-artist cap, because unrelated acts had been
            # merged under a shared MBID.
            #
            # uuid5 over the normalised name gives one row per artist and the
            # same id on every rebuild. The MusicBrainz id is still what the
            # genres came from; it is just not the primary key here.
            mbid = uuid.uuid5(ARTIST_NAMESPACE, normalise(name))

            cur.execute(
                "INSERT INTO artists (mbid, name, listen_count) VALUES (%s, %s, %s) "
                "ON CONFLICT (mbid) DO UPDATE SET "
                "  name = EXCLUDED.name, "
                "  listen_count = GREATEST(artists.listen_count, EXCLUDED.listen_count) "
                "RETURNING id",
                (str(mbid), name, plays),
            )
            artist_ids[name] = cur.fetchone()[0]

            if entry:
                for genre in entry.get("genres", []):
                    tag_id = tag_ids.get(genre.strip().lower())
                    if tag_id:
                        cur.execute(
                            "INSERT INTO artist_tags (artist_id, tag_id, weight) "
                            "VALUES (%s, %s, 1.0) ON CONFLICT DO NOTHING",
                            (artist_ids[name], tag_id),
                        )
        # Keyed by the raw name, but identity is uuid5 over the normalised
        # one, so "BTS" and "bts" are two keys and one row. Report rows.
        print(f"  {len(set(artist_ids.values())):,} artists")

        # --- recordings ---------------------------------------------------
        ordered = sorted(keep)
        recording_mbids = {
            index: uuid.uuid5(
                RECORDING_NAMESPACE,
                f"{normalise(items[index]['artist'])}\x1f{normalise(items[index]['track'])}",
            )
            for index in ordered
        }
        # A deterministic mbid means the rows can go in with one COPY and be
        # matched back afterwards, rather than a RETURNING per row.
        seen_mbids: set[uuid.UUID] = set()
        with cur.copy("COPY recordings (mbid, title, listen_count) FROM STDIN") as copy:
            for index in ordered:
                mbid = recording_mbids[index]
                if mbid in seen_mbids:
                    continue  # two dataset rows normalising to one track
                seen_mbids.add(mbid)
                copy.write_row(
                    (str(mbid), items[index]["track"][:300], int(listeners_per_item[index]))
                )

        cur.execute("SELECT mbid, id FROM recordings")
        by_mbid = {str(mbid): pk for mbid, pk in cur.fetchall()}
        item_to_recording = {
            index: by_mbid[str(recording_mbids[index])]
            for index in ordered
            if str(recording_mbids[index]) in by_mbid
        }

        with cur.copy(
            "COPY recording_artists (recording_id, artist_id, position) FROM STDIN"
        ) as copy:
            for index, recording_id in item_to_recording.items():
                if recording_mbids[index] in seen_mbids:
                    copy.write_row((recording_id, artist_ids[items[index]["artist"]], 0))
                    seen_mbids.discard(recording_mbids[index])

        with cur.copy(
            "COPY recording_links (recording_id, url, provider, source) FROM STDIN"
        ) as copy:
            for index, recording_id in item_to_recording.items():
                copy.write_row(
                    (
                        recording_id,
                        search_url(items[index]["artist"], items[index]["track"]),
                        "youtube",
                        "search_url",
                    )
                )
        print(f"  {len(item_to_recording):,} recordings")

        cur.execute(
            """
            INSERT INTO recording_tags (recording_id, tag_id, weight)
            SELECT ra.recording_id, at.tag_id, max(at.weight)
            FROM recording_artists ra
            JOIN artist_tags at ON at.artist_id = ra.artist_id
            GROUP BY ra.recording_id, at.tag_id
            ON CONFLICT DO NOTHING
            """
        )

        # --- listeners ----------------------------------------------------
        keep_mask = np.isin(cols, np.fromiter(item_to_recording, dtype=np.int64))
        kept_rows, kept_cols, kept_stamps = rows[keep_mask], cols[keep_mask], stamps[keep_mask]

        per_user: dict[int, list[tuple[int, int]]] = collections.defaultdict(list)
        for user, item, stamp in zip(kept_rows, kept_cols, kept_stamps, strict=True):
            per_user[int(user)].append((int(item), int(stamp)))

        eligible = sorted(
            (u for u, v in per_user.items() if len(v) >= 8),
            key=lambda u: (-len(per_user[u]), u),
        )[: args.max_listeners]

        written = 0
        listen_rows: list[tuple] = []
        for slot, user in enumerate(eligible):
            cur.execute(
                "INSERT INTO users (email, password_hash, display_name, is_synthetic) "
                "VALUES (%s, %s, %s, true) "
                "ON CONFLICT (email) DO UPDATE SET display_name = EXCLUDED.display_name "
                "RETURNING id",
                (f"listener-{slot:05d}@synthetic.invalid", NO_LOGIN, f"Listener {slot:05d}"),
            )
            user_id = cur.fetchone()[0]
            listen_rows.extend(
                (user_id, item_to_recording[i], stamp)
                for i, stamp in per_user[user]
                if i in item_to_recording
            )
            written += len(per_user[user])

        # One COPY for ~50,000 listens rather than 1,500 executemany batches.
        seen_listens: set[tuple] = set()
        with cur.copy("COPY listens (user_id, recording_id, listened_at) FROM STDIN") as copy:
            for user_id, recording_id, stamp in listen_rows:
                # COPY has no ON CONFLICT, and (user, recording, listened_at) is
                # unique -- so duplicates are dropped here instead.
                key = (user_id, recording_id, stamp)
                if key in seen_listens:
                    continue
                seen_listens.add(key)
                copy.write_row((user_id, recording_id, datetime.fromtimestamp(stamp, tz=UTC)))

        conn.commit()

        cur.execute("SELECT count(*) FROM users WHERE is_synthetic")
        listeners = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM listens")
        listens = cur.fetchone()[0]
        cur.execute(
            "SELECT count(DISTINCT a.id) FROM artists a JOIN artist_tags t ON t.artist_id = a.id"
        )
        tagged = cur.fetchone()[0]

    print(
        f"\nartists    {len(set(artist_ids.values())):,} ({tagged:,} with genres)\n"
        f"recordings {len(item_to_recording):,}\n"
        f"listeners  {listeners:,}\n"
        f"listens    {listens:,} ({written:,} written)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
