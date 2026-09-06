"""Build a user-item interaction dataset from a ListenBrainz listen dump.

    python scripts/build_dataset.py --download          # fetch the dump if absent
    python scripts/build_dataset.py --max-users 20000

The catalogue built by build_catalog.py comes from ListenBrainz's *sitewide top
1000 per time range*, which is fine for onboarding and useless for collaborative
filtering: it is ~1,600 of the most popular recordings on the service, so every
user looks identical. Real collaborative filtering needs the listens themselves.

Output goes to data/datasets/<name>/ as compressed NumPy arrays plus a JSON
sidecar naming the items. Nothing is committed -- see docs/data-sources.md.

## Two measured facts that shaped this

**The dump has almost no MBIDs.** Measured over 30,000 listens on 2026-09-06:
100% carry artist and track names, 1.9% carry `additional_info.recording_mbid`,
and `mbid_mapping` -- ListenBrainz's own resolved mapping -- is absent from the
dump entirely. So item identity here is the *normalised artist + track name*,
using the same `match_key` the importer uses. Waiting for MBIDs would discard
98% of the data; names are what the dump actually has.

**The dump is ordered by user, not shuffled.** One user accounted for 27,051 of
those 30,000 listens. Reading the first N lines is therefore not a sample of
users, it is a sample of one or two users, and per-user caps are not tidiness --
without them a single bulk importer defines the whole item vocabulary.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tarfile
import time
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import zstandard

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "apps", "api"))

from commonground_api.imports.matching import match_key  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DUMP_DIR = REPO_ROOT / "data" / "dumps"
DATASET_DIR = REPO_ROOT / "data" / "datasets"

DUMP_BASE = "https://data.metabrainz.org/pub/musicbrainz/listenbrainz/incremental"
DEFAULT_DUMP = "listenbrainz-dump-2653-20260906-000002-incremental"

USER_AGENT = os.environ.get("USER_AGENT", "CommonGround/0.1 (set USER_AGENT)")


def dump_url(name: str) -> str:
    stem = name.removeprefix("listenbrainz-dump-")
    return f"{DUMP_BASE}/{name}/listenbrainz-listens-dump-{stem}.tar.zst"


def download(name: str, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        print(f"  using cached {destination.name} ({destination.stat().st_size / 1e6:.0f} MB)")
        return destination
    url = dump_url(name)
    print(f"  downloading {url}")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=120) as response, destination.open("wb") as out:
        while chunk := response.read(1 << 20):
            out.write(chunk)
    return destination


def iter_listens(archive: Path):
    """Yield (user_id, item_key, timestamp) from the dump, streaming.

    Streamed rather than extracted: the archive is 370 MiB compressed and 4.3 GB
    of JSON inside, and nothing here needs it on disk twice.
    """
    with archive.open("rb") as raw:
        stream = zstandard.ZstdDecompressor().stream_reader(raw)
        with tarfile.open(fileobj=stream, mode="r|") as tar:
            for member in tar:
                if not member.name.endswith(".listens"):
                    continue
                handle = tar.extractfile(member)
                if handle is None:
                    continue
                for line in handle:
                    try:
                        row = json.loads(line)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    metadata = row.get("track_metadata") or {}
                    artist = metadata.get("artist_name")
                    track = metadata.get("track_name")
                    user = row.get("user_id")
                    stamp = row.get("timestamp")
                    if not artist or not track or user is None or not isinstance(stamp, int):
                        continue
                    yield user, match_key(artist, track), stamp, artist, track


def build(
    archive: Path,
    max_users: int,
    per_user_cap: int,
    min_user_listens: int,
    min_item_listens: int,
    max_lines: int | None,
) -> dict:
    """Collect interactions, applying the caps that keep the sample honest."""
    per_user: dict[int, dict[str, int]] = defaultdict(dict)
    per_user_total: Counter[int] = Counter()
    display: dict[str, tuple[str, str]] = {}
    lines = 0
    started = time.time()

    for user, key, stamp, artist, track in iter_listens(archive):
        lines += 1
        if max_lines and lines > max_lines:
            break

        # One bulk importer would otherwise define the whole item vocabulary.
        if per_user_total[user] >= per_user_cap:
            continue
        per_user_total[user] += 1

        # Keep the earliest timestamp per (user, item): repeated plays become
        # one interaction with implicit weight handled later, and the earliest
        # is the one a time-ordered split should see.
        existing = per_user[user].get(key)
        if existing is None or stamp < existing:
            per_user[user][key] = stamp
        display.setdefault(key, (artist, track))

        if len(per_user) > max_users * 4 and lines % 1_000_000 == 0:
            break

        if lines % 2_000_000 == 0:
            print(
                f"  {lines / 1e6:.0f}M lines, {len(per_user):,} users, "
                f"{len(display):,} items, {time.time() - started:.0f}s"
            )

    print(f"  read {lines:,} lines in {time.time() - started:.0f}s")

    # --- filter ---------------------------------------------------------
    users = {user: items for user, items in per_user.items() if len(items) >= min_user_listens}
    print(f"  {len(users):,} users with >= {min_user_listens} distinct items")

    item_counts: Counter[str] = Counter()
    for items in users.values():
        # .keys(), not the dict: Counter.update(dict) adds the dict's *values*
        # as counts, and the values here are timestamps -- which scored every
        # item at ~1.8 billion and let the whole 420k-item tail through a
        # ">= 5 listeners" filter that appeared to be working.
        item_counts.update(items.keys())
    keep_items = {key for key, count in item_counts.items() if count >= min_item_listens}
    print(f"  {len(keep_items):,} items played by >= {min_item_listens} of them")

    users = {
        user: {k: v for k, v in items.items() if k in keep_items} for user, items in users.items()
    }
    # Filtering items can push a user back under the threshold.
    users = {u: items for u, items in users.items() if len(items) >= min_user_listens}

    if len(users) > max_users:
        # Deterministic: the most active users, ties broken by id.
        chosen = sorted(users, key=lambda u: (-len(users[u]), u))[:max_users]
        users = {u: users[u] for u in chosen}
        item_counts = Counter()
        for items in users.values():
            item_counts.update(items.keys())
        keep_items = set(item_counts)

    # --- index ----------------------------------------------------------
    user_ids = sorted(users)
    item_keys = sorted(keep_items)
    user_index = {user: i for i, user in enumerate(user_ids)}
    item_index = {key: i for i, key in enumerate(item_keys)}

    rows, cols, stamps = [], [], []
    for user, items in users.items():
        for key, stamp in items.items():
            rows.append(user_index[user])
            cols.append(item_index[key])
            stamps.append(stamp)

    return {
        "user_ids": np.array(user_ids, dtype=np.int64),
        "item_keys": item_keys,
        "item_names": [display.get(k, ("", "")) for k in item_keys],
        "rows": np.array(rows, dtype=np.int32),
        "cols": np.array(cols, dtype=np.int32),
        "timestamps": np.array(stamps, dtype=np.int64),
    }


def write(dataset: dict, name: str, meta: dict) -> Path:
    out = DATASET_DIR / name
    out.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        out / "interactions.npz",
        user_ids=dataset["user_ids"],
        rows=dataset["rows"],
        cols=dataset["cols"],
        timestamps=dataset["timestamps"],
    )
    (out / "items.json").write_text(
        json.dumps(
            [
                {"key": key, "artist": artist, "track": track}
                for key, (artist, track) in zip(
                    dataset["item_keys"], dataset["item_names"], strict=True
                )
            ]
        )
    )
    (out / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dump", default=DEFAULT_DUMP)
    parser.add_argument("--download", action="store_true", help="fetch the dump if not cached")
    parser.add_argument("--name", default="lb-day", help="dataset directory name")
    parser.add_argument("--max-users", type=int, default=30_000)
    parser.add_argument("--per-user-cap", type=int, default=1_000)
    parser.add_argument("--min-user-listens", type=int, default=20)
    parser.add_argument("--min-item-listens", type=int, default=5)
    parser.add_argument("--max-lines", type=int, default=None)
    args = parser.parse_args(argv)

    archive = DUMP_DIR / f"{args.dump}.tar.zst"
    if not archive.exists():
        alternative = DUMP_DIR / "lb-2653-incremental.tar.zst"
        if alternative.exists():
            archive = alternative
        elif args.download:
            archive = download(args.dump, archive)
        else:
            print(f"{archive} not found; pass --download", file=sys.stderr)
            return 2

    print(f"building from {archive.name}")
    dataset = build(
        archive,
        max_users=args.max_users,
        per_user_cap=args.per_user_cap,
        min_user_listens=args.min_user_listens,
        min_item_listens=args.min_item_listens,
        max_lines=args.max_lines,
    )

    users = len(dataset["user_ids"])
    items = len(dataset["item_keys"])
    interactions = len(dataset["rows"])
    meta = {
        "source_dump": archive.name,
        "licence": "CC0-1.0",
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "item_identity": (
            "normalised artist + track name (the dump carries recording MBIDs for ~2% of listens)"
        ),
        "users": users,
        "items": items,
        "interactions": interactions,
        "density": round(interactions / max(users * items, 1), 8),
        "params": {
            "max_users": args.max_users,
            "per_user_cap": args.per_user_cap,
            "min_user_listens": args.min_user_listens,
            "min_item_listens": args.min_item_listens,
            "max_lines": args.max_lines,
        },
    }

    out = write(dataset, args.name, meta)
    print(
        f"\nusers        {users:,}\n"
        f"items        {items:,}\n"
        f"interactions {interactions:,}\n"
        f"density      {meta['density']:.6%}\n"
        f"written to   {out.relative_to(REPO_ROOT)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
