# commonground

Six people, one speaker, and every recommender built for one person at a time.
The usual fix is to average the group's taste, which reliably produces the one
playlist nobody chose, and averaging also hides *who* it failed. CommonGround
ranks a shared playlist on the thing a group actually cares about: how the
least-served person is doing. It reports that as a number, attributes it to a
name, and puts a plain-English reason under every track, generated from the
arithmetic that ranked it.

**[▶ Try it live](https://commonground-alpha.vercel.app)** ·
[API docs](https://commonground-api-2ysd.onrender.com/docs) ·
demo login `alex@commonground.demo` / `demo-read-only`

<p>
  <a href="https://commonground-alpha.vercel.app"><img alt="live demo" src="https://img.shields.io/badge/demo-live-4ade80"></a>
  <img alt="Python 3.12" src="https://img.shields.io/badge/python-3.12-3776ab?logo=python&logoColor=white">
  <img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white">
  <img alt="React 19" src="https://img.shields.io/badge/React_19-Vite-61dafb?logo=react&logoColor=black">
  <img alt="PostgreSQL 17" src="https://img.shields.io/badge/PostgreSQL-17-336791?logo=postgresql&logoColor=white">
  <img alt="260 tests passing" src="https://img.shields.io/badge/tests-260%20passing-4ade80">
  <img alt="MIT licence" src="https://img.shields.io/badge/licence-MIT-blue">
</p>

![Signing in as one of six seeded listeners, opening a room of people with deliberately conflicting taste, building a playlist where every track carries a reason, opening the per-track satisfaction to see who it serves least, voting, then switching the ranking mode to watch the same room re-rank.](docs/images/demo.gif)

<sub>Six listeners with deliberately conflicting taste. The summary states the
floor (<strong>0.73, and it belongs to Nina</strong>) and counts the tracks each
member was the best match for. Switching Fair rotation to Discovery re-ranks the
same room against the same catalogue, the comparison that makes the modes mean
anything. Playlists build in <strong>16ms</strong>.</sub>

<img src="docs/images/mobile.png" alt="The same room on a phone: members wrap to two rows, the reason stays readable, and the satisfaction strip survives a 390px viewport." width="280">

> The live demo sleeps when idle, so the first request can take up to a minute to
> wake the API. The app shows its progress while it waits.

## What it does

- **Ranks on the floor, not the average.** `G(i) = α·mean + (1−α)·min` over
  per-member predicted satisfaction, then a sequential selection that also
  weighs redundancy, artist repetition, novelty, and whose turn it is.
- **A hard veto that cannot be outvoted.** Nine members adoring a track does not
  override one member at zero. The veto is a filter, not a penalty term, because
  any penalty large enough to block that case would block everything else too.
- **Three modes over one ranker**, switchable inside the room: Consensus,
  Discovery, Fair rotation. Three parameter sets, not three code paths, so the
  evaluation compares them on identical machinery.
- **A reason under every track, derived from the score.** Because the ranking is
  a sum of *named* terms, the sentence is built from the terms that actually
  fired, and a test asserts the correspondence in both directions.
- **Spotify-free onboarding.** Pick artists and genres, or upload a listening
  history: Spotify GDPR export, ListenBrainz, or Last.fm CSV. No OAuth, and none
  of Spotify's restricted endpoints.
- **Rooms, invite links and live voting** over WebSockets. Votes are written over
  REST and only *broadcast* on the socket, so a dropped connection costs live
  updates but never a vote.

![The per-member satisfaction panel: each member's predicted score for one track, with the least-served member named.](docs/images/satisfaction.png)

## What was measured

Every number here comes from [docs/measurements.md](docs/measurements.md), which
is generated from `eval/results/` rather than typed. That file also records
measurements that turned out to be wrong and how they were caught. One example: a
Milestone 4 result that appeared to confirm the project's thesis was withdrawn
after a second dataset (MovieLens-1M) reversed its sign. The gap had been inside
the noise band all along.

**Individual recommendation.** 2,248 real ListenBrainz users, time-ordered
leave-last-5-out, ranking the full catalogue:

| model | P@10 | NDCG@10 | catalogue coverage |
| --- | --- | --- | --- |
| popularity | 0.0199 | 0.0412 | 0.55% |
| **hybrid (item-kNN + ALS)** | **0.0624** | **0.1136** | **37.6%** |

3.1× the precision of a popularity baseline, recommending 69× more of the
catalogue.

**Group recommendation.** 200 synthetic groups, paired bootstrap against the
average-score baseline, on two datasets:

| consensus − average-score | ListenBrainz | MovieLens-1M |
| --- | --- | --- |
| held-out accuracy | not distinguishable | not distinguishable |
| **veto violations** | **−0.073** ✓ | **−0.024** ✓ |
| **worst-artist share** | **−0.074** ✓ | **−0.147** ✓ |
| **predicted floor** | **+0.053** ✓ | **+0.009** ✓ |

The claim this earns is precise: fairness and repetition guarantees at no
measurable accuracy cost. Not "better recommendations", since the accuracy
intervals straddle zero in both directions.

**Latency**, measured over HTTP against a live server: playlist generation
**15.9ms p50 / 18.0ms p95**, everything else under 6ms, login 27.5ms (almost all
of it Argon2, and deliberately so).

## Stack

**Engine:** Python 3.12, NumPy, scikit-learn, `implicit`. A separate package with
no FastAPI, SQLAlchemy or HTTP imports, enforced by a test that walks its
imports.
**Backend:** FastAPI, Pydantic v2, SQLAlchemy 2.0, PostgreSQL 17, WebSockets, Redis (optional).
**Frontend:** TypeScript, React 19, Vite, Tailwind 4, hash routing.
**Tests:** pytest, Vitest, Playwright · **CI:** GitHub Actions · **Local:** Docker Compose.

## Docs

[Architecture](docs/architecture.md) · [Recommendation engine](docs/recommender.md) ·
[**Measurements**](docs/measurements.md) · [Evaluation protocol](docs/evaluation.md) ·
[Data sources](docs/data-sources.md) · [Deployment](docs/deployment.md) ·
[Decisions](docs/decisions.md) · [Running it locally](docs/development.md)

## Running it locally

```bash
docker compose up -d
python3.12 -m venv .venv
./.venv/bin/pip install -e "packages/engine[dev]" -e "apps/api[dev]" "psycopg[binary]"

export DATABASE_URL=postgresql://commonground:commonground@localhost:5434/commonground
./.venv/bin/python db/migrate.py
./.venv/bin/python scripts/seed_ci_catalogue.py   # synthetic, instant
./.venv/bin/python scripts/seed.py

./.venv/bin/uvicorn commonground_api.main:app --port 8010 &
npm install --prefix apps/web && npm run dev --prefix apps/web
```

That runs immediately on a generated catalogue. For the real one (6,217
MusicBrainz-tagged tracks, about forty minutes of it MusicBrainz's rate limit)
see [docs/development.md](docs/development.md), then
`scripts/build_catalog_from_dataset.py`.

## What's next

- **Broader catalogue.** The current catalogue is one day of ListenBrainz: 6,217
  tracks by 2,741 artists, all real, but one day reflects one day's release
  cycle. Tracks are already capped per artist so a single release cannot
  dominate. Widening it is a matter of ingesting more dumps, not changing code.
- **Group evaluation against real co-listening data.** Group metrics today rely
  on constructed groups and held-out accuracy, because no public dataset records
  which people actually shared a room. A real co-listening dataset would let the
  group layer be validated directly.

## Licence and data

The code is MIT (see [LICENSE](LICENSE)). The data is not all the same, and the
differences are load-bearing:

- MusicBrainz **core** data (artists, recordings, relationships) is **CC0**.
- MusicBrainz **tags, including genre associations**, are **CC BY-NC-SA 3.0**.
  Genre is this project's primary content feature, so CommonGround is
  non-commercial, credits MusicBrainz, and keeps every derivative of those tags
  out of this repository. `data/` is git-ignored, and CI generates a synthetic
  catalogue rather than committing a real one.
- ListenBrainz listens are **CC0**. MovieLens is used under GroupLens's research
  terms and is not redistributed here.

No upstream data ships in this repository, and no audio is hosted; every track
links out to legal external playback.

Music metadata from [MusicBrainz](https://musicbrainz.org) and listening data
from [ListenBrainz](https://listenbrainz.org), both by the MetaBrainz Foundation.
