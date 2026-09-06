"""Tests for the upstream source verifier.

These never touch the network. A fake fetcher returns canned directory listings,
so the parsing and the failure handling are exercised offline and in CI. The one
test that does hit the real sources is marked `network` and deselected by
default.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import verify_sources  # noqa: E402
from verify_sources import Source, build_provenance, check_source, human_bytes  # noqa: E402


class FakeFetcher:
    """Serves canned responses; raises on an unexpected URL rather than 404ing.

    An unexpected URL means the code under test changed what it asks for, and a
    silent 404 would look like an upstream failure instead of a test that needs
    updating.
    """

    def __init__(self, pages: dict[str, bytes], heads: dict[str, dict[str, str]]):
        self.pages = pages
        self.heads = heads
        self.requested: list[str] = []

    def get(self, url: str):
        self.requested.append(url)
        if url not in self.pages:
            raise AssertionError(f"unexpected GET {url}")
        return 200, self.pages[url], {}

    def head(self, url: str):
        self.requested.append(url)
        if url not in self.heads:
            raise AssertionError(f"unexpected HEAD {url}")
        return 200, self.heads[url]


DUMP_SOURCE = Source(
    key="fake_dump",
    name="Fake dump",
    url="https://example.test/dumps/",
    licence="CC0-1.0",
    licence_url="https://example.test/licence",
    used_for="testing",
    dir_pattern=r"^listenbrainz-dump-\d+-\d{8}-\d{6}-incremental/$",
    file_pattern=r"^listenbrainz-listens-dump-.*-incremental\.tar\.zst$",
)


def _index(entries: list[str]) -> bytes:
    return ("".join(f'<a href="{e}">{e}</a>\n' for e in entries)).encode()


def test_resolves_newest_dump_and_records_size() -> None:
    outer = _index(
        [
            "../",
            "listenbrainz-dump-2651-20260904-000002-incremental/",
            "listenbrainz-dump-2653-20260906-000002-incremental/",
            "listenbrainz-dump-2652-20260905-000002-incremental/",
        ]
    )
    inner = _index(
        [
            "../",
            "SHA256SUMS",
            "listenbrainz-listens-dump-2653-20260906-000002-incremental.tar.zst",
        ]
    )
    newest = "https://example.test/dumps/listenbrainz-dump-2653-20260906-000002-incremental/"
    target = newest + "listenbrainz-listens-dump-2653-20260906-000002-incremental.tar.zst"

    fetcher = FakeFetcher(
        pages={DUMP_SOURCE.url: outer, newest: inner},
        heads={
            target: {
                "Content-Length": "388100096",
                "Last-Modified": "Sun, 06 Sep 2026 00:08:00 GMT",
            }
        },
    )

    result = check_source(DUMP_SOURCE, fetcher)

    assert result.ok
    assert result.resolved_url == target
    assert result.bytes == 388100096
    assert result.last_modified == "Sun, 06 Sep 2026 00:08:00 GMT"
    # Listings are not sorted upstream; the newest must be chosen, not the last
    # one that happened to appear in the HTML.
    assert "2653" in result.resolved_url


def test_ignores_entries_that_do_not_match_the_dump_pattern() -> None:
    outer = _index(["../", "index.html", "listenbrainz-dump-2653-20260906-000002-full/"])
    fetcher = FakeFetcher(pages={DUMP_SOURCE.url: outer}, heads={})

    result = check_source(DUMP_SOURCE, fetcher)

    assert not result.ok
    assert result.error is not None
    assert "no dump directories matched" in result.error


def test_missing_file_inside_the_newest_dump_is_a_failure_not_a_crash() -> None:
    newest = "https://example.test/dumps/listenbrainz-dump-2653-20260906-000002-incremental/"
    fetcher = FakeFetcher(
        pages={
            DUMP_SOURCE.url: _index(["listenbrainz-dump-2653-20260906-000002-incremental/"]),
            newest: _index(["../", "SHA256SUMS"]),
        },
        heads={},
    )

    result = check_source(DUMP_SOURCE, fetcher)

    assert not result.ok
    assert result.error is not None
    assert "no file in" in result.error


def test_non_200_is_reported_with_its_status() -> None:
    class Failing:
        def get(self, url: str):
            return 503, b"", {}

        def head(self, url: str):
            raise AssertionError("should not HEAD after a failed GET")

    result = check_source(DUMP_SOURCE, Failing())

    assert not result.ok
    assert result.status == 503
    assert result.error == "HTTP 503"


def test_network_errors_are_caught_and_named() -> None:
    class Exploding:
        def get(self, url: str):
            raise OSError("connection reset")

        def head(self, url: str):
            raise OSError("connection reset")

    result = check_source(DUMP_SOURCE, Exploding())

    assert not result.ok
    assert result.error is not None
    assert "OSError" in result.error


def test_plain_url_source_needs_no_directory_walk() -> None:
    source = Source(
        key="api",
        name="Some API",
        url="https://example.test/api",
        licence="CC0-1.0",
        licence_url="https://example.test/licence",
        used_for="testing",
    )
    fetcher = FakeFetcher(pages={source.url: b"{}"}, heads={})

    result = check_source(source, fetcher)

    assert result.ok
    assert fetcher.requested == ["https://example.test/api"]


def test_provenance_records_the_licence_for_every_source() -> None:
    fetcher = FakeFetcher(pages={DUMP_SOURCE.url: _index([])}, heads={})
    provenance = build_provenance([check_source(DUMP_SOURCE, fetcher)])

    assert provenance["checked_at"]
    assert provenance["user_agent"].startswith("CommonGround/")
    assert provenance["sources"][0]["licence"] == "CC0-1.0"
    assert provenance["sources"][0]["licence_url"]


def test_every_declared_source_carries_a_licence_and_a_purpose() -> None:
    # docs/data-sources.md promises that nothing is ingested without a recorded
    # licence. This is that promise as an assertion.
    for source in verify_sources.SOURCES:
        assert source.licence, f"{source.key} has no licence"
        assert source.licence_url.startswith("https://"), f"{source.key} has no licence URL"
        assert source.used_for, f"{source.key} does not say what it is used for"


@pytest.mark.parametrize(
    ("count", "expected"),
    [(512, "512 B"), (1536, "1.5 KiB"), (388100096, "370.1 MiB"), (2 * 1024**3, "2.0 GiB")],
)
def test_human_bytes(count: int, expected: str) -> None:
    assert human_bytes(count) == expected


@pytest.mark.network
def test_real_sources_are_reachable() -> None:
    """Opt-in. Run with `pytest -m network` when the documentation is updated."""
    results = verify_sources.run(verify_sources.UrllibFetcher())
    failed = [r.name for r in results if r.required and not r.ok]
    assert not failed, f"unreachable required sources: {failed}"
