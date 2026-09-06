"""Fairness and satisfaction metrics for a group playlist.

Two families, and the difference between them is the whole reason this file is
separate from metrics.py:

**Held-out metrics** ask how many of each member's *actual* future plays the
playlist contains. Grounded in real behaviour. This is the honest headline,
because "the least-satisfied member" becomes a measured quantity rather than a
restatement of the model's own confidence.

**Proxy metrics** use s(u,i), the model's own predicted satisfaction. Smooth and
available for every track, but partly measures the model agreeing with itself,
so they are reported as secondary and labelled as such.

A group recommender that only reports the mean is hiding its failures: a
playlist can be excellent on average while one member sits through the whole
hour hearing nothing they like. Every function here exists to make that visible.
"""

from __future__ import annotations

import numpy as np

from .metrics import gini


def per_member_hits(playlist_items: np.ndarray, held_out: list[set[int]]) -> np.ndarray:
    """How many of each member's held-out items the playlist contains."""
    chosen = {int(item) for item in playlist_items}
    return np.array([len(chosen & member) for member in held_out], dtype=np.float64)


def per_member_hit_rate(playlist_items: np.ndarray, held_out: list[set[int]]) -> np.ndarray:
    """Hits divided by each member's own held-out count.

    Normalising per member matters: a member with twenty held-out tracks and a
    member with five are not comparable on raw hits, and comparing them anyway
    would make "least satisfied" mostly a measure of who listens most.
    """
    hits = per_member_hits(playlist_items, held_out)
    sizes = np.array([max(len(member), 1) for member in held_out], dtype=np.float64)
    return hits / sizes


def members_served(playlist_items: np.ndarray, held_out: list[set[int]]) -> float:
    """Fraction of members who got at least one of their held-out tracks.

    Exists because `held_min` is degenerate at this sparsity. With 20 tracks
    drawn from a 12,000-item catalogue and five held-out tracks per member, most
    members get zero hits, so the per-group *minimum* is 0.0 for nearly every
    group and every strategy -- a floor metric that cannot tell any two
    strategies apart is not measuring the floor.

    "How many members did we reach at all" is coarser but actually varies, and
    it is the same question the min was asking: is anyone being left out?
    """
    if not held_out:
        return 0.0
    hits = per_member_hits(playlist_items, held_out)
    return float(np.mean(hits > 0))


def mean_satisfaction(values: np.ndarray) -> float:
    return float(np.mean(values)) if len(values) else 0.0


def min_satisfaction(values: np.ndarray) -> float:
    """The worst-served member. The number an average is designed to hide."""
    return float(np.min(values)) if len(values) else 0.0


def satisfaction_variance(values: np.ndarray) -> float:
    return float(np.var(values)) if len(values) else 0.0


def satisfaction_gini(values: np.ndarray) -> float:
    """Inequality across members, 0 (everyone served equally) to 1.

    Gini rather than variance for the cross-group comparison: variance is
    scale-dependent, so a group whose scores are all small would look "fair"
    next to one whose scores are large, purely because of the scale. Gini is
    scale-invariant, which is what makes groups of different sizes and
    activity levels comparable.
    """
    values = np.asarray(values, dtype=np.float64)
    if values.size == 0:
        return 0.0
    # Hit rates are non-negative by construction; clip defensively so a proxy
    # score that dipped below zero cannot raise from deep inside an experiment.
    return gini(np.clip(values, 0.0, None))


def member_share(member_scores: np.ndarray) -> np.ndarray:
    """Fraction of tracks on which each member is the top scorer.

    Catches the failure the aggregate numbers cannot: one member quietly
    driving the whole playlist while everyone else's mean stays respectable.
    `member_scores` is (n_tracks, n_members).
    """
    if member_scores.size == 0:
        return np.zeros(0)
    winners = np.argmax(member_scores, axis=1)
    counts = np.bincount(winners, minlength=member_scores.shape[1])
    return counts / max(len(winners), 1)


def repetition(playlist_items: np.ndarray, item_artists: dict[int, str]) -> float:
    """Largest share of the playlist taken by any single artist.

    The failure users notice first, and long before they notice a worse NDCG.
    """
    if len(playlist_items) == 0:
        return 0.0
    artists = [item_artists.get(int(item)) for item in playlist_items]
    known = [a for a in artists if a is not None]
    if not known:
        return 0.0
    _, counts = np.unique(known, return_counts=True)
    return float(counts.max() / len(playlist_items))


def veto_violations(member_scores: np.ndarray, tau: float) -> float:
    """Share of tracks where at least one member scores below the threshold.

    Zero by construction for any mode that applies the veto, and reported
    anyway: it is the number that shows the baselines' cost. `average-score`
    has no veto, so this is where "one member hated a third of the playlist"
    becomes visible instead of being averaged away.
    """
    if member_scores.size == 0:
        return 0.0
    return float(np.mean(member_scores.min(axis=1) < tau))


def evaluate_playlist(
    playlist_items: np.ndarray,
    member_scores: np.ndarray,
    held_out: list[set[int]],
    *,
    item_artists: dict[int, str] | None = None,
    popularity: np.ndarray | None = None,
    tau: float = 0.35,
) -> dict[str, float]:
    """Every group metric for one playlist, in one place.

    `member_scores` is (n_tracks, n_members) of predicted satisfaction.
    """
    held_rates = per_member_hit_rate(playlist_items, held_out)
    proxy = member_scores.mean(axis=0) if member_scores.size else np.zeros(len(held_out))

    result = {
        # --- grounded in real held-out behaviour ---
        "held_mean": mean_satisfaction(held_rates),
        "held_min": min_satisfaction(held_rates),
        "held_served": members_served(playlist_items, held_out),
        "held_gini": satisfaction_gini(held_rates),
        "held_variance": satisfaction_variance(held_rates),
        "held_hits_total": float(per_member_hits(playlist_items, held_out).sum()),
        # --- the model's own proxy, secondary ---
        "proxy_mean": mean_satisfaction(proxy),
        "proxy_min": min_satisfaction(proxy),
        "proxy_gini": satisfaction_gini(proxy),
        "veto_violations": veto_violations(member_scores, tau),
        "tracks": float(len(playlist_items)),
    }

    if member_scores.size:
        result["max_member_share"] = float(member_share(member_scores).max())

    if item_artists is not None:
        result["max_artist_share"] = repetition(playlist_items, item_artists)

    if popularity is not None and len(playlist_items):
        total = popularity.sum()
        if total > 0:
            probability = np.clip(popularity / total, 1e-12, None)
            information = -np.log2(probability)
            result["novelty"] = float(np.mean([information[int(i)] for i in playlist_items]))
    return result
