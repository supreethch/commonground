# commonground

Five people in a car, one speaker, and a recommender that was built for one
person at a time. The usual fix is to average everyone's taste, which reliably
produces the one playlist nobody chose: average the person who wants Ethiopian
jazz with the person who wants hyperpop and you get neither, and the averaging
hides *who* it failed. CommonGround ranks a shared playlist on what a group
actually needs — how satisfied the **least** satisfied member is, whether one
person is quietly driving the whole hour, and whether anyone is being made to sit
through something they have explicitly said they hate — and tells you, per track,
in a sentence, why it is there.

> **Status: milestone 2 of 6 — foundation.** No live demo and no UI yet. What
> works today: real signup and login, Spotify-free taste onboarding against a
> catalogue built from CC0 sources, listening-history import for three export
> formats, and six seeded demo accounts with deliberately conflicting taste. The
> recommender itself lands in M3 and M4. This section will keep saying exactly
> where the project is.

## Why this is not a wrapper around a language model

No LLM produces or explains a playlist here. The ranker is a hybrid of
content-based similarity and collaborative filtering, combined by a group
aggregation step that is a sum of named terms — so the explanation attached to a
track is *derived from the arithmetic that ranked it*, not written alongside it
afterwards. A test asserts every clause in a rendered reason corresponds to a
non-zero score contribution.

That is the whole point. "Recommended because 3 members like indie rock, it's
similar to Alex's favourites, and it introduces a new artist without strongly
conflicting with anyone's dislikes" is only worth reading if each clause is a
number the system actually computed.

## Three modes, one ranker

| Mode | What it optimises |
| --- | --- |
| **Consensus** | Protects the least-satisfied member. Strict vetoes, low novelty. |
| **Discovery** | Rewards music unfamiliar to *everyone*, relaxes the floor slightly. |
| **Fair Rotation** | At each slot, picks for whoever the playlist has served least so far. |

They are three parameter sets over the same ranker, not three code paths — which
also means the evaluation compares them on identical machinery.

## Planned stack

**Engine:** Python 3.12, NumPy, pandas, scikit-learn, `implicit` (ALS)
**API:** FastAPI, Pydantic v2, SQLAlchemy 2.0, PostgreSQL 17, WebSockets
**Frontend:** TypeScript, React 19, Vite, Tailwind
**Tests:** pytest, Vitest, Playwright · **CI:** GitHub Actions · **Local:** Docker Compose

## Docs

[Architecture](docs/architecture.md) ·
[Recommendation engine](docs/recommender.md) ·
[Database schema](db/001_init.sql) ·
[Evaluation plan](docs/evaluation.md) ·
[Data sources and licences](docs/data-sources.md) ·
[Deployment](docs/deployment.md) ·
[Decisions](docs/decisions.md)

## Verify

```bash
docker compose up -d                       # Postgres on :5434, Redis on :6380
python3.12 -m venv .venv                   # 3.12 specifically; 3.9 will not do
./.venv/bin/pip install -e "packages/engine[dev]" -e "apps/api[dev]" "psycopg[binary]"

export DATABASE_URL=postgresql://commonground:commonground@localhost:5434/commonground
./.venv/bin/python db/migrate.py
./.venv/bin/pytest -q
./.venv/bin/ruff check . && ./.venv/bin/ruff format --check .
./.venv/bin/python scripts/verify_sources.py
```

To build the catalogue and try the API for real, see
[docs/development.md](docs/development.md). The catalogue build takes about 40
minutes the first time — almost all of it waiting on MusicBrainz's rate limit —
and is cached and resumable.

The last command re-checks every upstream data source and rewrites
[`docs/source-provenance.json`](docs/source-provenance.json) with what they
actually returned — sizes, timestamps, licences. Numbers quoted in the
documentation come from that file rather than from memory.

## Measured so far

Only what has actually been run. This table grows as milestones land; it will not
contain a number that was not produced by a command in this repository.

| | Measured 2026-09-06 |
| --- | --- |
| Python tests passing | 94 (against both a populated and an empty catalogue) |
| Tables in the schema | 21 |
| Catalogue: recordings / artists | 1,637 / 588 |
| Artists with genre tags | 211 of 588 (top 220 by listen count) |
| Distinct genres / tags | 295 / 610 |
| MusicBrainz canonical dump | 2.2 GiB compressed |
| ListenBrainz daily incremental dump | 370.6 MiB compressed |
| ListenBrainz sitewide stats ceiling | 1,000 rows per time range; offsets past it return empty |
| MusicBrainz genre lookups | 193 fetched, 0 failed, ~9s each under throttling |
| MusicBrainz recording-level URL relations | 1 of 10 recordings in a spot check had any |

Two of those rows changed the design.

**URL relations are too sparse to rely on.** Streaming links were meant to come
from MusicBrainz's CC0 relationships; at the recording level they are mostly
absent. Playback links are deterministic search URLs, with real relations
preferred where they exist, and `recording_links.source` records which is which.

**The genre pass had to be reordered.** MusicBrainz throttles by stalling a
connection ~20s and then returning 503, and the build originally walked artists
in MBID order — so any capped or interrupted run tagged an effectively random
subset, leaving the artists every onboarding screen shows untagged. It now walks
most-listened first, so any prefix of the work is the most useful prefix
available.

**Known gap:** the catalogue comes from ListenBrainz's most-played recordings,
so it skews hard to pop and rock. The seeded ambient/classical persona matches
only 6 artists against 94 for the indie-rock one. That is a real limitation of
this data source, and M3 addresses it by building from the listen dumps instead.

## Licence and data

The code is MIT — see [LICENSE](LICENSE). The data is not all the same, and the
differences are load-bearing:

- MusicBrainz **core** data (artists, recordings, releases, relationships) is
  **CC0**.
- MusicBrainz **tags, including genre associations**, are **CC BY-NC-SA 3.0**.
  Genre is this project's primary content feature, so **CommonGround is
  non-commercial**, credits MusicBrainz, and keeps everything derived from those
  tags out of this repository — `data/` is git-ignored and rebuilt locally.
- ListenBrainz listens are **CC0**.

No upstream data ships here, and **no audio is hosted** — tracks link out to
legal external playback. Full terms in
[docs/data-sources.md](docs/data-sources.md).
