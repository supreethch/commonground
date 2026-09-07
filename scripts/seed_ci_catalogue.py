"""Generate a small synthetic catalogue, for CI and for a first run.

    python scripts/seed_ci_catalogue.py

CI needs a catalogue to drive the app against, and there are two ways not to get
one:

**Fetching the real thing.** A 2 GB MusicBrainz dump in a pull request is absurd,
and it would make every build depend on MetaBrainz being up -- an upstream
outage would turn every PR red for reasons no author could fix.

**Committing a slice of it.** MusicBrainz genre tags are CC BY-NC-SA, and this
repository has kept every derivative of them out of git from M1 onward
(docs/data-sources.md). Adding a tagged fixture to an MIT tree to make CI
convenient would quietly undo that.

So the catalogue is *invented*. Deterministic, seeded, no upstream, no licence
question, and structured enough that collaborative filtering has something to
learn: listeners are drawn from taste clusters, so items genuinely co-occur.

It is not a substitute for the real catalogue. It exists so an end-to-end test
can click through a working app, and so someone cloning the repo can see it run
before spending forty minutes on the real build.
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import uuid
from datetime import UTC, datetime, timedelta

import psycopg

SEED = 20260906

# Invented genres. Not MusicBrainz's vocabulary -- these exist only so the
# explanation layer has something to say and the personas have something to
# match against.
CLUSTERS = [
    ("indie rock", ["alternative rock", "indie pop", "rock"]),
    ("hip hop", ["rap", "trap"]),
    ("electronic", ["techno", "house", "idm"]),
    ("jazz", ["soul", "funk"]),
    ("metal", ["heavy metal", "hardcore"]),
    ("ambient", ["classical", "modern classical"]),
]

ARTISTS_PER_CLUSTER = 14
TRACKS_PER_ARTIST = 4
LISTENERS_PER_CLUSTER = 40
# How much of a listener's history comes from outside their own cluster. Zero
# would make the clusters trivially separable and collaborative filtering a
# lookup table.
WANDER = 0.18
LISTENS_PER_LISTENER = 55


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reset", action="store_true", help="clear any existing catalogue")
    args = parser.parse_args(argv)

    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2

    rng = random.Random(SEED)

    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM recordings")
        if cur.fetchone()[0] > 0 and not args.reset:
            print("catalogue already present; pass --reset to replace it")
            return 0
        if args.reset:
            cur.execute("DELETE FROM users WHERE is_synthetic")
            cur.execute("DELETE FROM recordings")
            cur.execute("DELETE FROM artists")
            cur.execute("DELETE FROM tags")

        tag_ids: dict[str, int] = {}
        for primary, extras in CLUSTERS:
            for name in [primary, *extras]:
                if name in tag_ids:
                    continue
                cur.execute(
                    "INSERT INTO tags (name, is_genre) VALUES (%s, true) "
                    "ON CONFLICT (name) DO UPDATE SET is_genre = true RETURNING id",
                    (name,),
                )
                tag_ids[name] = cur.fetchone()[0]

        cluster_recordings: list[list[int]] = []
        for index, (primary, extras) in enumerate(CLUSTERS):
            recordings: list[int] = []
            for artist_index in range(ARTISTS_PER_CLUSTER):
                name = f"{primary.title()} Artist {artist_index + 1}"
                cur.execute(
                    "INSERT INTO artists (mbid, name, listen_count) VALUES (%s, %s, %s) "
                    "ON CONFLICT (mbid) DO NOTHING RETURNING id",
                    (uuid.uuid4(), name, rng.randint(500, 50_000)),
                )
                artist_id = cur.fetchone()[0]

                # The primary genre always, plus one extra sometimes, so items
                # within a cluster are similar but not identical.
                genres = [primary] + ([rng.choice(extras)] if rng.random() < 0.7 else [])
                for genre in genres:
                    cur.execute(
                        "INSERT INTO artist_tags (artist_id, tag_id, weight) VALUES (%s, %s, %s) "
                        "ON CONFLICT DO NOTHING",
                        (artist_id, tag_ids[genre], float(rng.randint(1, 40))),
                    )

                for track_index in range(TRACKS_PER_ARTIST):
                    cur.execute(
                        "INSERT INTO recordings (mbid, title, listen_count) "
                        "VALUES (%s, %s, %s) RETURNING id",
                        (
                            uuid.uuid4(),
                            f"{primary.title()} Track {artist_index + 1}-{track_index + 1}",
                            rng.randint(100, 20_000),
                        ),
                    )
                    recording_id = cur.fetchone()[0]
                    cur.execute(
                        "INSERT INTO recording_artists (recording_id, artist_id, position) "
                        "VALUES (%s, %s, 0)",
                        (recording_id, artist_id),
                    )
                    cur.execute(
                        "INSERT INTO recording_links (recording_id, url, provider, source) "
                        "VALUES (%s, %s, 'youtube', 'search_url') ON CONFLICT DO NOTHING",
                        (
                            recording_id,
                            "https://www.youtube.com/results?search_query="
                            f"{primary.replace(' ', '+')}+{artist_index}+{track_index}",
                        ),
                    )
                    recordings.append(recording_id)
            cluster_recordings.append(recordings)
            del index

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

        everything = [r for cluster in cluster_recordings for r in cluster]
        now = datetime.now(UTC)
        listeners = 0
        for cluster_index, recordings in enumerate(cluster_recordings):
            for offset in range(LISTENERS_PER_CLUSTER):
                cur.execute(
                    "INSERT INTO users (email, password_hash, display_name, is_synthetic) "
                    "VALUES (%s, 'synthetic-listener-no-login', %s, true) "
                    "ON CONFLICT (email) DO UPDATE SET display_name = EXCLUDED.display_name "
                    "RETURNING id",
                    (
                        f"ci-listener-{cluster_index}-{offset}@synthetic.invalid",
                        f"CI Listener {cluster_index}-{offset}",
                    ),
                )
                user_id = cur.fetchone()[0]
                listeners += 1

                rows = []
                for index in range(LISTENS_PER_LISTENER):
                    pool = everything if rng.random() < WANDER else recordings
                    rows.append(
                        (
                            user_id,
                            rng.choice(pool),
                            now - timedelta(days=rng.uniform(0, 180), minutes=index),
                        )
                    )
                cur.executemany(
                    "INSERT INTO listens (user_id, recording_id, listened_at) "
                    "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                    rows,
                )

        conn.commit()

        cur.execute("SELECT count(*) FROM artists")
        artists = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM recordings")
        recordings_total = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM listens")
        listens = cur.fetchone()[0]

    print(
        f"synthetic catalogue (seed={SEED})\n"
        f"  artists    {artists:,}\n"
        f"  recordings {recordings_total:,}\n"
        f"  listeners  {listeners:,}\n"
        f"  listens    {listens:,}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
