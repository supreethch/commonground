"""Seed demo accounts with contrasting taste profiles.

    python scripts/seed.py            # create or refresh the demo accounts
    python scripts/seed.py --reset    # delete their data first

Two jobs:

  1. **A recruiter can sign in immediately.** No registration, no waiting for a
     history import to resolve. Demo accounts are marked `is_demo`, which makes
     them read-only through the API -- one shared login that every visitor can
     rewrite is a demo that is broken by lunchtime.

  2. **A group with real disagreement exists on day one.** The whole project is
     about what happens when tastes conflict, so the personas are chosen to
     conflict: a metal listener and an ambient listener in one room is the case
     that makes averaging visibly fail. A seed set of five people who all like
     indie rock would make the recommender look good and prove nothing.

Deterministic: same catalogue in, same profiles and listens out. Listens are
generated from a seeded RNG so M3's evaluation can be re-run against an
identical starting state.
"""

from __future__ import annotations

import argparse
import os
import random
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import psycopg

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "apps", "api"))

from commonground_api.security import hash_password  # noqa: E402

# Changing this changes every generated listen. It is recorded here rather than
# passed in so a seeded database is reproducible from the repository alone.
SEED = 20260906

LISTENS_PER_PERSONA = 120
LISTEN_WINDOW_DAYS = 180


@dataclass
class Persona:
    email: str
    display_name: str
    # Tags are matched case-insensitively against the catalogue's tag names.
    # Several spellings per persona, because tag vocabularies are inconsistent
    # and a persona that matches nothing seeds an empty profile.
    genres: list[str]
    # How much of their listening comes from outside their stated genres. Nobody
    # listens to one thing, and a profile with zero spread makes collaborative
    # filtering trivially easy in a way real data never is.
    wander: float = 0.15
    artist_ids: list[int] = field(default_factory=list)


PERSONAS = [
    Persona("alex@commonground.demo", "Alex", ["indie rock", "alternative rock", "indie pop"]),
    Persona("sam@commonground.demo", "Sam", ["hip hop", "rap", "trap"]),
    Persona("rio@commonground.demo", "Rio", ["electronic", "techno", "house", "idm"]),
    Persona("jules@commonground.demo", "Jules", ["jazz", "soul", "funk"]),
    # Deliberately hard to please alongside the others: the room where
    # "average everyone" produces something nobody asked for.
    Persona("nina@commonground.demo", "Nina", ["metal", "heavy metal", "hardcore"], wander=0.10),
    Persona(
        "theo@commonground.demo", "Theo", ["ambient", "classical", "modern classical"], wander=0.10
    ),
]


def _fetch_artists_by_genre(cur, genres: list[str]) -> list[int]:
    cur.execute(
        """
        SELECT DISTINCT a.id
        FROM artists a
        JOIN artist_tags at ON at.artist_id = a.id
        JOIN tags t        ON t.id = at.tag_id
        WHERE lower(t.name) = ANY(%s)
        ORDER BY a.id
        """,
        ([g.lower() for g in genres],),
    )
    return [row[0] for row in cur.fetchall()]


def _recordings_for_artists(cur, artist_ids: list[int]) -> list[int]:
    if not artist_ids:
        return []
    cur.execute(
        "SELECT DISTINCT recording_id FROM recording_artists WHERE artist_id = ANY(%s) "
        "ORDER BY recording_id",
        (artist_ids,),
    )
    return [row[0] for row in cur.fetchall()]


def seed(conn: psycopg.Connection, password: str, reset: bool) -> dict:
    rng = random.Random(SEED)
    report: dict[str, dict] = {}

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM recordings")
        if cur.fetchone()[0] == 0:
            raise SystemExit(
                "the catalogue is empty -- run scripts/build_catalog.py first, "
                "otherwise there is nothing for a demo profile to point at"
            )

        cur.execute("SELECT DISTINCT recording_id FROM recording_artists ORDER BY recording_id")
        all_recordings = [row[0] for row in cur.fetchall()]

        for persona in PERSONAS:
            cur.execute(
                """
                INSERT INTO users (email, password_hash, display_name, is_demo, onboarded_at)
                VALUES (%s, %s, %s, true, now())
                ON CONFLICT (email) DO UPDATE SET
                    password_hash = EXCLUDED.password_hash,
                    display_name  = EXCLUDED.display_name,
                    is_demo       = true,
                    onboarded_at  = now()
                RETURNING id
                """,
                (persona.email, hash_password(password), persona.display_name),
            )
            user_id = cur.fetchone()[0]

            if reset:
                cur.execute("DELETE FROM listens WHERE user_id = %s", (user_id,))
                cur.execute("DELETE FROM profile_artists WHERE user_id = %s", (user_id,))
                cur.execute("DELETE FROM profile_tags WHERE user_id = %s", (user_id,))

            artist_ids = _fetch_artists_by_genre(cur, persona.genres)
            persona.artist_ids = artist_ids

            # --- taste profile ------------------------------------------------
            for artist_id in artist_ids[:40]:
                cur.execute(
                    "INSERT INTO profile_artists (user_id, artist_id, weight, source) "
                    "VALUES (%s, %s, 1.0, 'onboarding') "
                    "ON CONFLICT (user_id, artist_id) DO NOTHING",
                    (user_id, artist_id),
                )
            cur.execute(
                "SELECT id FROM tags WHERE lower(name) = ANY(%s)",
                ([g.lower() for g in persona.genres],),
            )
            tag_ids = [row[0] for row in cur.fetchall()]
            for tag_id in tag_ids:
                cur.execute(
                    "INSERT INTO profile_tags (user_id, tag_id, weight, source) "
                    "VALUES (%s, %s, 1.0, 'onboarding') "
                    "ON CONFLICT (user_id, tag_id) DO NOTHING",
                    (user_id, tag_id),
                )

            # --- synthetic listening history ---------------------------------
            in_genre = _recordings_for_artists(cur, artist_ids)
            listened = 0
            if in_genre:
                now = datetime.now(UTC)
                for index in range(LISTENS_PER_PERSONA):
                    pool = (
                        all_recordings
                        if (all_recordings and rng.random() < persona.wander)
                        else in_genre
                    )
                    recording_id = rng.choice(pool)
                    # Spread over months rather than all at one instant: the
                    # evaluation split in M3 is time-ordered, and a history with
                    # no time structure would make it meaningless.
                    listened_at = now - timedelta(
                        days=rng.uniform(0, LISTEN_WINDOW_DAYS),
                        minutes=index,
                    )
                    cur.execute(
                        "INSERT INTO listens (user_id, recording_id, listened_at) "
                        "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                        (user_id, recording_id, listened_at),
                    )
                    listened += cur.rowcount

            report[persona.display_name] = {
                "artists": len(artist_ids),
                "in_genre_recordings": len(in_genre),
                "listens_inserted": listened,
                "genres_matched": len(tag_ids),
            }

    conn.commit()
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reset", action="store_true", help="clear demo data before seeding")
    args = parser.parse_args(argv)

    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2

    password = os.environ.get("DEMO_USER_PASSWORD", "demo-read-only")

    with psycopg.connect(url) as conn:
        report = seed(conn, password, args.reset)

    print(f"seeded {len(report)} demo accounts (seed={SEED})\n")
    print(f"  {'persona':10} {'artists':>8} {'tracks':>8} {'listens':>8} {'genres':>7}")
    for name, row in report.items():
        print(
            f"  {name:10} {row['artists']:>8} {row['in_genre_recordings']:>8} "
            f"{row['listens_inserted']:>8} {row['genres_matched']:>7}"
        )

    empty = [name for name, row in report.items() if row["artists"] == 0]
    if empty:
        # Loud, because a silently empty persona would look like a working seed
        # and then produce meaningless recommendations in M4.
        print(
            f"\nWARNING: no catalogue artists matched for: {', '.join(empty)}.\n"
            "The catalogue is built from ListenBrainz's most-played recordings, so "
            "narrower genres may be absent. Widen the persona's genre list or build "
            "a larger catalogue.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
