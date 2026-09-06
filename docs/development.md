# Running it locally

Everything here is free and needs no account anywhere.

## Prerequisites

- Python 3.12 (3.9, which macOS ships, will not work)
- Docker, for Postgres and Redis
- Node 22+ (only from M5, when the frontend arrives)

## Setup

```bash
docker compose up -d                       # Postgres on :5434, Redis on :6380
python3.12 -m venv .venv
./.venv/bin/pip install -e "packages/engine[dev]" -e "apps/api[dev]" "psycopg[binary]"

cp .env.example .env                       # then set USER_AGENT to a real address
export DATABASE_URL=postgresql://commonground:commonground@localhost:5434/commonground
./.venv/bin/python db/migrate.py
```

Ports are 5434 and 6380 rather than the defaults so this can run alongside
a2transit (5432/6379) and pulse (5433). `CG_DB_PORT` and `CG_REDIS_PORT`
override them.

## Building the catalogue

```bash
export USER_AGENT="CommonGround/0.1 (you@example.com)"
./.venv/bin/python scripts/build_catalog.py
./.venv/bin/python scripts/seed.py
```

The first command takes **roughly 20 minutes on a first run** and a few seconds
afterwards. Almost all of it is stage 2: MusicBrainz rate-limits to about one
request per second, and there are ~590 artists to look up. Responses are cached
under `data/cache/`, so the script is resumable and a re-run is nearly free.

`--skip-tags` skips stage 2 entirely if you only need recordings and popularity;
`--max-artists N` caps the genre pass. Both produce a working but less useful
catalogue, because genre is the primary content feature.

Nothing it writes is committed. `data/` is git-ignored, and the derived
artist-genre data carries a ShareAlike obligation that must not attach to an MIT
repository — see [data-sources.md](data-sources.md).

### What MusicBrainz actually does when you go too fast

It does not return 503 immediately. It stalls the connection for ~20 seconds and
*then* returns 503. Measured on 2026-09-06: a single client gets ~0.50s per
request for a short burst, then hits that wall. The first version of this script
treated the 503 as a permanent failure and dropped the artist, which produced a
catalogue with quietly missing genres and no error — so the fetch now backs off
and retries, honouring `Retry-After`.

Running two copies of the build at once makes it dramatically worse: with a
second client competing, individual requests measured 13–20 seconds.

## Running the API

```bash
./.venv/bin/uvicorn commonground_api.main:app --reload --port 8000
```

- <http://localhost:8000/docs> — interactive API docs
- <http://localhost:8000/health> — reports whether the database is reachable and
  which broadcaster is in use

## Tests

```bash
./.venv/bin/pytest -q                      # everything except network and slow
./.venv/bin/pytest -q -m network           # also hits the real upstream sources
./.venv/bin/ruff check . && ./.venv/bin/ruff format --check .
```

Database-backed tests **skip themselves** when no Postgres is reachable, so the
suite runs offline. That is a convenience, not a licence to ignore them: CI runs
a Postgres service container precisely so the skip cannot become permanent.

Each database test runs inside a transaction that is rolled back, so the suite
leaves no rows behind and can be run against your development database safely.

## Demo accounts

`scripts/seed.py` creates six accounts, password from `DEMO_USER_PASSWORD`
(default `demo-read-only`):

| Email | Taste |
| --- | --- |
| `alex@commonground.demo` | indie rock |
| `sam@commonground.demo` | hip hop |
| `rio@commonground.demo` | electronic |
| `jules@commonground.demo` | jazz, soul, funk |
| `nina@commonground.demo` | metal |
| `theo@commonground.demo` | ambient, classical |

They are marked `is_demo`, which makes them **read-only through the API**. One
shared login that any visitor can rewrite is a demo that breaks by lunchtime.

The personas are chosen to *disagree*. A room containing Nina and Theo is the
case where averaging visibly fails, which is the whole point of the project — six
people who all liked indie rock would make the recommender look good and prove
nothing.
