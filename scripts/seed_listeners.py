"""Seed synthetic listeners so collaborative filtering has something to learn.

    python scripts/seed_listeners.py --max-users 1200

The demo catalogue holds ~1,600 recordings and, before this runs, a handful of
accounts. Collaborative filtering over eight histories learns nothing -- every
recommendation collapses to popularity, and the part of this project worth
showing becomes invisible in the product meant to show it.

So real ListenBrainz users (CC0) are mapped onto catalogue recordings by the
same normalised artist+title key the importer uses, and written as listeners
that own history and nothing else: no login, no rooms, no votes.

**Measured coverage (2026-09-06):** 1,457 of the slice's 12,273 items map onto
1,449 of the catalogue's 1,756 recordings, carrying 41,065 of 98,960
interactions. The catalogue is small and popular, the slice is long-tailed, and
the overlap is the popular middle -- which is the part a demo actually plays.

These are not accounts and are never presented as users. They are training data
that happens to live in the users table because `listens.user_id` is a foreign
key, and `is_synthetic` marks them so nothing counts them as people.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import sys
from pathlib import Path

import numpy as np
import psycopg

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "apps" / "api"))

from commonground_api.imports.matching import match_key  # noqa: E402

# Argon2 cannot parse this, so verify_password fails closed on it. A synthetic
# listener cannot be logged into even if someone guesses the email.
NO_LOGIN = "synthetic-listener-no-login"

# Listens are spread across this window so the time-ordered split in the engine
# has real structure rather than one instant.
EPOCH_DAYS = 180


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", default="data/datasets/lb-day")
    parser.add_argument("--max-users", type=int, default=1200)
    parser.add_argument("--min-interactions", type=int, default=10)
    parser.add_argument("--reset", action="store_true", help="delete existing listeners first")
    args = parser.parse_args(argv)

    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2

    dataset_dir = REPO_ROOT / args.dataset
    items = json.loads((dataset_dir / "items.json").read_text())
    arrays = np.load(dataset_dir / "interactions.npz")
    rows, cols, stamps = arrays["rows"], arrays["cols"], arrays["timestamps"]

    with psycopg.connect(url) as conn, conn.cursor() as cur:
        if args.reset:
            cur.execute("DELETE FROM users WHERE is_synthetic")
            print(f"  removed {cur.rowcount:,} existing listeners")

        cur.execute(
            """
            SELECT r.id, a.name, r.title
            FROM recordings r
            JOIN recording_artists ra ON ra.recording_id = r.id
            JOIN artists a ON a.id = ra.artist_id
            """
        )
        catalogue: dict[str, int] = {}
        for recording_id, artist, title in cur.fetchall():
            catalogue.setdefault(match_key(artist, title), recording_id)

        item_to_recording = {}
        for index, item in enumerate(items):
            recording_id = catalogue.get(item["key"])
            if recording_id is not None:
                item_to_recording[index] = recording_id

        keep = np.isin(cols, np.fromiter(item_to_recording, dtype=np.int64))
        rows, cols, stamps = rows[keep], cols[keep], stamps[keep]
        print(
            f"  {len(item_to_recording):,} slice items map onto "
            f"{len(set(item_to_recording.values())):,} catalogue recordings"
        )
        print(f"  {len(rows):,} interactions survive the mapping")

        per_user: dict[int, list[tuple[int, int]]] = collections.defaultdict(list)
        for user, item, stamp in zip(rows, cols, stamps, strict=True):
            per_user[int(user)].append((int(item), int(stamp)))

        eligible = sorted(
            (u for u, v in per_user.items() if len(v) >= args.min_interactions),
            # Most active first, ties on id: a truncated run still seeds the
            # users who carry the most co-occurrence signal.
            key=lambda u: (-len(per_user[u]), u),
        )[: args.max_users]
        print(f"  {len(eligible):,} listeners with >= {args.min_interactions} interactions\n")

        written = listens = 0
        for slot, user in enumerate(eligible):
            cur.execute(
                """
                INSERT INTO users (email, password_hash, display_name, is_demo, is_synthetic)
                VALUES (%s, %s, %s, false, true)
                ON CONFLICT (email) DO UPDATE SET display_name = EXCLUDED.display_name
                RETURNING id
                """,
                (
                    f"listener-{slot:05d}@synthetic.invalid",
                    NO_LOGIN,
                    f"Listener {slot:05d}",
                ),
            )
            user_id = cur.fetchone()[0]
            written += 1

            values = [(user_id, item_to_recording[item], stamp) for item, stamp in per_user[user]]
            cur.executemany(
                "INSERT INTO listens (user_id, recording_id, listened_at) "
                "VALUES (%s, %s, to_timestamp(%s)) ON CONFLICT DO NOTHING",
                values,
            )
            listens += len(values)

            if written % 200 == 0:
                print(f"  {written:,}/{len(eligible):,}")

        conn.commit()

        cur.execute("SELECT count(*) FROM users WHERE is_synthetic")
        total_listeners = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM listens")
        total_listens = cur.fetchone()[0]
        cur.execute("SELECT count(DISTINCT recording_id) FROM listens")
        covered = cur.fetchone()[0]

    print(
        f"\nlisteners        {total_listeners:,}\n"
        f"listens in total {total_listens:,} ({listens:,} written this run)\n"
        f"recordings with at least one listen {covered:,}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
