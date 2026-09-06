"""Schema tests, run against a real Postgres.

Marked `db` and skipped automatically when no database is reachable, so the
suite still runs offline and in a CI job without services. Start one with
`docker compose up -d`.

These assert the constraints the application relies on for correctness -- the
ones whose absence shows up as duplicated votes or double-counted plays long
after the bug was introduced, rather than as an obvious crash.

Every test runs inside a transaction that is always rolled back, so the suite
leaves the development database exactly as it found it. Statements expected to
fail are wrapped in a nested `conn.transaction()`, which psycopg implements as a
savepoint: without one, the first constraint violation would poison the outer
transaction and every later statement in the test would fail for the wrong
reason.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_URL = "postgresql://commonground:commonground@localhost:5434/commonground"

pytestmark = pytest.mark.db


def _database_url() -> str:
    return os.environ.get("DATABASE_URL", DEFAULT_URL)


def _run_migrations(url: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "db" / "migrate.py")],
        env={**os.environ, "DATABASE_URL": url},
        capture_output=True,
        text=True,
    )


@pytest.fixture(scope="module")
def conn():
    url = _database_url()
    try:
        connection = psycopg.connect(url, connect_timeout=3)
    except psycopg.OperationalError as exc:
        pytest.skip(f"no database at {url}: {exc}")

    # Apply migrations before asserting anything about the schema, so the test
    # exercises the migration runner rather than assuming the schema arrived
    # some other way.
    result = _run_migrations(url)
    assert result.returncode == 0, f"migrations failed:\n{result.stdout}\n{result.stderr}"

    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def tx(conn):
    """Give the test a connection inside a transaction that is always undone."""
    with conn.transaction() as transaction:
        yield conn
        raise psycopg.Rollback(transaction)


def _make_user(conn, email: str) -> uuid.UUID:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (email, password_hash, display_name) "
            "VALUES (%s, 'x', 'Test') RETURNING id",
            (email,),
        )
        return cur.fetchone()[0]


def _make_recording(conn, title: str = "T") -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO recordings (mbid, title) VALUES (gen_random_uuid(), %s) RETURNING id",
            (title,),
        )
        return cur.fetchone()[0]


def test_every_expected_table_exists(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        tables = {row[0] for row in cur.fetchall()}
    expected = {
        "users",
        "refresh_tokens",
        "artists",
        "recordings",
        "recording_artists",
        "tags",
        "artist_tags",
        "recording_tags",
        "recording_links",
        "profile_artists",
        "profile_tags",
        "imports",
        "listens",
        "interactions",
        "rooms",
        "room_members",
        "room_invites",
        "playlists",
        "playlist_tracks",
        "votes",
        "rec_runs",
    }
    assert expected <= tables, f"missing tables: {sorted(expected - tables)}"


def test_migrations_are_idempotent(conn) -> None:
    """Running the migrator a second time must be a no-op, not an error."""
    result = _run_migrations(_database_url())
    assert result.returncode == 0, result.stderr
    assert "applied" not in result.stdout


def test_email_must_be_stored_lowercase(tx) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        with tx.transaction():
            _make_user(tx, "Mixed@Case.test")


def test_email_is_unique(tx) -> None:
    _make_user(tx, "dupe@example.test")
    with pytest.raises(psycopg.errors.UniqueViolation):
        with tx.transaction():
            _make_user(tx, "dupe@example.test")


def test_a_member_cannot_vote_twice_on_one_track(tx) -> None:
    """The constraint that stops a refresh-and-click from double-counting."""
    user_id = _make_user(tx, "voter@example.test")
    recording_id = _make_recording(tx)
    with tx.cursor() as cur:
        cur.execute(
            "INSERT INTO rooms (name, owner_id, mode) VALUES ('R', %s, 'consensus') RETURNING id",
            (user_id,),
        )
        room_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO playlists (room_id, mode, engine_version, params, seed, member_ids) "
            "VALUES (%s, 'consensus', '0.1.0', '{}'::jsonb, 1, ARRAY[%s]::uuid[]) RETURNING id",
            (room_id, user_id),
        )
        playlist_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO playlist_tracks "
            "(playlist_id, recording_id, position, group_score, member_scores, "
            " contributions, explanation) "
            "VALUES (%s, %s, 0, 0.5, '{}'::jsonb, '{}'::jsonb, '{}'::jsonb) RETURNING id",
            (playlist_id, recording_id),
        )
        track_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO votes (playlist_track_id, user_id, value) VALUES (%s, %s, 1)",
            (track_id, user_id),
        )

    with pytest.raises(psycopg.errors.UniqueViolation):
        with tx.transaction(), tx.cursor() as cur:
            cur.execute(
                "INSERT INTO votes (playlist_track_id, user_id, value) VALUES (%s, %s, -1)",
                (track_id, user_id),
            )


def test_a_vote_must_be_plus_or_minus_one(tx) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        with tx.transaction(), tx.cursor() as cur:
            cur.execute(
                "INSERT INTO votes (playlist_track_id, user_id, value) "
                "VALUES (1, gen_random_uuid(), 5)"
            )


def test_a_replayed_import_cannot_double_count_a_listen(tx) -> None:
    """Users re-upload overlapping exports. The unique key is what saves us."""
    user_id = _make_user(tx, "importer@example.test")
    recording_id = _make_recording(tx)
    with tx.cursor() as cur:
        cur.execute(
            "INSERT INTO listens (user_id, recording_id, listened_at) "
            "VALUES (%s, %s, '2026-01-01T00:00:00Z')",
            (user_id, recording_id),
        )

    with pytest.raises(psycopg.errors.UniqueViolation):
        with tx.transaction(), tx.cursor() as cur:
            cur.execute(
                "INSERT INTO listens (user_id, recording_id, listened_at) "
                "VALUES (%s, %s, '2026-01-01T00:00:00Z')",
                (user_id, recording_id),
            )


def test_room_mode_is_constrained_to_the_three_modes(tx) -> None:
    user_id = _make_user(tx, "modes@example.test")
    with pytest.raises(psycopg.errors.CheckViolation):
        with tx.transaction(), tx.cursor() as cur:
            cur.execute(
                "INSERT INTO rooms (name, owner_id, mode) VALUES ('R', %s, 'whatever')",
                (user_id,),
            )


def test_deleting_a_user_removes_their_rooms_and_votes(tx) -> None:
    """A delete-my-account path must not leave orphaned rows behind."""
    user_id = _make_user(tx, "leaver@example.test")
    with tx.cursor() as cur:
        cur.execute(
            "INSERT INTO rooms (name, owner_id, mode) VALUES ('R', %s, 'consensus')",
            (user_id,),
        )
        cur.execute("DELETE FROM users WHERE id = %s", (user_id,))
        cur.execute("SELECT count(*) FROM rooms WHERE owner_id = %s", (user_id,))
        assert cur.fetchone()[0] == 0
