# Evaluation plan

Nothing in this file is a result. It is the protocol, written before any numbers
exist, so the metrics and baselines cannot be chosen afterwards to flatter
whatever the engine happens to do. Results land in `eval/results/` and are
summarised in `docs/measurements.md`, which is **generated from those files**
rather than typed — a documented number and a measured number that can drift
apart eventually will.

## Two evaluations, because there are two claims

**M3 — individual recommendation.** Does the hybrid model predict what a single
user listens to better than the obvious alternatives? Standard, well-understood,
and mostly there to establish that the per-member satisfaction scores the group
layer consumes are worth anything.

**M4 — group recommendation.** This is the real claim, and it is not "higher
accuracy". It is that group ranking produces **a better floor and a fairer
distribution at an acceptable cost to the mean**. That is a trade, so it must be
reported as a trade, including the cases where it loses.

## Splitting

**Time-ordered, leave-last-N-out per user.** The most recent N interactions of
each user with enough history are held out; everything earlier is training.

Not a random split. A random split lets the model train on a user's future to
predict their past, and for listening data — where taste drifts and an album
release puts ten plays of one artist in one week — that leaks badly enough to
make results meaningless. Random splits are common in recommender papers and
they are why reported numbers so often fail to reproduce in production.

Users below a minimum-interaction threshold are excluded from *evaluation* and
kept in *training*. Evaluating on a user with four listens measures noise.

## Individual metrics (M3)

For K ∈ {5, 10, 20}:

| Metric | What it answers |
| --- | --- |
| Precision@K | Of what we recommended, how much did they actually play? |
| Recall@K | Of what they played, how much did we surface? |
| NDCG@K | Same, but rewarding correct items ranked higher |
| Catalogue coverage | What fraction of the catalogue ever gets recommended to anyone? |
| Novelty | Mean negative log popularity of recommended items |
| Intra-list diversity | Mean pairwise distance inside one recommendation list |

The last three are not decoration. A recommender that only ever returns the
global top 200 scores respectably on precision and is worthless in a product,
and coverage is what exposes that.

## Group metrics (M4)

Given a group `U` and a generated playlist `P`:

| Metric | Definition | Why it matters |
| --- | --- | --- |
| Mean satisfaction | `mean_{u∈U} mean_{i∈P} s(u,i)` | The usual headline |
| **Min-member satisfaction** | `min_{u∈U} mean_{i∈P} s(u,i)` | The worst-served member — the number this project exists for |
| Satisfaction variance | variance across members | Spread |
| **Gini across members** | Gini coefficient of per-member satisfaction | Variance is scale-dependent; Gini compares across groups of different sizes |
| Share of playlist | fraction of tracks where each member is the top scorer | Whether one member is quietly driving everything |
| Novelty / diversity | as above | Whether Discovery mode does anything |
| Repetition | max tracks per artist, per genre | The failure mode users notice first |

## Group construction

Real groups do not exist in the data, so they are constructed from real
ListenBrainz users under a stated rule, and the rule matters more than the
sample size:

- **Homogeneous** — members sampled to have high pairwise taste similarity. The
  easy case; everything works here, so it mostly checks for bugs.
- **Mixed** — members sampled at moderate similarity. The realistic case.
- **Adversarial** — at least two members with near-disjoint taste. The case
  where averaging visibly fails and the whole design should earn its keep.
- **Cold-start-heavy** — a majority of members with onboarding-only profiles,
  because that is what a real demo room looks like.

Group sizes 2–8. Sampling is seeded and the seed is recorded, so a group set can
be rebuilt exactly.

## Baselines

1. **Random** — the floor.
2. **Popularity-only** — global play count. Genuinely hard to beat on mean
   satisfaction, which is the point of including it.
3. **Average score** — per-member scores averaged, no veto, no penalties. The
   naive group method this project claims to improve on.
4. **Least misery** — rank by `min_u s(u,i)`. The standard fairness-first
   comparison, and the one CommonGround must beat on *mean* to justify existing.

Each mode is evaluated against all four. **Expect Consensus to lose to average
score on mean satisfaction.** If it does, that is reported as the cost of the
floor it buys, not quietly omitted.

## Reproducibility

```
eval/configs/*.yaml     one file per experiment: dataset slice, split, seeds, params
eval/results/*.json     one file per run: metrics, config hash, git SHA, timings
```

`scripts/run_eval.py --config eval/configs/<name>.yaml` is the only way results
are produced. Every result records the git SHA and the config hash that made it,
so a number in the README can be traced to a commit and re-run. A run whose
config hash does not match its config file is treated as stale.

## What this evaluation cannot show

Stated now rather than discovered by a reader later:

- **Offline satisfaction is a proxy.** `s(u,i)` is the model's own prediction, so
  a group metric built on it partly measures the model agreeing with itself.
  Held-out interactions ground it where they exist, but for a hypothetical group
  there is no ground truth about how five people felt about a playlist. The
  honest framing is *relative* comparison under one consistent proxy, not an
  absolute claim about human satisfaction.
- **Constructed groups are not real groups.** Friends share taste for reasons
  the sampler cannot see.
- **No online evaluation.** No A/B test, no real users. Anything from the live
  demo is anecdote and will be labelled as such.

This is the same discipline pulse applies by excluding the model's own
predictions from its label set: an evaluation whose weaknesses are hidden is
worth less than one whose weaknesses are written down.
