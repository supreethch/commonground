# The recommendation engine

This is a real recommender, not a prompt. No language model is involved in
producing or explaining a playlist. Every number below is arithmetic this
repository performs, which is what makes the explanations checkable.

Parameter values in this document are **starting points, not results**. The
fitted values land in `eval/results/` in M3–M4 and are reported in
`docs/measurements.md`. Nothing here is a claim about quality yet.

## Four stages

```
candidates  ──▶  per-member scoring  ──▶  group aggregation  ──▶  sequential selection
  ~2k items        s(u,i) ∈ [0,1]            G(i)                   K tracks + reasons
```

## 1. Candidate generation

Scoring the whole catalogue for every room is wasteful and unnecessary — almost
all of it scores near zero for everyone. For a room of `n` members we assemble a
few thousand candidates:

- **Per-member content neighbours.** Top-N items by cosine similarity to each
  member's taste vector.
- **Per-member collaborative neighbours.** Top-N items by ALS score for each
  member.
- **A popularity pool.** A fixed slice of broadly-liked items, which is what
  keeps a room of five cold-start users from getting an empty candidate set.
- **Bridge items.** Items scoring moderately for *several* members rather than
  highly for one. These are the interesting ones in a group setting and they
  fall out of a union of top-N lists only by luck, so they are sought explicitly.

Then remove: anything already in this room's playlist history, anything by an
artist any member has explicitly disliked, and anything failing the room's
content settings.

## 2. Per-member scoring

For member `u` and candidate `i`, five normalised components:

| Component | What it is |
| --- | --- |
| `cf(u,i)` | ALS implicit-feedback score from the user–item matrix |
| `content(u,i)` | cosine between `u`'s taste vector and `i`'s feature vector (TF-IDF over genres and tags, plus artist affinity) |
| `pop(i)` | log-scaled global play count |
| `rec(i)` | release recency, mild |
| `fb(u,i)` | explicit signal: likes, dislikes, votes and skips on the item, its artist, and its genres |

```
raw(u,i) = w_cf·cf + w_ct·content + w_pop·pop + w_rec·rec + w_fb·fb
```

Weights are fitted by grid search on a validation split in M3 and written to the
experiment record — not hand-tuned until the demo looks good.

### Rank-percentile normalisation, and why it is load-bearing

```
s(u,i) = percentile_rank of raw(u,i) among all candidates, for this member
```

Members' raw scores live on different scales. Someone with a dense listening
history gets confident, well-spread scores; someone who picked four artists at
onboarding gets scores bunched near the middle. If those raw values were
compared directly, **the sparse member would be permanently "least satisfied"**
and every fairness mechanism in the system would fire on their behalf regardless
of what was actually playing.

Converting to a within-member percentile makes `s(u,i)` mean the same thing for
everyone: *where this track sits among the things we could have played you*.
Fairness comparisons across members are only meaningful after this step, so it
happens before any aggregation.

## 3. Group aggregation

For candidate `i` across `n` members:

```
mean(i) = (1/n) Σ_u s(u,i)
min(i)  = min_u s(u,i)                    least-satisfied member
var(i)  = variance of s(·,i)              disagreement

G(i)    = α·mean(i) + (1−α)·min(i)
```

`α` trades average happiness against protecting the worst-off member and is the
main knob distinguishing the modes. `α = 1` is exactly the "average score"
baseline; `α = 0` is classic least-misery.

**The veto is separate and hard.** A candidate is dropped outright if any member
has explicitly disliked its artist, or if `s(u,i) < τ_veto` for any `u`. This is
not folded into the weighted sum on purpose: a large enough mean must not be able
to buy its way past one member's strong objection, and a penalty term always can.

## 4. Sequential selection

Tracks are chosen one slot at a time, because the value of a track depends on
what is already in the playlist. With `S` the tracks chosen so far:

```
score_t(i) = G(i)
           − λ_div · maxsim(i, S)          redundancy against what is already there
           − λ_rep · rep(i, S)             same artist or genre inside a window
           + λ_nov · nov(i)                novelty  (Discovery)
           + λ_fair · s(u*, i)             u* = member with the lowest cumulative
                                           satisfaction so far  (Fair Rotation)
```

`nov(i)` combines low global popularity with unfamiliarity to *every* member —
a track one member already knows is not a discovery for the room.

`u*` is the member with the smallest `Σ_{j∈S} s(u,j)`. Picking for them at each
slot is what "fair rotation" means concretely: not alternating members by turn,
but continuously serving whoever the playlist has served least so far.

### The three modes are parameter sets, not code paths

| | `α` | `τ_veto` | `λ_div` | `λ_nov` | `λ_fair` |
| --- | --- | --- | --- | --- | --- |
| **Consensus** | 0.5 | 0.35 | med | 0 | 0 |
| **Discovery** | 0.7 | 0.25 | high | high | 0 |
| **Fair Rotation** | 0.3 | 0.35 | med | low | high |

One ranker, three configurations. A new mode is a row in a table and an entry in
the evaluation, not a new branch — which also means the evaluation compares the
modes on identical machinery.

## Explanations come from the arithmetic

Because `score_t(i)` is a sum of named terms, the engine returns the
contribution of each term alongside the total. An explanation is the top
contributions rendered through templates, joined with group facts computed
exactly — how many members have that genre in their profile, whose taste vector
the item is closest to, whether the artist is new to everyone, whether anyone
was near their veto threshold.

> "Recommended because 3 members like indie rock, it's similar to Alex's
> favourites, and it introduces a new artist without strongly conflicting with
> anyone's dislikes."

Each clause maps to a term: the genre count is a group fact, "similar to Alex's"
is the member with the highest `content(u,i)`, "new artist" is `nov(i)`, and
"without strongly conflicting" is the veto having been checked and passed. The
sentence is generated *from* the ranking, so it cannot describe a reason that did
not actually move the score. A test asserts that every clause in a rendered
explanation corresponds to a non-zero contribution.

## Cold start

A member who onboarded with a handful of artists has no row in the ALS matrix.
Rather than retraining, their user factor is **folded in**: a least-squares solve
against the fixed item factors for the items they do have. Cheap, no retraining,
and it degrades gracefully — a member with nothing at all scores content-only
against genre priors, and the UI says so instead of implying a prediction.

## Baselines to beat

Named now so the comparison cannot be chosen retrospectively to flatter the
result:

1. **Random** — sanity floor.
2. **Popularity-only** — rank by global play count. Beats more group recommenders
   than anyone likes to admit.
3. **Average score** (`α = 1`, no veto, no penalties) — the naive group approach,
   and the one this project claims to improve on.
4. **Least misery** (`α = 0`) — the standard fairness-first baseline.

The claim CommonGround has to earn is not "highest mean satisfaction" — average
score will often win that. It is **a better floor and a fairer distribution at an
acceptable cost to the mean**, and that trade has to be shown as a number, in
both directions, including where it loses.

## Complexity

With `|C| ≈ 2000` candidates, `n ≤ 8` members, `K ≈ 30` tracks: scoring is
`O(|C|·n·d)` over dense feature vectors, selection is `O(K·|C|·|S|)` dominated by
the redundancy term. Both are small. Whether the *end-to-end* room request is
fast enough to stay synchronous depends on loading and matrix assembly, not this
arithmetic, and is measured in M3 rather than assumed.
