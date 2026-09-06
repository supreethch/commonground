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

> **Status: milestone 1 of 6 — research and architecture.** There is no live demo
> and no working app yet. What exists is the design, the schema, and the tests
> and tooling that keep the documentation honest. This section will keep saying
> exactly where the project is.

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

## Verify milestone 1

```bash
docker compose up -d                       # Postgres on :5434, Redis on :6380
python3.12 -m venv .venv                   # 3.12 specifically; 3.9 will not do
./.venv/bin/pip install -e "packages/engine[dev]" "psycopg[binary]"

export DATABASE_URL=postgresql://commonground:commonground@localhost:5434/commonground
./.venv/bin/python db/migrate.py
./.venv/bin/pytest -q
./.venv/bin/ruff check . && ./.venv/bin/ruff format --check .
./.venv/bin/python scripts/verify_sources.py
```

The last command re-checks every upstream data source and rewrites
[`docs/source-provenance.json`](docs/source-provenance.json) with what they
actually returned — sizes, timestamps, licences. Numbers quoted in the
documentation come from that file rather than from memory.

## Measured so far

Only what has actually been run. This table grows as milestones land; it will not
contain a number that was not produced by a command in this repository.

| | Measured 2026-09-06 |
| --- | --- |
| Python tests passing | 23 (7 schema tests against a live Postgres 17) |
| Tables in the schema | 21 |
| MusicBrainz canonical dump | 2.2 GiB compressed |
| ListenBrainz daily incremental dump | 370.6 MiB compressed |
| MusicBrainz recording-level URL relations | 1 of 10 recordings in a spot check had any |

That last row changed the design: streaming links were meant to come from
MusicBrainz's CC0 URL relationships, and at the recording level they are too
sparse to rely on. Playback links are now deterministic search URLs, with real
relations used where they exist. Actual coverage gets measured during M2 ingest.

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
