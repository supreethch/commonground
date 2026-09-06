"""Apply numbered SQL migrations, in order, exactly once.

    python db/migrate.py                    # apply anything not yet applied
    python db/migrate.py --status           # show what would be applied

Raw SQL rather than an ORM's migration tool: the schema is the part of this
project a reader is most likely to want to understand quickly, and a .sql file
is legible to anyone. The tradeoff is no autogeneration and no down-migrations,
which is the right trade for a project whose schema is small enough to read.

Each migration runs inside a transaction together with the row recording it, so
a failure halfway through leaves neither a partial schema nor a false record.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

import psycopg

MIGRATIONS_DIR = Path(__file__).resolve().parent

BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename    text        PRIMARY KEY,
    -- The checksum catches an already-applied migration being edited, which is
    -- the failure that otherwise shows up as a mysterious difference between
    -- a developer's database and production's.
    checksum    text        NOT NULL,
    applied_at  timestamptz NOT NULL DEFAULT now()
);
"""


def migration_files() -> list[Path]:
    return sorted(p for p in MIGRATIONS_DIR.glob("*.sql"))


def checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print(
            "DATABASE_URL is not set. For the local compose stack:\n"
            "  export DATABASE_URL=postgresql://commonground:commonground@localhost:5434/commonground",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return url


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", action="store_true", help="show pending migrations, apply none")
    args = parser.parse_args(argv)

    files = migration_files()
    if not files:
        print("no migrations found")
        return 0

    with psycopg.connect(database_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(BOOTSTRAP)
        conn.commit()

        with conn.cursor() as cur:
            cur.execute("SELECT filename, checksum FROM schema_migrations")
            applied = dict(cur.fetchall())

        for path in files:
            name = path.name
            digest = checksum(path)
            if name in applied:
                if applied[name] != digest:
                    print(
                        f"ERROR: {name} was already applied but its contents have changed.\n"
                        "Add a new migration instead of editing an applied one.",
                        file=sys.stderr,
                    )
                    return 1
                print(f"  ok      {name}")
                continue

            if args.status:
                print(f"  pending {name}")
                continue

            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(path.read_text(encoding="utf-8"))
                    cur.execute(
                        "INSERT INTO schema_migrations (filename, checksum) VALUES (%s, %s)",
                        (name, digest),
                    )
            print(f"  applied {name}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
