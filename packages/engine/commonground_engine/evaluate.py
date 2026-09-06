"""The offline evaluation harness.

One function evaluates one model against one split, so every model in a
comparison is measured by identical code. The alternative -- a per-model
evaluation path -- is how a comparison ends up measuring the harness instead of
the models.

The protocol this implements is written down in docs/evaluation.md, which was
committed before any of these numbers existed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np

from . import metrics
from .dataset import Interactions
from .recommenders import Recommender
from .split import Split


@dataclass
class Result:
    model: str
    ks: list[int]
    scores: dict[str, float] = field(default_factory=dict)
    users_evaluated: int = 0
    fit_seconds: float = 0.0
    eval_seconds: float = 0.0

    def as_dict(self) -> dict:
        return {
            "model": self.model,
            "users_evaluated": self.users_evaluated,
            "fit_seconds": round(self.fit_seconds, 3),
            "eval_seconds": round(self.eval_seconds, 3),
            "metrics": {name: round(value, 6) for name, value in sorted(self.scores.items())},
        }


def evaluate(
    model: Recommender,
    split: Split,
    ks: tuple[int, ...] = (5, 10, 20),
    max_users: int | None = None,
    seed: int = 0,
) -> Result:
    """Fit on the training split and score the held-out items.

    Items the user interacted with in training are excluded from the
    recommendations. Not doing so lets a model score well by returning things
    the user has already played, which is trivially easy and useless.
    """
    started = time.perf_counter()
    model.fit(split.train)
    fit_seconds = time.perf_counter() - started

    users = split.evaluated_users
    if max_users is not None and len(users) > max_users:
        # Seeded sample so a fast run is reproducible rather than "the first N",
        # which correlates with user id and therefore with signup order.
        users = sorted(np.random.default_rng(seed).choice(users, max_users, replace=False).tolist())

    seen = split.train.seen_by_user()
    popularity = split.train.item_popularity()
    largest_k = max(ks)

    started = time.perf_counter()
    per_k: dict[int, dict[str, list[float]]] = {
        k: {"precision": [], "recall": [], "ndcg": []} for k in ks
    }
    recommendations: list[np.ndarray] = []

    for user in users:
        relevant = {int(item) for item in split.test[user]}
        ranked = model.recommend(user, largest_k, exclude=seen[user])
        recommendations.append(ranked)
        for k in ks:
            per_k[k]["precision"].append(metrics.precision_at_k(ranked, relevant, k))
            per_k[k]["recall"].append(metrics.recall_at_k(ranked, relevant, k))
            per_k[k]["ndcg"].append(metrics.ndcg_at_k(ranked, relevant, k))
    eval_seconds = time.perf_counter() - started

    scores: dict[str, float] = {}
    for k in ks:
        for name, values in per_k[k].items():
            scores[f"{name}@{k}"] = float(np.mean(values)) if values else 0.0

    scores["coverage@10"] = metrics.catalogue_coverage(recommendations, split.train.n_items, 10)
    scores["novelty@10"] = metrics.novelty(recommendations, popularity, 10)

    # How unevenly recommendations are spread across the catalogue. A model that
    # shows the same forty items to everyone scores near 1 here while looking
    # respectable on precision, which is exactly the failure coverage exists to
    # catch.
    exposure = np.zeros(split.train.n_items, dtype=np.float64)
    for ranked in recommendations:
        for item in ranked[:10]:
            exposure[int(item)] += 1
    scores["exposure_gini@10"] = metrics.gini(exposure)

    return Result(
        model=model.name,
        ks=list(ks),
        scores=scores,
        users_evaluated=len(users),
        fit_seconds=fit_seconds,
        eval_seconds=eval_seconds,
    )


def compare(
    models: dict[str, Recommender],
    split: Split,
    ks: tuple[int, ...] = (5, 10, 20),
    max_users: int | None = None,
    seed: int = 0,
) -> list[Result]:
    """Evaluate several models against the same split, in a stable order."""
    results = []
    for name, model in models.items():
        result = evaluate(model, split, ks=ks, max_users=max_users, seed=seed)
        result.model = name
        results.append(result)
    return results


def summary_table(results: list[Result], ks: tuple[int, ...] = (10,)) -> str:
    """A markdown table, so docs/measurements.md is generated, not typed."""
    columns = (
        [f"P@{k}" for k in ks]
        + [f"R@{k}" for k in ks]
        + [f"NDCG@{k}" for k in ks]
        + ["coverage@10", "novelty@10", "gini@10"]
    )
    keys = (
        [f"precision@{k}" for k in ks]
        + [f"recall@{k}" for k in ks]
        + [f"ndcg@{k}" for k in ks]
        + ["coverage@10", "novelty@10", "exposure_gini@10"]
    )

    lines = [
        "| model | " + " | ".join(columns) + " | fit (s) |",
        "| --- | " + " | ".join("---" for _ in columns) + " | --- |",
    ]
    for result in results:
        cells = [f"{result.scores.get(key, 0.0):.4f}" for key in keys]
        lines.append(f"| {result.model} | " + " | ".join(cells) + f" | {result.fit_seconds:.1f} |")
    return "\n".join(lines)


def item_features_from_artists(data: Interactions, artist_tags: dict[str, list[str]] | None = None):
    """Build a sparse item x feature matrix from artist identity and genres.

    Every item gets its artist as a feature; items whose artist has known genre
    tags get those too. Measured on the ListenBrainz slice: 100% of items have
    the artist feature and 33.7% additionally have genres, because the tagged
    catalogue covers 193 of the slice's 3,411 artists.

    Kept here rather than in the model so ContentKNN stays ignorant of music.
    """
    from scipy import sparse

    if data.item_artists is None:
        raise ValueError("the dataset carries no artist names")

    artist_tags = artist_tags or {}
    feature_index: dict[str, int] = {}
    rows, cols, values = [], [], []

    for item, artist in enumerate(data.item_artists):
        artist_feature = f"artist:{artist.casefold()}"
        index = feature_index.setdefault(artist_feature, len(feature_index))
        rows.append(item)
        cols.append(index)
        # The artist is weighted above any single genre: two tracks by the same
        # artist are more alike than two tracks sharing the tag "rock".
        values.append(2.0)

        for tag in artist_tags.get(artist.casefold(), []):
            tag_feature = f"tag:{tag}"
            index = feature_index.setdefault(tag_feature, len(feature_index))
            rows.append(item)
            cols.append(index)
            values.append(1.0)

    return sparse.csr_matrix(
        (values, (rows, cols)), shape=(data.n_items, len(feature_index)), dtype=np.float32
    )
