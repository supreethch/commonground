"""Test fixtures for the API.

Every test runs against a real Postgres inside a transaction that is rolled
back, so the suite leaves no rows behind and tests cannot see each other's data.
There is no SQLite fallback: the schema uses JSONB, uuid arrays and ON CONFLICT,
and a test suite that passes on a database the application never runs against is
worse than no suite at all.

Skipped automatically when no database is reachable, so `pytest` still works
offline -- the engine and parser tests carry on regardless.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

from commonground_api.api.rooms import get_socket_session_factory  # noqa: E402
from commonground_api.config import Settings  # noqa: E402
from commonground_api.db import get_session, normalise_url  # noqa: E402
from commonground_api.deps import get_settings  # noqa: E402
from commonground_api.main import create_app  # noqa: E402
from commonground_api.ratelimit import reset_limiter  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_URL = "postgresql://commonground:commonground@localhost:5434/commonground"

pytestmark = pytest.mark.db


def _database_url() -> str:
    return os.environ.get("DATABASE_URL", DEFAULT_URL)


@pytest.fixture(scope="session")
def database_url() -> str:
    url = _database_url()
    try:
        psycopg.connect(url, connect_timeout=3).close()
    except psycopg.OperationalError as exc:
        pytest.skip(f"no database at {url}: {exc}")

    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "db" / "migrate.py")],
        env={**os.environ, "DATABASE_URL": url},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"migrations failed:\n{result.stdout}\n{result.stderr}"
    return url


@pytest.fixture(scope="session")
def engine(database_url: str):
    return create_engine(normalise_url(database_url), future=True)


@pytest.fixture
def session(engine) -> Iterator[Session]:
    """A session bound to a transaction that is rolled back afterwards.

    The connection is held open and the session joined to its transaction, so
    even code that commits (the endpoints all do) is undone at the end.
    """
    connection = engine.connect()
    transaction = connection.begin()
    db = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield db
    finally:
        db.close()
        transaction.rollback()
        connection.close()


@pytest.fixture(autouse=True)
def _fresh_rate_limiter():
    """Counters are process-global, so without this the sixth test to sign up
    gets a 429 from the fifth test's traffic."""
    reset_limiter()
    yield
    reset_limiter()


@pytest.fixture(autouse=True)
def _fresh_recommender_snapshot():
    """The recommender caches its fitted model in a process-global singleton.

    Each test runs in a transaction that is rolled back, so a snapshot built
    inside one test references recording ids that no longer exist once it ends.
    Without this reset, the next test to generate a playlist reuses that stale
    snapshot and inserts playlist_tracks rows that violate the foreign key --
    which is exactly how CI first failed while local runs, with a real catalogue
    already committed, passed.
    """
    from commonground_api.services.recommender import recommender_service

    recommender_service.invalidate()
    yield
    recommender_service.invalidate()


@pytest.fixture
def settings() -> Settings:
    return Settings(
        environment="test",
        jwt_secret="test-secret-that-is-long-enough-to-be-plausible",
        database_url=_database_url(),
    )


@pytest.fixture
def client(session: Session, settings: Settings) -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_settings] = lambda: settings

    # The websocket handshake opens its own short-lived session so it does not
    # hold a pooled connection for the socket's lifetime. Point that factory at
    # the test transaction, or the handshake cannot see users this test created
    # and every socket test fails as "not authenticated".
    @contextlib.contextmanager
    def _test_session():
        yield session

    app.dependency_overrides[get_socket_session_factory] = lambda: _test_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def register(client: TestClient):
    """Create an account and return its tokens plus an auth header."""

    def _register(email: str | None = None, password: str = "correct horse battery staple"):
        email = email or f"user-{uuid.uuid4().hex[:12]}@example.com"
        response = client.post(
            "/api/auth/signup",
            json={"email": email, "password": password, "display_name": "Test User"},
        )
        assert response.status_code == 201, response.text
        body = response.json()
        headers = {"Authorization": f"Bearer {body['access_token']}"}
        me = client.get("/api/auth/me", headers=headers)
        return {
            "email": email,
            "password": password,
            "headers": headers,
            "user_id": me.json()["id"],
            **body,
        }

    return _register


@pytest.fixture
def catalogue(session: Session):
    """Insert a tiny, known catalogue.

    Fixed rather than sampled from whatever the real build produced: a test that
    depends on Radiohead being in the database fails for reasons unrelated to
    the code under test.
    """

    def _make(count: int = 3) -> dict:
        artists, recordings = [], []
        tag_id = session.execute(
            text(
                "INSERT INTO tags (name, is_genre) VALUES (:n, true) "
                "ON CONFLICT (name) DO UPDATE SET is_genre = true RETURNING id"
            ),
            {"n": f"test-genre-{uuid.uuid4().hex[:8]}"},
        ).scalar_one()

        for index in range(count):
            artist_id = session.execute(
                text(
                    "INSERT INTO artists (mbid, name, listen_count) "
                    "VALUES (gen_random_uuid(), :n, :c) RETURNING id"
                ),
                {"n": f"Test Artist {index}", "c": 1000 - index},
            ).scalar_one()
            session.execute(
                text("INSERT INTO artist_tags (artist_id, tag_id, weight) VALUES (:a, :t, 1.0)"),
                {"a": artist_id, "t": tag_id},
            )
            recording_id = session.execute(
                text(
                    "INSERT INTO recordings (mbid, title, listen_count) "
                    "VALUES (gen_random_uuid(), :t, :c) RETURNING id"
                ),
                {"t": f"Test Track {index}", "c": 500 - index},
            ).scalar_one()
            session.execute(
                text(
                    "INSERT INTO recording_artists (recording_id, artist_id, position) "
                    "VALUES (:r, :a, 0)"
                ),
                {"r": recording_id, "a": artist_id},
            )
            artists.append(artist_id)
            recordings.append(recording_id)

        session.flush()
        return {"artist_ids": artists, "recording_ids": recordings, "tag_id": tag_id}

    return _make
