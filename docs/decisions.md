# Decisions

Short records of choices that were not obvious, and what each one costs. Written
when the decision is made, not reconstructed afterwards.

## 1. React + Vite, not Next.js

**Chose** the same frontend stack as a2transit and pulse.

Next.js would add a framework to the résumé that neither existing project has.
But a Next app in front of a separate Python API means two deploy targets and a
server-rendering layer that renders almost nothing — the interesting screens are
a live room and a playlist, both client-state-heavy and both behind auth. The
engineering story here is the recommender and the real-time layer; spending
milestone time on framework novelty would dilute it.

**Cost:** no SSR, and no Next.js on the résumé from this project.

## 2. The engine is a separate package, enforced by a test

**Chose** `packages/engine` with no FastAPI, SQLAlchemy, Redis or HTTP imports,
and `test_engine_isolation.py` walking the AST to keep it that way.

Offline evaluation over hundreds of thousands of interactions must not need a
database or a web server, and determinism and fairness properties are far easier
to assert against arrays than through an HTTP client. Architectural boundaries
that nothing enforces decay quickly — the first time a config value is needed in
a hurry, the fastest fix is the import that welds the layers together.

**Cost:** some data has to be marshalled across the boundary rather than queried
where it is needed.

## 3. `implicit` for ALS, not TensorFlow or PyTorch

**Chose** NumPy, pandas, scikit-learn and `implicit`.

TensorFlow came up as an option. It is the wrong tool here: this is implicit-
feedback matrix factorisation on a laptop-sized catalogue, which is a
least-squares problem with a well-understood closed-form update, not something
that needs autograd. Adding a deep-learning framework would mean a much larger
image, slower CI, and a harder time keeping the deployment free — in exchange for
no measurable gain on this data.

The interesting engineering in this project is the **group** layer: aggregation,
fairness, veto handling, explanation. Spending milestone 3 on a neural
architecture would spend it in the place where this project is *least* novel.

Verified during M1 that `implicit` 0.7.3 installs cleanly on Apple Silicon under
Python 3.12, since a source-only build would have been a real cost.

**Cost:** no deep-learning line on this project. If it is ever wanted, a
two-tower model slots in as one more scorer behind the same interface, and the
evaluation harness would compare it honestly.

## 4. Genre features come from CC BY-NC-SA data, and `data/` is git-ignored

**Chose** MusicBrainz tags as the content feature, accepting the non-commercial
ShareAlike licence, and keeping everything derived from them out of git.

MusicBrainz core data is CC0, but tags — including genre associations — are
supplementary and CC BY-NC-SA 3.0. Genre is the primary content feature, so the
project inherits non-commercial and ShareAlike terms on anything derived from it.
Committing a derived artist–genre matrix would place a ShareAlike obligation on a
directory inside an MIT repository, which is exactly the kind of quiet licence
conflict that makes a repository unusable by others.

**Cost:** a fresh clone cannot run until `build_catalog.py` has been run.

## 5. Votes are written over REST and broadcast over the socket

**Chose** not to accept state changes as WebSocket messages.

A dropped socket then costs a user their live updates but never their vote, and
validation, authorisation and rate limiting live in one place instead of two.

**Cost:** a vote is two round trips rather than one. At the scale of a room of
friends this is invisible.

## 6. No keepalive, and the demo cold-starts

**Chose** to ship no keepalive workflow and say so in the README.

Render allows 750 free instance-hours per month per workspace. a2transit and
pulse are already there, and pulse pings itself every ten minutes to stay awake.
One always-awake service costs about 730 hours a month; three would need about
2,190. A keepalive here would not make this demo fast, it would exhaust the
shared budget and suspend all three.

**Cost:** a wait on the first load after an idle period. The README says so, as
both other projects already do. No duration is quoted: see docs/deployment.md for
what was actually observed.

## 7. Rank-percentile normalisation before any group aggregation

**Chose** to convert each member's raw scores to a within-member percentile
before combining them.

Members' raw score distributions differ with how much history they have. Compared
directly, the member with the sparsest profile would be permanently "least
satisfied" and every fairness mechanism would fire on their behalf regardless of
what was playing — the fairness features would be responding to a scale artefact
rather than to unfairness.

**Cost:** absolute score magnitude is discarded. A group where genuinely nothing
fits anyone looks the same as one where everything does, so *absolute* fit is
tracked separately for the UI rather than inferred from `s(u,i)`.

## 8. Raw SQL migrations, not Alembic

**Chose** numbered `.sql` files with a small runner that checksums what it
applied.

The schema is the part of this project a reader is most likely to want to
understand quickly, and a `.sql` file is legible to anyone. The checksum catches
an applied migration being edited, which otherwise surfaces as an unexplained
difference between a developer's database and production's. Same approach pulse
takes.

**Cost:** no autogeneration from models, and no down-migrations.
