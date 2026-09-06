"""Attach MusicBrainz genres to a dataset's artists, from the bulk JSON dump.

    python scripts/enrich_tags.py --dataset data/datasets/lb-day

Writes `artist_tags.json` beside the dataset. Nothing is committed: the tags are
MusicBrainz supplementary data under CC BY-NC-SA 3.0, and a derived file in an
MIT tree is exactly the licence conflict docs/data-sources.md exists to avoid.

## Why the dump and not the web service

The dataset carries artist *names*, not MBIDs -- the listen dump has a recording
MBID for 1.9% of listens -- so the web-service path costs a search plus a lookup
per artist. At MusicBrainz's ~1 request/second, and the ~9s/artist this project
measured once throttling starts, tagging the ~1,000 artists that cover 90% of
interactions would take hours and resolve names fuzzily.

The `artist` JSON dump carries `genres` and `tags` inline for every artist. One
2 GB download, streamed and never written to disk, decompresses to ~17.5 GB and
scans at ~119 MB/s -- a complete pass in roughly two and a half minutes, with no
rate limit and no name-to-MBID search at all.

Most lines are rejected by a regex on the `name` field before any JSON parsing
happens, which is what makes that rate achievable: full `json.loads` on 2.3M
artist records would dominate everything else.

## Name collisions

Normalised names are not unique in MusicBrainz -- several distinct artists are
called "Nirvana". **The candidate with the most total tag votes wins**, which is
a decent proxy for the one a listener meant. The ambiguity rate is recorded in
the output rather than hidden, because it is a real source of wrong tags.

## Why aliases are matched too, and why the catalogue is merged in

A first version matched only the primary `name` field and covered 27.8% of
interactions -- *worse* than the 56.6% the database catalogue already had from
193 artists resolved by MBID. The catalogue wins on interaction coverage
because MBID resolution is exact while name matching silently misses the
popular artists whose scrobbled name differs from their MusicBrainz primary
name ("Beatles" vs "The Beatles", romanised vs native script).

So two changes: aliases and sort-names are matched as well as the primary name,
and `--merge-catalogue` folds in the MBID-resolved tags the database already
holds. Name matching is the limiting factor here, not tag sparsity, and the
honest fix is to use the exact identifiers where they exist rather than to
pretend fuzzy matching covered it.
"""

from __future__ import annotations

import argparse
import json
import lzma
import re
import sys
import tarfile
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "apps" / "api"))

from commonground_api.imports.matching import normalise  # noqa: E402

DUMP_BASE = "https://data.metabrainz.org/pub/musicbrainz/data/json-dumps"
USER_AGENT = "CommonGround/0.1 (portfolio project; skchittaluri06@gmail.com)"

# Matches every "name":"..." in a line without parsing it. Aliases and areas
# carry name fields too, which is deliberate: scanning all of them is how an
# artist is found under a name other than their MusicBrainz primary one.
NAME_FIELD = re.compile(rb'"name":"((?:[^"\\]|\\.)*)"')


def latest_dump_directory() -> str:
    request = urllib.request.Request(f"{DUMP_BASE}/", headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        listing = response.read().decode("utf-8", errors="replace")
    directories = sorted(set(re.findall(r'href="(\d{8}-\d{6})/"', listing)))
    if not directories:
        raise RuntimeError("no dated directories found in the JSON dump index")
    return directories[-1]


def stream_artists(url: str):
    """Yield raw JSON lines from the artist dump, streaming and undecompressed."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    response = urllib.request.urlopen(request, timeout=120)
    try:
        with tarfile.open(fileobj=lzma.LZMAFile(response), mode="r|") as tar:
            for member in tar:
                if not member.name.endswith("/artist"):
                    continue
                handle = tar.extractfile(member)
                if handle is None:
                    continue
                yield from handle
                return
    finally:
        response.close()


def collect(targets: set[str], url: str, progress_every: int = 500_000) -> dict:
    """Scan the dump, keeping the best-tagged candidate for each target name."""
    best: dict[str, dict] = {}
    collisions: dict[str, int] = {}
    scanned = 0
    started = time.time()

    for line in stream_artists(url):
        scanned += 1
        if scanned % progress_every == 0:
            print(
                f"  {scanned / 1e6:.1f}M artists, {len(best):,}/{len(targets):,} matched, "
                f"{time.time() - started:.0f}s",
                flush=True,
            )

        # Aliases carry "name" fields of their own, so scanning every name-like
        # field in the line is both the cheap gate and the alias match. Only
        # lines with at least one hit are parsed as JSON, which is what keeps
        # the scan at ~119 MB/s over 2.3M records.
        keys = set()
        for candidate in NAME_FIELD.finditer(line):
            try:
                value = json.loads(b'"' + candidate.group(1) + b'"')
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            key = normalise(value)
            if key in targets:
                keys.add(key)
        if not keys:
            continue

        try:
            record = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue

        sort_name = record.get("sort-name")
        if sort_name:
            sort_key = normalise(sort_name)
            if sort_key in targets:
                keys.add(sort_key)

        genres = [
            {"name": g["name"], "count": int(g.get("count") or 0)}
            for g in (record.get("genres") or [])
            if g.get("name")
        ]
        tags = [
            {"name": t["name"], "count": int(t.get("count") or 0)}
            for t in (record.get("tags") or [])
            if t.get("name")
        ]
        # Total tag votes as the tie-breaker between artists sharing a name. It
        # is a proxy for "the one people actually mean", not a guarantee.
        votes = sum(g["count"] for g in genres) + sum(t["count"] for t in tags)

        for key in keys:
            collisions[key] = collisions.get(key, 0) + 1
            previous = best.get(key)
            if previous is None or votes > previous["votes"]:
                best[key] = {
                    "mbid": record.get("id"),
                    "name": record.get("name"),
                    "votes": votes,
                    "genres": genres,
                    "tags": tags,
                }

    elapsed = time.time() - started
    print(f"  scanned {scanned:,} artists in {elapsed:.0f}s", flush=True)
    return {"best": best, "collisions": collisions, "scanned": scanned, "seconds": elapsed}


def catalogue_tags() -> dict[str, dict]:
    """Genre tags the database already holds, keyed by normalised artist name.

    These came from MBID lookups during the catalogue build, so they are exact
    where the dump's name matching is fuzzy -- and they cover the popular
    artists, which is why they carry far more interaction weight per artist.
    Absence of a database is a fallback, not a failure.
    """
    import os

    url = os.environ.get("DATABASE_URL")
    if not url:
        return {}
    try:
        import psycopg
    except ImportError:
        return {}

    found: dict[str, dict] = {}
    try:
        with psycopg.connect(url, connect_timeout=5) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT a.name, a.mbid, t.name, t.is_genre
                FROM artists a
                JOIN artist_tags at ON at.artist_id = a.id
                JOIN tags t ON t.id = at.tag_id
                """
            )
            for artist_name, mbid, tag_name, is_genre in cur.fetchall():
                entry = found.setdefault(
                    normalise(artist_name),
                    {"mbid": str(mbid), "name": artist_name, "genres": [], "tags": []},
                )
                (entry["genres"] if is_genre else entry["tags"]).append(tag_name)
    except Exception as exc:  # noqa: BLE001 - a missing database is a fallback
        print(f"  (catalogue merge skipped: {type(exc).__name__})")
        return {}
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", default="data/datasets/lb-day")
    parser.add_argument("--dump", default=None, help="dated dump directory; default is latest")
    parser.add_argument(
        "--merge-catalogue",
        action="store_true",
        help="fold in MBID-resolved tags from DATABASE_URL (recommended)",
    )
    parser.add_argument(
        "--min-count",
        type=int,
        default=1,
        help="drop tags with fewer than this many votes",
    )
    args = parser.parse_args(argv)

    dataset_dir = REPO_ROOT / args.dataset
    items_path = dataset_dir / "items.json"
    if not items_path.exists():
        print(f"{items_path} not found -- run scripts/build_dataset.py first", file=sys.stderr)
        return 2

    items = json.loads(items_path.read_text())
    targets = {normalise(item["artist"]) for item in items}
    print(f"dataset  {len(items):,} items, {len(targets):,} distinct artists")

    dump = args.dump or latest_dump_directory()
    url = f"{DUMP_BASE}/{dump}/artist.tar.xz"
    print(f"dump     {url}\n")

    collected = collect(targets, url)
    best = collected["best"]
    collisions = collected["collisions"]

    tags_by_artist = {}
    for key, record in best.items():
        genres = [g["name"] for g in record["genres"] if g["count"] >= args.min_count]
        # Free tags are kept separately: they are noisier than the curated genre
        # list, so the explanation layer can prefer genres and fall back.
        free = [t["name"] for t in record["tags"] if t["count"] >= args.min_count]
        if not genres and not free:
            continue
        tags_by_artist[key] = {
            "mbid": record["mbid"],
            "name": record["name"],
            "genres": genres,
            "tags": free,
            "candidates": collisions.get(key, 1),
        }

    from_dump = len(tags_by_artist)
    merged = 0
    if args.merge_catalogue:
        for key, entry in catalogue_tags().items():
            if key not in targets:
                continue
            existing = tags_by_artist.get(key)
            if existing is None:
                tags_by_artist[key] = {
                    "mbid": entry["mbid"],
                    "name": entry["name"],
                    "genres": sorted(set(entry["genres"])),
                    "tags": sorted(set(entry["tags"])),
                    "candidates": 1,
                    "source": "catalogue",
                }
                merged += 1
            else:
                # Union: the two sources are the same upstream data reached by
                # different routes, so neither is authoritative over the other.
                existing["genres"] = sorted(set(existing["genres"]) | set(entry["genres"]))
                existing["tags"] = sorted(set(existing["tags"]) | set(entry["tags"]))
                existing["source"] = "both"
        print(f"  merged {merged:,} artists the dump's name matching missed")

    for entry in tags_by_artist.values():
        entry.setdefault("source", "dump")

    ambiguous = sum(1 for key in tags_by_artist if collisions.get(key, 1) > 1)
    covered_items = sum(1 for item in items if normalise(item["artist"]) in tags_by_artist)
    with_genres = sum(1 for v in tags_by_artist.values() if v["genres"])

    output = {
        "source": url,
        "licence": "CC BY-NC-SA 3.0 (MusicBrainz supplementary data)",
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "collision_rule": "highest total tag votes wins",
        "stats": {
            "dump_artists_scanned": collected["scanned"],
            "scan_seconds": round(collected["seconds"], 1),
            "target_artists": len(targets),
            "artists_matched": len(tags_by_artist),
            "artists_from_dump": from_dump,
            "artists_from_catalogue_merge": merged,
            "artists_with_genres": with_genres,
            "artists_ambiguous": ambiguous,
            "items_total": len(items),
            "items_covered": covered_items,
        },
        "artists": tags_by_artist,
    }

    out = dataset_dir / "artist_tags.json"
    out.write_text(json.dumps(output, indent=2, sort_keys=True) + "\n")

    print(
        f"\nartists matched   {len(tags_by_artist):,} / {len(targets):,} "
        f"({len(tags_by_artist) / max(len(targets), 1):.1%})\n"
        f"  with genres     {with_genres:,}\n"
        f"  ambiguous name  {ambiguous:,} "
        f"({ambiguous / max(len(tags_by_artist), 1):.1%} of matches)\n"
        f"items covered     {covered_items:,} / {len(items):,} "
        f"({covered_items / max(len(items), 1):.1%})\n"
        f"written to        {out.relative_to(REPO_ROOT)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
