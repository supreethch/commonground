# Architecture

## The shape of the problem

Single-user recommendation asks "what will this person like?" Group
recommendation asks a harder question: **whose preference loses, and by how
much?** A playlist that maximises average satisfaction can leave one member
miserable for its whole length, and averages hide that by construction. So the
system is built around per-member predicted satisfaction as a first-class value
that survives all the way to the UI — not a number that gets averaged away in
the ranker and reconstructed afterwards for a caption.

That decision drives most of what follows: why the engine is a separate package,
why scoring is a sum of named contributions rather than a black box, and why
explanations are derived from the arithmetic instead of written next to it.

## Layout

```
commonground/
├── packages/engine/     commonground_engine — the recommender. Pure Python.
│                        Takes arrays and dataframes, returns rankings.
│                        No FastAPI, no SQLAlchemy, no HTTP, no globals.
├── apps/api/            FastAPI: REST for CRUD, WebSocket for room events.
│                        Owns persistence, auth, and the mapping from database
│                        rows to the engine's array inputs.
├── apps/web/            React 19 + Vite + TypeScript + Tailwind.
├── packages/            (Python) engine only for now; a second package appears
│                        in M2 if the import parsers outgrow the API app.
├── db/                  Numbered SQL migrations, applied by db/migrate.py.
├── data/                Git-ignored. Built by scripts/build_catalog.py.
├── eval/                Experiment configs, and results written by the runner.
├── scripts/             verify_sources.py, build_catalog.py, run_eval.py, measure.py
└── docs/
```

### Why the engine is its own package

Three reasons, in order of how much they matter:

1. **It can be evaluated without the app.** Offline evaluation over hundreds of
   thousands of interactions must not require a database, a web server or a
   login. `scripts/run_eval.py` imports `commonground_engine` and nothing else
   from this repository.
2. **It can be tested for properties, not just outputs.** Determinism,
   monotonicity ("adding a member who likes X cannot decrease X's group score"),
   and the veto guarantee are properties of pure functions. They are painful to
   assert through an HTTP client and easy to assert against arrays.
3. **The layering can be enforced.** A test walks the engine's imports and fails
   if `fastapi`, `sqlalchemy`, `redis` or `httpx` appear. Architecture claims
   that no test enforces stop being true within a month.

The API's job at the boundary is narrow: load the rows, build the matrices, call
the engine, persist the result with the parameters that produced it.

## Request paths

**REST** (`/api/*`) handles everything with a request/response shape: signup,
login, taste onboarding, history upload, room creation, invite redemption,
playlist retrieval, history.

**WebSocket** (`/ws/rooms/{room_id}`) carries the things that are only
interesting live: a member joining, a vote landing, a track being skipped, a
playlist finishing regeneration. Votes are *written over REST and broadcast over
the socket*, rather than being accepted as socket messages. A dropped socket then
costs a user their live updates but never their vote, and the write path keeps
one set of validation and rate limits instead of two.

### Broadcasting, and why Redis is optional

The socket layer sits behind a `Broadcaster` interface with two
implementations:

- `InProcessBroadcaster` — an in-memory fan-out to the sockets this process
  holds. Correct whenever there is exactly one API process.
- `RedisBroadcaster` — Redis pub/sub, correct across processes.

The free-tier deployment runs a single instance, so it uses the in-process one
and needs no Redis at all; `docker compose` runs Redis locally so the
multi-instance path is exercised rather than merely written. This is the same
move a2transit makes by polling in-process when there is no worker to run: the
distributed implementation exists and is tested, but the free deployment does
not pay for infrastructure it cannot use.

### Where recommendation runs

Unknown until measured, and the design does not pretend otherwise. Generating a
playlist for a room is: load member profiles, build candidate sets, score, rank.
If p95 for a realistic room is comfortably inside a request, it stays synchronous
and the code is simpler. If it is not, it moves behind a job with the socket
delivering completion — which is the reason the socket exists in the first place,
so the fallback costs no new infrastructure.

`scripts/measure.py` answers this in M3 and the answer goes in
`docs/measurements.md`. Choosing the async path now would be guessing.

## Stack, and what is deliberately different from a2transit and pulse

| Layer | Choice | Note |
| --- | --- | --- |
| Frontend | React 19, Vite, TypeScript, Tailwind | Same as both existing projects — deliberate reuse |
| API | Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2.0 | Same as a2transit |
| Engine | NumPy, pandas, scipy, scikit-learn, `implicit` | New: neither project has a model layer |
| Database | PostgreSQL 17 | No PostGIS — nothing here is geographic |
| Real-time | WebSockets | New: a2transit polls, pulse uses one-way SSE |
| Cache/jobs | Redis (local; optional in production) | |
| Tests | pytest, Vitest, Playwright | New: **neither existing repo has e2e tests** |
| CI | GitHub Actions running lint + tests | New: **neither existing repo runs tests in CI** |

The last two rows are worth stating out loud. a2transit has 22 test files and
pulse has 262 tests, but neither repository has a workflow that runs them on a
pull request — pulse's only workflow is a keepalive ping. CommonGround gets CI
from the first commit that has something to run.

## Determinism

Given the same catalogue snapshot, the same member profiles, the same mode and
the same seed, a playlist is byte-identical. That requires care in specific
places, so they are named here and tested in M4:

- Every sort has an explicit final tiebreak on recording MBID. No reliance on
  Python's sort stability over an upstream ordering that could change.
- No iteration over unordered sets where the order reaches output.
- One seeded `numpy.random.Generator`, passed explicitly. No global seeding.
- The engine version, the parameter set and the seed are written onto the
  playlist row, so any saved playlist can be regenerated and diffed.

Determinism is not pedantry here: it is what makes the explanations honest. If
the ranking could not be reproduced, the explanation attached to it could not be
checked.

## Failure and degradation

- **A member with no taste profile** (invited, joined, never onboarded) does not
  block generation. They are scored from group priors and the UI says their
  contribution is a prior, not a prediction.
- **A cold catalogue item** with no interactions falls back to content-only
  scoring, and the explanation says so.
- **A dropped socket** reconnects with a room-state fetch over REST. The socket
  is a delivery optimisation, never the only route to a fact.
- **A slow or absent upstream** (ListenBrainz API, Last.fm) never blocks a
  request path; the catalogue is built offline.
