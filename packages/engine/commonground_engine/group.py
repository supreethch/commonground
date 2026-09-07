"""Group ranking: turn per-member scores into one playlist, with reasons.

This is the part of the project that is actually novel. Single-user
recommendation asks "what will this person like?"; group recommendation asks
"whose preference loses, and by how much?" -- and an average hides that by
construction.

Four stages, matching docs/recommender.md:

    per-member scores  ──▶  aggregate  ──▶  veto  ──▶  sequential selection
      s(u,i) ∈ [0,1]          G(i)          hard        K tracks + reasons

Everything here is deterministic. Given the same scores, the same mode and the
same seed, the playlist is identical down to the tie-breaks -- which is what
makes the explanations checkable, since a ranking that cannot be reproduced
cannot have its reasons verified.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

import numpy as np


@dataclass(frozen=True)
class ModeConfig:
    """A mode is a parameter set, not a code path.

    One ranker, three configurations, so the evaluation compares the modes on
    identical machinery and a new mode is a row in a table rather than a branch.
    """

    name: str
    # Trades average happiness against protecting the worst-off member.
    # alpha=1 is exactly the "average score" baseline; alpha=0 is least misery.
    alpha: float = 0.5
    # Below this, a member is treated as objecting. Checked as a hard filter,
    # never as a penalty -- see `apply_veto`.
    tau_veto: float = 0.35
    # Redundancy against what is already in the playlist (MMR-style).
    lambda_div: float = 0.15
    # Same artist appearing again inside a window.
    lambda_rep: float = 0.25
    # Rewards items unfamiliar to *everyone*.
    lambda_nov: float = 0.0
    # Picks for whoever the playlist has served least so far.
    lambda_fair: float = 0.0
    # How many recent slots the repetition penalty looks back over.
    repetition_window: int = 5

    def with_params(self, **overrides) -> ModeConfig:
        return replace(self, **overrides)


# The **fitted** parameters, from scripts/sweep_modes.py on validation groups
# nested inside the training split. These are the values docs/measurements.md
# reports, and therefore the values the running app must use: a product whose
# defaults differ from the ones its own evaluation measured is reporting numbers
# about a system nobody is running.
#
# The design intentions these started from are in docs/recommender.md, alongside
# what the fit changed and the one place the fit was overridden.
CONSENSUS = ModeConfig(
    name="consensus", alpha=1.0, tau_veto=0.35, lambda_div=0.15, lambda_fair=0.45
)
DISCOVERY = ModeConfig(
    name="discovery",
    alpha=0.7,
    tau_veto=0.25,
    lambda_div=0.30,
    lambda_nov=0.35,
    lambda_fair=0.45,
)
FAIR_ROTATION = ModeConfig(
    name="fair_rotation",
    alpha=0.7,
    tau_veto=0.35,
    lambda_div=0.15,
    lambda_nov=0.0,
    lambda_fair=0.45,
)

MODES = {mode.name: mode for mode in (CONSENSUS, DISCOVERY, FAIR_ROTATION)}


# ------------------------------------------------------------- aggregation --


def rank_percentile(scores: np.ndarray) -> np.ndarray:
    """Map each row of scores to [0, 1] by rank, ties sharing the average.

    Load-bearing, and not a cosmetic normalisation. Members' raw score
    distributions differ with how much history they have: someone with a dense
    profile gets confident, well-spread scores, someone who picked four artists
    at onboarding gets everything bunched near the middle. Compared directly,
    the sparse member is permanently "least satisfied" and every fairness
    mechanism fires on their behalf regardless of what is playing -- responding
    to a scale artefact rather than to unfairness.

    After this, s(u,i) means the same thing for everyone: where this track sits
    among the things we could have played *you*.
    """
    scores = np.asarray(scores, dtype=np.float64)
    if scores.ndim == 1:
        scores = scores[None, :]

    n = scores.shape[1]
    if n == 0:
        return scores
    if n == 1:
        return np.ones_like(scores)

    out = np.empty_like(scores)
    for row in range(scores.shape[0]):
        values = scores[row]
        order = np.argsort(values, kind="stable")
        ranks = np.empty(n, dtype=np.float64)
        ranks[order] = np.arange(n, dtype=np.float64)

        unique, inverse, counts = np.unique(values, return_inverse=True, return_counts=True)
        if len(unique) < n:
            sums = np.zeros(len(unique), dtype=np.float64)
            np.add.at(sums, inverse, ranks)
            ranks = (sums / counts)[inverse]

        out[row] = ranks / (n - 1)
    return out


@dataclass(frozen=True)
class GroupScores:
    """Per-member satisfaction for a candidate set, already normalised.

    `matrix` is (n_members, n_candidates) with values in [0, 1].
    """

    matrix: np.ndarray
    candidate_ids: np.ndarray
    member_ids: list[int] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.matrix.ndim != 2:
            raise ValueError("scores must be (members x candidates)")
        if self.matrix.shape[1] != len(self.candidate_ids):
            raise ValueError(
                f"{self.matrix.shape[1]} score columns but {len(self.candidate_ids)} candidates"
            )

    @property
    def n_members(self) -> int:
        return self.matrix.shape[0]

    @property
    def n_candidates(self) -> int:
        return self.matrix.shape[1]

    def mean(self) -> np.ndarray:
        return self.matrix.mean(axis=0)

    def minimum(self) -> np.ndarray:
        """The least-satisfied member's score. The number this project is for."""
        return self.matrix.min(axis=0)

    def variance(self) -> np.ndarray:
        return self.matrix.var(axis=0)


def aggregate(scores: GroupScores, alpha: float) -> np.ndarray:
    """G(i) = alpha*mean + (1-alpha)*min."""
    if not 0.0 <= alpha <= 1.0:
        raise ValueError(f"alpha must be in [0, 1], got {alpha}")
    return alpha * scores.mean() + (1.0 - alpha) * scores.minimum()


def apply_veto(
    scores: GroupScores,
    tau: float,
    disliked: np.ndarray | None = None,
) -> np.ndarray:
    """Boolean mask of candidates that survive.

    A candidate is dropped outright if any member scores below `tau`, or if any
    member has explicitly disliked it.

    Deliberately a filter and not a penalty term. Folded into the weighted sum,
    a large enough mean could always buy its way past one member's strong
    objection -- which is exactly the failure the veto exists to prevent. Making
    it a hard mask means no combination of other terms can override it.
    """
    survives = scores.minimum() >= tau
    if disliked is not None:
        if disliked.shape != scores.matrix.shape:
            raise ValueError("disliked mask must match the score matrix shape")
        survives &= ~disliked.any(axis=0)
    return survives


# --------------------------------------------------------------- selection --


@dataclass
class SelectedTrack:
    candidate_id: int
    position: int
    group_score: float
    member_scores: np.ndarray
    # Every named term that moved the score, and by how much. The explanation
    # layer renders these; it never recomputes anything, which is what stops a
    # reason describing something that did not actually happen.
    contributions: dict[str, float]
    # Which member index the fairness bonus was computed for, when it applied.
    #
    # Recorded rather than re-derived because the two are not the same member.
    # The bonus serves whoever has the lowest *cumulative* satisfaction across
    # the playlist so far; the lowest scorer *on this track* is often someone
    # else. An explanation that named the latter would be describing a
    # different quantity than the one that moved the score.
    served_member: int | None = None


@dataclass
class GroupPlaylist:
    tracks: list[SelectedTrack]
    mode: str
    seed: int
    n_candidates: int
    n_vetoed: int

    @property
    def item_ids(self) -> np.ndarray:
        return np.array([t.candidate_id for t in self.tracks], dtype=np.int64)

    def member_satisfaction(self, n_members: int) -> np.ndarray:
        """Mean s(u,i) over the playlist, per member."""
        if not self.tracks:
            return np.zeros(n_members)
        return np.mean([t.member_scores for t in self.tracks], axis=0)


def select(
    scores: GroupScores,
    mode: ModeConfig,
    k: int,
    *,
    item_similarity=None,
    item_artists: dict[int, str] | None = None,
    novelty: np.ndarray | None = None,
    disliked: np.ndarray | None = None,
    seed: int = 0,
) -> GroupPlaylist:
    """Choose k tracks one slot at a time.

    Sequential rather than top-k, because the value of a track depends on what
    is already in the playlist: the second song by an artist is worth less than
    the first, and a track close to one already chosen adds nothing.

    `item_similarity(a, b) -> [0, 1]` is optional; without it the redundancy
    term is zero and only the artist-repetition penalty applies.
    """
    if k < 0:
        raise ValueError("k must not be negative")

    base = aggregate(scores, mode.alpha)
    survives = apply_veto(scores, mode.tau_veto, disliked)
    n_vetoed = int((~survives).sum())

    available = list(np.flatnonzero(survives))
    chosen: list[SelectedTrack] = []
    # Cumulative satisfaction per member, which is what fair rotation serves.
    cumulative = np.zeros(scores.n_members, dtype=np.float64)

    rng = np.random.default_rng(seed)
    del rng  # Reserved: no stochastic step today, but the seed is recorded.

    while available and len(chosen) < k:
        best_index: int | None = None
        best_total = -np.inf
        best_terms: dict[str, float] = {}

        # The member the playlist has served least so far. Fair rotation means
        # continuously picking for them, not alternating members by turn.
        neediest = int(np.argmin(cumulative)) if scores.n_members else 0

        for column in available:
            terms: dict[str, float] = {"group_score": float(base[column])}

            if mode.lambda_div and item_similarity is not None and chosen:
                redundancy = max(
                    item_similarity(int(scores.candidate_ids[column]), t.candidate_id)
                    for t in chosen
                )
                terms["diversity_penalty"] = -mode.lambda_div * float(redundancy)

            if mode.lambda_rep and item_artists:
                artist = item_artists.get(int(scores.candidate_ids[column]))
                if artist is not None:
                    window = chosen[-mode.repetition_window :]
                    repeats = sum(1 for t in window if item_artists.get(t.candidate_id) == artist)
                    if repeats:
                        terms["repetition_penalty"] = -mode.lambda_rep * repeats

            if mode.lambda_nov and novelty is not None:
                terms["novelty_bonus"] = mode.lambda_nov * float(novelty[column])

            if mode.lambda_fair and scores.n_members:
                terms["fairness_bonus"] = mode.lambda_fair * float(scores.matrix[neediest, column])

            total = sum(terms.values())
            # Strictly greater, and candidates are walked in ascending column
            # order, so an exact tie keeps the lower candidate id. Without this
            # the winner depends on dict iteration and float noise, and the
            # ranking stops being reproducible.
            if total > best_total:
                best_total = total
                best_index = column
                best_terms = terms

        if best_index is None:
            break

        member_scores = scores.matrix[:, best_index].copy()
        chosen.append(
            SelectedTrack(
                candidate_id=int(scores.candidate_ids[best_index]),
                position=len(chosen),
                group_score=float(best_total),
                member_scores=member_scores,
                contributions=best_terms,
                served_member=neediest if "fairness_bonus" in best_terms else None,
            )
        )
        cumulative += member_scores
        available.remove(best_index)

    return GroupPlaylist(
        tracks=chosen,
        mode=mode.name,
        seed=seed,
        n_candidates=scores.n_candidates,
        n_vetoed=n_vetoed,
    )


# -------------------------------------------------------------- baselines --


def select_average_score(scores: GroupScores, k: int) -> GroupPlaylist:
    """The naive group method: average everyone, no veto, no penalties.

    This is the baseline CommonGround claims to improve on, expressed in the
    same machinery (alpha=1 and every lambda zero) rather than as separate code,
    so the comparison cannot be an artefact of two different implementations.
    """
    return select(
        scores,
        ModeConfig(name="average-score", alpha=1.0, tau_veto=0.0, lambda_div=0.0, lambda_rep=0.0),
        k,
    )


def select_least_misery(scores: GroupScores, k: int) -> GroupPlaylist:
    """Rank purely by the worst-off member. The standard fairness baseline."""
    return select(
        scores,
        ModeConfig(name="least-misery", alpha=0.0, tau_veto=0.0, lambda_div=0.0, lambda_rep=0.0),
        k,
    )


def select_popularity(scores: GroupScores, popularity: np.ndarray, k: int) -> GroupPlaylist:
    """Ignore the group entirely and play the hits.

    Genuinely hard to beat on mean satisfaction, which is why it is here.
    """
    order = np.lexsort((scores.candidate_ids, -popularity))[:k]
    tracks = [
        SelectedTrack(
            candidate_id=int(scores.candidate_ids[column]),
            position=position,
            group_score=float(popularity[column]),
            member_scores=scores.matrix[:, column].copy(),
            contributions={"popularity": float(popularity[column])},
        )
        for position, column in enumerate(order)
    ]
    return GroupPlaylist(
        tracks=tracks,
        mode="popularity",
        seed=0,
        n_candidates=scores.n_candidates,
        n_vetoed=0,
    )
