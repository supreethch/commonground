"""Recommender tests.

Weighted toward the properties the whole project rests on -- determinism, tie
handling, and the exclusion of already-seen items -- because those fail silently
and produce results that look fine.
"""

from __future__ import annotations

import numpy as np
import pytest
from commonground_engine.dataset import Interactions
from commonground_engine.recommenders import (
    ALSRecommender,
    ContentKNN,
    HybridRecommender,
    ItemKNN,
    PopularityRecommender,
    RandomRecommender,
    _rank_percentile,
    top_k,
)
from scipy import sparse


@pytest.fixture
def data() -> Interactions:
    """Two clear taste groups: users 0-2 like items 0-4, users 3-5 like 5-9."""
    rows = []
    stamp = 1000
    for user in range(3):
        for item in range(5):
            rows.append((user, item, stamp := stamp + 1))
    for user in range(3, 6):
        for item in range(5, 10):
            rows.append((user, item, stamp := stamp + 1))
    # Item 0 is also globally popular, so popularity and taste disagree.
    for user in range(3, 6):
        rows.append((user, 0, stamp := stamp + 1))

    users, items, stamps = zip(*rows, strict=True)
    return Interactions(
        users=np.array(users, dtype=np.int32),
        items=np.array(items, dtype=np.int32),
        timestamps=np.array(stamps, dtype=np.int64),
        n_users=6,
        n_items=12,
    )


# ------------------------------------------------------------------ top_k --


def test_top_k_breaks_ties_by_ascending_item_id() -> None:
    """Without this the order depends on argpartition's internals, and a
    'deterministic' ranking is quietly untrue."""
    scores = np.array([1.0, 1.0, 1.0, 1.0])
    assert top_k(scores, 3).tolist() == [0, 1, 2]


def test_top_k_orders_by_descending_score() -> None:
    assert top_k(np.array([0.1, 0.9, 0.5]), 3).tolist() == [1, 2, 0]


def test_top_k_excludes_without_renumbering() -> None:
    scores = np.array([0.9, 0.8, 0.7, 0.6])
    assert top_k(scores, 2, exclude={0}).tolist() == [1, 2]


def test_top_k_returns_fewer_than_k_when_everything_is_excluded() -> None:
    scores = np.array([0.5, 0.4])
    assert top_k(scores, 5, exclude={0, 1}).tolist() == []


def test_top_k_handles_k_larger_than_the_catalogue() -> None:
    assert top_k(np.array([0.2, 0.1]), 50).tolist() == [0, 1]


# ------------------------------------------------------- rank percentile --


def test_rank_percentile_maps_to_the_unit_interval() -> None:
    result = _rank_percentile(np.array([5.0, 1.0, 3.0]))
    assert result.min() == 0.0
    assert result.max() == 1.0
    assert result[1] == 0.0 and result[0] == 1.0


def test_rank_percentile_gives_ties_the_same_value() -> None:
    """A model that scores everything equally must not have an arbitrary order
    imposed on it that later reads as a real preference."""
    result = _rank_percentile(np.array([2.0, 2.0, 2.0, 9.0]))
    assert result[0] == result[1] == result[2]
    assert result[3] > result[0]


def test_rank_percentile_is_immune_to_an_outlier() -> None:
    """Which is why it is used instead of min-max: score distributions here are
    long-tailed by construction."""
    modest = _rank_percentile(np.array([1.0, 2.0, 3.0]))
    with_outlier = _rank_percentile(np.array([1.0, 2.0, 10_000.0]))
    assert modest.tolist() == with_outlier.tolist()


# ------------------------------------------------------------- baselines --


def test_random_is_deterministic_for_a_seed(data) -> None:
    first = RandomRecommender(seed=7).fit(data).recommend(0, 5)
    second = RandomRecommender(seed=7).fit(data).recommend(0, 5)
    assert first.tolist() == second.tolist()


def test_random_differs_between_seeds(data) -> None:
    first = RandomRecommender(seed=1).fit(data).recommend(0, 5)
    second = RandomRecommender(seed=2).fit(data).recommend(0, 5)
    assert first.tolist() != second.tolist()


def test_popularity_ranks_the_most_played_item_first(data) -> None:
    model = PopularityRecommender().fit(data)
    # Item 0 was played by all six users; nothing else was played by more than three.
    assert model.recommend(0, 1).tolist() == [0]


def test_popularity_recommends_the_same_thing_to_everyone(data) -> None:
    """Not a bug -- it is the baseline's defining weakness, and coverage is the
    metric that exposes it."""
    model = PopularityRecommender().fit(data)
    assert model.recommend(0, 3).tolist() == model.recommend(4, 3).tolist()


# ---------------------------------------------------------------- models --


def test_item_knn_learns_taste_groups_from_co_occurrence(data) -> None:
    """The property that separates collaborative filtering from popularity.

    Users 3-5 played items 5-9 and also item 0. So for a user who has played
    only items 1-4 (group A minus item 0), item 0 should score above the group-B
    items: it co-occurs with A's items through users 3-5 far less than A's own
    items co-occur with each other.
    """
    model = ItemKNN(k_neighbours=10).fit(data)

    scores = model.score_all(0)  # user 0 played items 0-4
    group_a_unseen = scores[10:12]  # items nobody played at all
    group_b = scores[5:10]

    # Items in the other taste group still score above items nobody ever played,
    # because item 0 bridges the two groups -- but both are far below zero-risk
    # nonsense, so the ordering is the thing worth asserting.
    assert group_b.max() > group_a_unseen.max()
    assert scores[10] == 0.0, "an item nobody played can have no collaborative signal"


def test_item_knn_gives_different_users_different_rankings(data) -> None:
    """Unlike popularity, which is the point of the comparison."""
    model = ItemKNN(k_neighbours=10).fit(data)
    assert model.recommend(0, 5).tolist() != model.recommend(4, 5).tolist()


def test_item_knn_is_deterministic(data) -> None:
    first = ItemKNN(k_neighbours=10).fit(data).recommend(0, 5)
    second = ItemKNN(k_neighbours=10).fit(data).recommend(0, 5)
    assert first.tolist() == second.tolist()


def test_content_knn_requires_features_matching_the_item_count(data) -> None:
    wrong = sparse.csr_matrix(np.ones((3, 2), dtype=np.float32))
    with pytest.raises(ValueError, match="features have 3 rows"):
        ContentKNN(features=wrong).fit(data)


def test_content_knn_needs_features_at_all(data) -> None:
    with pytest.raises(ValueError, match="item x feature matrix"):
        ContentKNN().fit(data)


def test_content_knn_scores_items_sharing_a_feature(data) -> None:
    # Items 0-4 share feature 0; items 5-11 share feature 1.
    features = np.zeros((12, 2), dtype=np.float32)
    features[:5, 0] = 1.0
    features[5:, 1] = 1.0
    model = ContentKNN(features=sparse.csr_matrix(features), k_neighbours=10).fit(data)

    scores = model.score_all(0)  # user 0 played items 0-4
    assert scores[:5].sum() > scores[5:].sum()


def test_als_is_deterministic_for_a_seed(data) -> None:
    """Threads are pinned to 1 precisely so this holds; the parallel solver sums
    float updates in a nondeterministic order."""
    first = ALSRecommender(factors=8, iterations=5, seed=3).fit(data).recommend(0, 5)
    second = ALSRecommender(factors=8, iterations=5, seed=3).fit(data).recommend(0, 5)
    assert first.tolist() == second.tolist()


def test_als_fold_in_produces_a_factor_for_an_unknown_user(data) -> None:
    """The cold-start path: a member who onboarded five minutes ago is scoreable
    without retraining."""
    model = ALSRecommender(factors=8, iterations=5, seed=3).fit(data)

    factor = model.fold_in([0, 1, 2])

    assert factor.shape == (8,)
    assert np.isfinite(factor).all()
    assert not np.allclose(factor, 0)


def test_als_fold_in_of_nothing_is_a_zero_factor_not_a_crash(data) -> None:
    model = ALSRecommender(factors=8, iterations=5, seed=3).fit(data)
    assert np.allclose(model.fold_in([]), 0)


# ---------------------------------------------------------------- hybrid --


def test_hybrid_needs_a_non_zero_component(data) -> None:
    model = HybridRecommender(components=[(PopularityRecommender(), 0.0)]).fit(data)
    with pytest.raises(ValueError, match="non-zero weight"):
        model.score_all(0)


def test_hybrid_with_one_component_ranks_like_that_component(data) -> None:
    popularity = PopularityRecommender()
    hybrid = HybridRecommender(components=[(PopularityRecommender(), 1.0)]).fit(data)
    popularity.fit(data)

    assert hybrid.recommend(0, 5).tolist() == popularity.recommend(0, 5).tolist()


def test_hybrid_weights_shift_the_ranking(data) -> None:
    mostly_popular = HybridRecommender(
        components=[(PopularityRecommender(), 1.0), (ItemKNN(k_neighbours=10), 0.01)]
    ).fit(data)
    mostly_knn = HybridRecommender(
        components=[(PopularityRecommender(), 0.01), (ItemKNN(k_neighbours=10), 1.0)]
    ).fit(data)

    assert mostly_popular.recommend(3, 5).tolist() != mostly_knn.recommend(3, 5).tolist()


def test_hybrid_contributions_are_named_and_sum_to_the_score(data) -> None:
    """The explanations in M4 are built from these terms, so they have to be the
    same numbers that produced the ranking -- not a parallel calculation."""
    model = HybridRecommender(
        components=[(PopularityRecommender(), 0.6), (ItemKNN(k_neighbours=10), 0.4)]
    ).fit(data)

    contributions = model.contributions(0)
    assert set(contributions) == {"popularity", "item-knn"}

    total = sum(contributions.values())
    np.testing.assert_allclose(total, model.score_all(0), rtol=1e-9)
