"""Check that every upstream data source is reachable, and record what it is.

The point is not monitoring. It is that docs/data-sources.md makes concrete
claims -- this dump is CC0, that one is 370 MiB, this API needs no key -- and a
claim nobody re-checks quietly rots. Running this rewrites
docs/source-provenance.json with what the sources actually returned today, so
the documentation can cite a file instead of a memory.

    python scripts/verify_sources.py            # check and rewrite provenance
    python scripts/verify_sources.py --check     # fail if provenance is stale

Exits non-zero if a source marked required is unreachable.

No third-party dependencies on purpose: this runs before anything is installed,
including in a fresh clone.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

REPO_ROOT = Path(__file__).resolve().parent.parent
PROVENANCE_PATH = REPO_ROOT / "docs" / "source-provenance.json"

# MetaBrainz asks for a real contact address on automated requests, and so does
# every other source here. Same discipline pulse applies to its RSS sources.
USER_AGENT = (
    "CommonGround/0.1 (portfolio project; https://github.com/supreethchittaluri/commonground)"
)

TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class Source:
    """One declared upstream, with the licence we believe applies to it."""

    key: str
    name: str
    url: str
    licence: str
    licence_url: str
    used_for: str
    required: bool = True
    # For directory-listing sources: how to find the newest dump, and the file
    # inside it whose size we want.
    dir_pattern: str | None = None
    file_pattern: str | None = None


SOURCES: tuple[Source, ...] = (
    Source(
        key="musicbrainz_canonical_dump",
        name="MusicBrainz canonical data dump",
        url="https://data.metabrainz.org/pub/musicbrainz/canonical_data/",
        licence="CC0-1.0",
        licence_url="https://musicbrainz.org/doc/About/Data_License",
        used_for="catalogue spine: artists, recordings, releases",
        dir_pattern=r"^musicbrainz-canonical-dump-\d{8}-\d{6}/$",
        file_pattern=r"^musicbrainz-canonical-dump-\d{8}-\d{6}\.tar\.zst$",
    ),
    Source(
        key="listenbrainz_incremental_dump",
        name="ListenBrainz incremental listen dump",
        url="https://data.metabrainz.org/pub/musicbrainz/listenbrainz/incremental/",
        licence="CC0-1.0",
        licence_url="https://listenbrainz.org/data/",
        used_for="user-item interactions for collaborative filtering",
        dir_pattern=r"^listenbrainz-dump-\d+-\d{8}-\d{6}-incremental/$",
        file_pattern=r"^listenbrainz-listens-dump-.*-incremental\.tar\.zst$",
    ),
    Source(
        key="musicbrainz_ws",
        name="MusicBrainz web service",
        # A recording lookup with url-rels: the endpoint the playback-link
        # builder falls back to, and the one whose sparsity is documented.
        url="https://musicbrainz.org/ws/2/recording/?query=recording:test&fmt=json&limit=1",
        licence="CC0-1.0 (core data)",
        licence_url="https://musicbrainz.org/doc/About/Data_License",
        used_for="entity resolution for user-supplied history imports",
    ),
    Source(
        key="listenbrainz_api",
        name="ListenBrainz web API",
        url="https://api.listenbrainz.org/1/stats/sitewide/artists?count=1",
        licence="CC0-1.0",
        licence_url="https://listenbrainz.org/data/",
        used_for="optional live enrichment; never on a user request path",
        required=False,
    ),
)


class Fetcher(Protocol):
    """Minimal HTTP surface, so the checks can be tested without a network."""

    def get(self, url: str) -> tuple[int, bytes, dict[str, str]]: ...

    def head(self, url: str) -> tuple[int, dict[str, str]]: ...


class UrllibFetcher:
    def _open(self, url: str, method: str):
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT}, method=method)
        return urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS)

    def get(self, url: str) -> tuple[int, bytes, dict[str, str]]:
        with self._open(url, "GET") as response:
            return response.status, response.read(), dict(response.headers)

    def head(self, url: str) -> tuple[int, dict[str, str]]:
        with self._open(url, "HEAD") as response:
            return response.status, dict(response.headers)


@dataclass
class Result:
    key: str
    name: str
    ok: bool
    licence: str
    licence_url: str
    used_for: str
    required: bool
    checked_url: str
    status: int | None = None
    error: str | None = None
    resolved_url: str | None = None
    bytes: int | None = None
    last_modified: str | None = None
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        data = {k: v for k, v in self.__dict__.items() if v is not None}
        if not data.get("notes"):
            data.pop("notes", None)
        return data


def _listing_entries(html: bytes) -> list[str]:
    """Pull hrefs out of an Apache-style directory index.

    Deliberately not an HTML parser: these indexes are machine-generated and one
    regex is easier to reason about than a dependency.
    """
    return re.findall(r'href="([^"]+)"', html.decode("utf-8", errors="replace"))


def check_source(source: Source, fetcher: Fetcher) -> Result:
    result = Result(
        key=source.key,
        name=source.name,
        ok=False,
        licence=source.licence,
        licence_url=source.licence_url,
        used_for=source.used_for,
        required=source.required,
        checked_url=source.url,
    )
    try:
        status, body, _ = fetcher.get(source.url)
        result.status = status
        if status != 200:
            result.error = f"HTTP {status}"
            return result

        if source.dir_pattern is None:
            result.ok = True
            return result

        # Directory listing: find the newest dump, then the file inside it.
        dumps = sorted(e for e in _listing_entries(body) if re.match(source.dir_pattern, e))
        if not dumps:
            result.error = "no dump directories matched the expected pattern"
            return result
        newest = dumps[-1]
        result.notes.append(f"{len(dumps)} dump directories listed; newest is {newest}")

        inner_url = source.url + newest
        status, inner_body, _ = fetcher.get(inner_url)
        if status != 200:
            result.error = f"HTTP {status} listing {inner_url}"
            return result

        assert source.file_pattern is not None
        files = [e for e in _listing_entries(inner_body) if re.match(source.file_pattern, e)]
        if not files:
            result.error = f"no file in {newest} matched the expected pattern"
            return result

        result.resolved_url = inner_url + files[0]
        status, headers = fetcher.head(result.resolved_url)
        if status != 200:
            result.error = f"HTTP {status} on {result.resolved_url}"
            return result
        length = headers.get("Content-Length") or headers.get("content-length")
        if length is not None:
            result.bytes = int(length)
        result.last_modified = headers.get("Last-Modified") or headers.get("last-modified")
        result.ok = True
        return result

    except (urllib.error.URLError, OSError, ValueError) as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        return result


def human_bytes(count: int) -> str:
    value = float(count)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GiB"


def run(fetcher: Fetcher, sources: tuple[Source, ...] = SOURCES) -> list[Result]:
    return [check_source(source, fetcher) for source in sources]


def build_provenance(results: list[Result]) -> dict:
    return {
        "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "user_agent": USER_AGENT,
        "sources": [r.as_dict() for r in results],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="do not write; exit non-zero if any required source is unreachable",
    )
    args = parser.parse_args(argv)

    results = run(UrllibFetcher())

    for result in results:
        mark = "ok  " if result.ok else ("FAIL" if result.required else "warn")
        detail = ""
        if result.bytes is not None:
            detail = f"  {human_bytes(result.bytes)}"
        if result.error:
            detail = f"  {result.error}"
        print(f"[{mark}] {result.name}{detail}")
        for note in result.notes:
            print(f"       {note}")
        if result.resolved_url:
            print(f"       {result.resolved_url}")

    failed = [r for r in results if r.required and not r.ok]

    if not args.check:
        PROVENANCE_PATH.write_text(
            json.dumps(build_provenance(results), indent=2) + "\n", encoding="utf-8"
        )
        print(f"\nwrote {PROVENANCE_PATH.relative_to(REPO_ROOT)}")

    if failed:
        print(f"\n{len(failed)} required source(s) unreachable", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
