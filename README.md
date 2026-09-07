# commonground

Five people in a car, one speaker, and a recommender built for one person at a
time. The usual fix is to average everyone's taste, which reliably produces the
one playlist nobody chose: average the person who wants Ethiopian jazz with the
person who wants hyperpop and you get neither — and the averaging hides *who* it
failed. CommonGround ranks a shared playlist on what a group actually needs —
how satisfied the **least** satisfied member is, whether one person is quietly
driving the whole hour, whether anyone is sitting through something they have
explicitly rejected — and tells you, per track, in a sentence, why it is there.

<p>
  <a href="https://commonground-alpha.vercel.app"><img alt="live demo" src="https://img.shields.io/badge/demo-live-4ade80"></a>
  <img alt="Python 3.12" src="https://img.shields.io/badge/python-3.12-3776ab?logo=python&logoColor=white">
  <img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white">
  <img alt="React 19" src="https://img.shields.io/badge/React_19-Vite-61dafb?logo=react&logoColor=black">
  <img alt="PostgreSQL 17" src="https://img.shields.io/badge/PostgreSQL-17-336791?logo=postgresql&logoColor=white">
  <img alt="263 tests" src="https://img.shields.io/badge/tests-263%20passing-4ade80">
  <img alt="MIT" src="https://img.shields.io/badge/licence-MIT-blue">
</p>

![A room of six with a generated playlist. Each track carries a sentence explaining why it is there and a strip of bars showing how well it serves each member, with the least-served one in amber.](docs/images/room.png)

<sub>Six listeners with deliberately conflicting taste. Fair rotation picks each
slot for whoever the room has served least — Alex, then Rio, then Nina — and the
reason on every track is generated from the arithmetic that ranked it, not
written next to it.</sub>

> **[Live demo →](https://commonground-alpha.vercel.app)** — open it and press
> **Try the demo account**. The API runs on a free instance that sleeps after 15
> minutes of no traffic, so the first request after an idle period takes about a
> minute to wake and fit the model; it is quick after that. Everything also runs
> locally with `docker compose up` — see
> [docs/development.md](docs/development.md).

## The thing this project is actually about

No language model produces or explains a playlist here. The ranker is a hybrid
of collaborative filtering and content similarity, combined by a group
aggregation step that is a **sum of named terms** — so the explanation attached
to a track is *derived from the arithmetic that ranked it*. A test asserts, in
both directions, that every rendered clause maps to a term that actually moved
the score and that a term which did not fire produces no clause.

That correspondence is the whole point. "Recommended because everyone likes hip
hop, it's Alex's turn to be served, and it introduces an artist new to everyone"
is only worth reading if each clause is a number the system computed.

![The per-member satisfaction panel, showing each member's predicted score and naming the one served least by this track.](docs/images/satisfaction.png)

## What was measured, including where it loses

Full detail — and the caveats — in **[docs/measurements.md](docs/measurements.md)**,
which is *generated* from `eval/results/`, never typed.

**Individual recommendation**, 2,248 real ListenBrainz users, time-ordered
leave-last-5-out:

| model | P@10 | NDCG@10 | catalogue coverage |
| --- | --- | --- | --- |
| popularity baseline | 0.0199 | 0.0412 | 0.55% |
| **hybrid (item-kNN + ALS)** | **0.0624** | **0.1136** | **37.6%** |

3.1× the precision of popularity while recommending 69× more of the catalogue.

**Group recommendation**, 200 synthetic groups on two datasets, paired bootstrap
against the average-score baseline:

| consensus − average-score | ListenBrainz | MovieLens-1M |
| --- | --- | --- |
| held-out accuracy | not distinguishable | not distinguishable |
| **veto violations** | **−0.073** ✓ | **−0.024** ✓ |
| **worst-artist share** | **−0.074** ✓ | **−0.147** ✓ |
| **predicted floor** | **+0.053** ✓ | **+0.009** ✓ |

The honest claim: **fairness and repetition guarantees at no measurable accuracy
cost.** Not "better recommendations" — the accuracy intervals straddle zero on
both datasets, in both directions.

**I had to withdraw a claim to get here.** Milestone 4 reported that the group
layer wins on groups whose tastes conflict and loses on groups that agree,
calling it the project's thesis measured. It was a ~0.01 gap over 50 groups, it
sits inside the noise band, and **its sign reverses on the second dataset.**
Adding MovieLens is what caught it. The retraction is in the git history and in
the measurements doc, because an evaluation that only ever confirms itself is
not an evaluation.

**API latency**, measured over HTTP against a live server:

| | p50 | p95 |
| --- | --- | --- |
| playlist generation (20 tracks, 6 members) | 15.9ms | 18.0ms |
| login | 27.5ms | 29.5ms |
| everything else | 1.6–4.9ms | ≤5.8ms |

Model fit is 555ms once per process, not per request. No index was added on the
strength of these numbers, because nothing is slow — the honest response to a
fast system is to record the threshold, not to optimise against a problem it
does not have.

## Three modes, one ranker

| mode | optimises |
| --- | --- |
| **Consensus** | Protects whoever is worst served. Strict about strong objections. |
| **Discovery** | Rewards music unfamiliar to *everyone*, at a real accuracy cost. |
| **Fair rotation** | Each slot picks for whoever the room has served least. |

Three parameter sets over one ranker, fitted on validation groups nested inside
the training split — so the evaluation compares them on identical machinery, and
the running app uses the same numbers the measurements report.

The veto is a **hard filter, not a penalty term**: nine members adoring a track
must not outvote one member at zero, and any penalty large enough to prevent that
would block everything else too. When a room is too divided to fill a playlist
under the strict floor, the veto is relaxed one step at a time and **the UI says
so** rather than quietly serving someone a track they would have rejected.

## Stack

**Engine:** Python 3.12, NumPy, scikit-learn, `implicit` (ALS) — a separate
package with no FastAPI, SQLAlchemy or HTTP imports, enforced by a test that
walks its AST.
**API:** FastAPI, Pydantic v2, SQLAlchemy 2.0, PostgreSQL 17, WebSockets.
**Web:** TypeScript, React 19, Vite, Tailwind 4.
**Tests:** pytest, Vitest, Playwright · **CI:** GitHub Actions · **Local:** Docker Compose.

<img src="docs/images/mobile.png" alt="The same room on a phone: members wrap, the reason stays readable, and the satisfaction strip survives a 390px viewport." width="300">

## Running it

```bash
docker compose up -d
python3.12 -m venv .venv
./.venv/bin/pip install -e "packages/engine[dev]" -e "apps/api[dev]" "psycopg[binary]"

export DATABASE_URL=postgresql://commonground:commonground@localhost:5434/commonground
./.venv/bin/python db/migrate.py
./.venv/bin/python scripts/seed_ci_catalogue.py   # synthetic; instant
./.venv/bin/python scripts/seed.py

./.venv/bin/uvicorn commonground_api.main:app --port 8010 &
npm install --prefix apps/web && npm run dev --prefix apps/web
```

Then open <http://localhost:5173> and click **Try the demo account**.

For the real catalogue instead of the synthetic one — about 40 minutes, almost
all of it MusicBrainz's rate limit — see
[docs/development.md](docs/development.md).

## Docs

[Architecture](docs/architecture.md) ·
[Recommendation engine](docs/recommender.md) ·
[**Measurements**](docs/measurements.md) ·
[Evaluation protocol](docs/evaluation.md) ·
[Data sources and licences](docs/data-sources.md) ·
[Deployment](docs/deployment.md) ·
[Decisions](docs/decisions.md) ·
[Running it locally](docs/development.md)

## Some things that went wrong

Kept because a repository containing only successes is not an audit trail.

- **A room of eight returned an empty playlist.** The veto's survival rate is
  `0.65ⁿ` and the candidate pool was *shrinking* as it needed to grow, because
  every member's history is excluded from it. Found by measuring room sizes 2–12,
  not by using the app.
- **`Counter.update(dict)` adds the dict's values as counts.** Mine were
  timestamps, so every item scored ~1.8 billion and sailed through a "≥5
  listeners" filter. Caught because 420,530 surviving items on 636,072
  interactions is arithmetically impossible.
- **The WebSocket held a pooled database connection for its entire lifetime.**
  With `pool_size=5`, ten open rooms would have exhausted the pool and stopped
  the API answering anything.
- **Every reason in a playlist read identically.** Member genres came from
  listens alone, so a freshly-onboarded user — the one most likely to be looking
  at the demo — got the same fallback sentence twenty times.
- **The app and its own measurements ran different parameters.** The evaluation
  applied fitted values through a config override while the API used the
  documented defaults, so the numbers described a system nobody was running.

## Licence and data

The code is MIT — see [LICENSE](LICENSE). The data is not all the same, and the
differences are load-bearing:

- MusicBrainz **core** data (artists, recordings, relationships) is **CC0**.
- MusicBrainz **tags, including genre associations**, are **CC BY-NC-SA 3.0**.
  Genre is this project's primary content feature, so **CommonGround is
  non-commercial**, credits MusicBrainz, and keeps everything derived from those
  tags out of this repository — `data/` is git-ignored and rebuilt locally. CI
  generates a synthetic catalogue rather than committing one, for the same
  reason.
- ListenBrainz listens are **CC0**. MovieLens is used under GroupLens's research
  terms and is not redistributed here.

No upstream data ships in this repository, and **no audio is hosted** — tracks
link out to legal external playback.
