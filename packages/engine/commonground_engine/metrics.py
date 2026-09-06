"""Ranking metrics.

Pure functions over a ranked list of item ids and a set of relevant ones. No
model, no dataset, no state -- which is what makes them testable against hand-
worked examples rather than against whatever the recommender happened to output.

Accuracy metrics are only half of this. A recommender that returns the global
top 200 to everyone scores respectably on precision and is worthless in a
product; coverage and novelty are what expose that, so they are computed
alongside rather than as an afterthought.
"""

from __future__ import annotations

import numpy as np


def precision_at_k(ranked: np.ndarray, relevant: set[int], k: int) -> float:
    """Of what we recommended, how much did they actually play?"""
    if k <= 0:
        return 0.0
    top = ranked[:k]
    return sum(1 for item in top if int(item) in relevant) / k


def recall_at_k(ranked: np.ndarray, relevant: set[int], k: int) -> float:
    """Of what they played, how much did we surface?"""
    if not relevant:
        return 0.0
    top = ranked[:k]
    return sum(1 for item in top if int(item) in relevant) / len(relevant)


def dcg_at_k(ranked: np.ndarray, relevant: set[int], k: int) -> float:
    return sum(
        1.0 / np.log2(position + 2)
        for position, item in enumerate(ranked[:k])
        if int(item) in relevant
    )


def ndcg_at_k(ranked: np.ndarray, relevant: set[int], k: int) -> float:
    """DCG normalised by the best achievable ordering.

    The ideal DCG uses min(len(relevant), k) hits, so a user with two held-out
    items can still score 1.0 at k=10. Normalising by k instead would cap such a
    user at 0.2 and quietly turn NDCG into a measure of how much history each
    user happened to have.
    """
    if not relevant:
        return 0.0
    ideal = sum(1.0 / np.log2(position + 2) for position in range(min(len(relevant), k)))
    if ideal == 0:
        return 0.0
    return dcg_at_k(ranked, relevant, k) / ideal


def catalogue_coverage(recommendations: list[np.ndarray], n_items: int, k: int) -> float:
    """Share of the catalogue that appears in anyone's top-k."""
    if n_items <= 0:
        return 0.0
    seen: set[int] = set()
    for ranked in recommendations:
        seen.update(int(item) for item in ranked[:k])
    return len(seen) / n_items


def novelty(recommendations: list[np.ndarray], popularity: np.ndarray, k: int) -> float:
    """Mean self-information of recommended items: -log2(p(item)).

    Higher means less popular, so a recommender that only returns the global top
    40 scores near zero. Items nobody has played get the floor rather than
    infinity, which would otherwise make one cold item dominate the mean.
    """
    total = popularity.sum()
    if total <= 0:
        return 0.0
    probability = np.clip(popularity / total, 1e-12, None)
    information = -np.log2(probability)

    values = [information[int(item)] for ranked in recommendations for item in ranked[:k]]
    return float(np.mean(values)) if values else 0.0


def intra_list_diversity(recommendations: list[np.ndarray], similarity_of, k: int) -> float:
    """Mean pairwise dissimilarity within each recommendation list.

    `similarity_of(a, b)` returns a value in [0, 1]. Lists shorter than two
    items contribute nothing -- there is no pair to be diverse about, and
    scoring them 0 would punish a recommender for a short list.
    """
    scores: list[float] = []
    for ranked in recommendations:
        top = [int(item) for item in ranked[:k]]
        if len(top) < 2:
            continue
        pairs = [
            1.0 - similarity_of(top[i], top[j])
            for i in range(len(top))
            for j in range(i + 1, len(top))
        ]
        if pairs:
            scores.append(float(np.mean(pairs)))
    return float(np.mean(scores)) if scores else 0.0


def gini(values: np.ndarray) -> float:
    """Inequality of a non-negative distribution, 0 (equal) to 1 (concentrated).

    Used two ways: over item exposure, to show whether a recommender serves the
    same few items to everyone; and in M4 over per-member satisfaction, where
    variance is scale-dependent and Gini is not -- which is what makes fairness
    comparable across groups of different sizes.
    """
    values = np.asarray(values, dtype=np.float64)
    if values.size == 0:
        return 0.0
    if np.any(values < 0):
        raise ValueError("gini is undefined for negative values")
    total = values.sum()
    if total == 0:
        return 0.0
    ordered = np.sort(values)
    n = ordered.size
    index = np.arange(1, n + 1)
    return float((2 * (index * ordered).sum()) / (n * total) - (n + 1) / n)
