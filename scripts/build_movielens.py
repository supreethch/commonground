"""Build a MovieLens-1M dataset in the same shape as the ListenBrainz slice.

    python scripts/build_movielens.py

Why a second dataset at all: the ListenBrainz numbers are only meaningful
*relative* to the baselines beside them, because that slice is built by this
repository from one daily dump and exists nowhere else. MovieLens-1M is the most
reported benchmark in the field, so a table on it can be compared -- carefully --
to published work.

It is also a useful contrast in a way that has nothing to do with comparability.
Every MovieLens film carries genres, where only ~91% of the music items do and
those arrive through fuzzy name matching. If the content model earns weight on
MovieLens and not on ListenBrainz, that separates "content features do not help"
from "our content features are thin".

## Implicit feedback from explicit ratings

MovieLens ratings are 1-5; this engine is implicit-feedback throughout. Ratings
>= 4 become positive interactions and everything else is dropped, which is the
standard binarisation in the implicit-feedback literature. It is a real choice
with a real cost: a 3-star rating is evidence of *something*, and discarding it
throws away roughly a third of the file. Recorded in meta.json so a comparison
against a paper using a different threshold is not made by accident.

## The download, and the expired certificate

**files.grouplens.org's TLS certificate expired on 28 August 2026.** The host
and issuer are legitimate (University of Minnesota, InCommon/Internet2); the
certificate simply lapsed, and plain HTTP redirects straight back to the broken
HTTPS.

So the archive is fetched without certificate verification and then **verified
against a SHA-256 digest published by TensorFlow Datasets over a properly
validated channel**. That is what a checksum is for: authenticity is established
by the digest, not by the transport. The script **fails closed** -- a mismatch
raises and writes nothing, and the digest is pinned in this file rather than
fetched from the same broken host.

If GroupLens renews the certificate, delete `INSECURE_HOSTS` and nothing else
changes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import ssl
import time
import urllib.request
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
DUMP_DIR = REPO_ROOT / "data" / "dumps"
DATASET_DIR = REPO_ROOT / "data" / "datasets"

URL = "https://files.grouplens.org/datasets/movielens/ml-1m.zip"

# Published by TensorFlow Datasets at
# tensorflow_datasets/url_checksums/movielens.txt, fetched over a valid TLS
# connection. Pinned here so the integrity check never depends on the host whose
# certificate is broken.
EXPECTED_SHA256 = "a6898adb50b9ca05aa231689da44c217cb524e7ebd39d264c56e2832f2c54e20"
EXPECTED_BYTES = 5_917_549

# Hosts whose certificate verification is skipped, and only these. Remove the
# entry once the certificate is renewed.
INSECURE_HOSTS = {"files.grouplens.org"}

USER_AGENT = "CommonGround/0.1 (portfolio project; skchittaluri06@gmail.com)"

POSITIVE_RATING = 4


def download(destination: Path) -> Path:
    if destination.exists():
        print(f"  using cached {destination.name} ({destination.stat().st_size:,} bytes)")
        return destination

    destination.parent.mkdir(parents=True, exist_ok=True)
    context = ssl.create_default_context()
    if "files.grouplens.org" in INSECURE_HOSTS:
        print(
            "  NOTE: certificate verification disabled for files.grouplens.org\n"
            "        (their certificate expired 2026-08-28). Integrity is checked\n"
            "        against a SHA-256 digest published independently by TFDS."
        )
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

    request = urllib.request.Request(URL, headers={"User-Agent": USER_AGENT})
    print(f"  downloading {URL}")
    with urllib.request.urlopen(request, timeout=120, context=context) as response:
        payload = response.read()

    digest = hashlib.sha256(payload).hexdigest()
    if digest != EXPECTED_SHA256:
        raise SystemExit(
            "SHA-256 mismatch -- refusing to use this file.\n"
            f"  expected {EXPECTED_SHA256}\n"
            f"  got      {digest}\n"
            "The download was not certificate-verified, so a mismatch means the "
            "bytes cannot be trusted. Nothing has been written."
        )
    if len(payload) != EXPECTED_BYTES:
        raise SystemExit(f"unexpected size {len(payload):,}, expected {EXPECTED_BYTES:,}")

    destination.write_bytes(payload)
    print(f"  verified sha256 {digest[:16]}... ({len(payload):,} bytes)")
    return destination


def parse(archive: Path, min_user: int, min_item: int) -> dict:
    """Read ratings.dat and movies.dat out of the zip."""
    with zipfile.ZipFile(archive) as bundle:
        ratings_name = next(n for n in bundle.namelist() if n.endswith("ratings.dat"))
        movies_name = next(n for n in bundle.namelist() if n.endswith("movies.dat"))
        # latin-1: the file is not UTF-8 and several titles carry accented
        # characters. utf-8 raises on them, and errors="replace" would mangle
        # exactly the titles a reader would notice.
        ratings_raw = bundle.read(ratings_name).decode("latin-1")
        movies_raw = bundle.read(movies_name).decode("latin-1")

    movies: dict[int, dict] = {}
    for line in movies_raw.splitlines():
        if not line.strip():
            continue
        movie_id, title, genres = line.split("::")
        movies[int(movie_id)] = {
            "title": title,
            "genres": [g for g in genres.split("|") if g and g != "(no genres listed)"],
        }

    per_user: dict[int, dict[int, int]] = defaultdict(dict)
    total = kept = 0
    for line in ratings_raw.splitlines():
        if not line.strip():
            continue
        user_id, movie_id, rating, stamp = line.split("::")
        total += 1
        if int(rating) < POSITIVE_RATING:
            continue
        kept += 1
        per_user[int(user_id)][int(movie_id)] = int(stamp)

    print(f"  {total:,} ratings, {kept:,} at >= {POSITIVE_RATING} stars ({kept / total:.1%})")

    users = {u: items for u, items in per_user.items() if len(items) >= min_user}
    item_counts: Counter[int] = Counter()
    for items in users.values():
        item_counts.update(items.keys())
    keep = {i for i, c in item_counts.items() if c >= min_item}
    users = {u: {i: t for i, t in items.items() if i in keep} for u, items in users.items()}
    users = {u: items for u, items in users.items() if len(items) >= min_user}

    remaining: Counter[int] = Counter()
    for items in users.values():
        remaining.update(items.keys())

    user_ids = sorted(users)
    item_ids = sorted(remaining)
    user_index = {u: i for i, u in enumerate(user_ids)}
    item_index = {m: i for i, m in enumerate(item_ids)}

    rows, cols, stamps = [], [], []
    for user, items in users.items():
        for movie, stamp in items.items():
            rows.append(user_index[user])
            cols.append(item_index[movie])
            stamps.append(stamp)

    return {
        "user_ids": np.array(user_ids, dtype=np.int64),
        "rows": np.array(rows, dtype=np.int32),
        "cols": np.array(cols, dtype=np.int32),
        "timestamps": np.array(stamps, dtype=np.int64),
        "items": [
            {
                "key": str(movie_id),
                # The repetition axis. For music that is the artist; a film has
                # no artist, and the analogue a listener would notice is the
                # genre -- "four action films in a row" is the same complaint as
                # "four tracks by the same band". meta.json records which axis
                # a dataset uses so the metric is never compared across them
                # without noticing.
                "artist": (movies.get(movie_id, {}).get("genres") or ["Unknown"])[0],
                "track": movies.get(movie_id, {}).get("title", f"movie {movie_id}"),
                "genres": movies.get(movie_id, {}).get("genres", []),
            }
            for movie_id in item_ids
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", default="ml-1m")
    parser.add_argument("--min-user-listens", type=int, default=20)
    parser.add_argument("--min-item-listens", type=int, default=5)
    args = parser.parse_args(argv)

    archive = download(DUMP_DIR / "ml-1m.zip")
    print("\nparsing")
    built = parse(archive, args.min_user_listens, args.min_item_listens)

    users = len(built["user_ids"])
    items = len(built["items"])
    interactions = len(built["rows"])
    with_genres = sum(1 for item in built["items"] if item["genres"])

    out = DATASET_DIR / args.name
    out.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        out / "interactions.npz",
        user_ids=built["user_ids"],
        rows=built["rows"],
        cols=built["cols"],
        timestamps=built["timestamps"],
    )
    (out / "items.json").write_text(json.dumps(built["items"]))
    (out / "meta.json").write_text(
        json.dumps(
            {
                "source": URL,
                "sha256": EXPECTED_SHA256,
                "licence": (
                    "MovieLens data is provided by GroupLens for research use; "
                    "see the ml-1m README for the usage conditions and the "
                    "requested citation. Not redistributed by this repository."
                ),
                "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "item_identity": "MovieLens movie id",
                "repetition_axis": "genre (a film has no artist)",
                "binarisation": f"ratings >= {POSITIVE_RATING} are positive; the rest dropped",
                "users": users,
                "items": items,
                "interactions": interactions,
                "density": round(interactions / max(users * items, 1), 8),
                "items_with_genres": with_genres,
                "params": {
                    "min_user_listens": args.min_user_listens,
                    "min_item_listens": args.min_item_listens,
                },
            },
            indent=2,
        )
        + "\n"
    )

    print(
        f"\nusers        {users:,}\n"
        f"items        {items:,}\n"
        f"interactions {interactions:,}\n"
        f"density      {interactions / max(users * items, 1):.4%}\n"
        f"with genres  {with_genres:,} ({with_genres / max(items, 1):.1%})\n"
        f"written to   {out.relative_to(REPO_ROOT)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
