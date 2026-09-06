"""Metric tests against hand-worked examples.

Every expected value here is computed by hand in the comment beside it. A metric
test that calls the implementation to produce its own expectation proves only
that the code is consistent with itself.
"""

from __future__ import annotations

import numpy as np
import pytest
from commonground_engine import metrics


def test_precision_counts_hits_over_k() -> None:
    ranked = np.array([1, 2, 3, 4, 5])
    # items 2 and 4 are relevant and inside the top 5 -> 2/5
    assert metrics.precision_at_k(ranked, {2, 4}, 5) == pytest.approx(0.4)
    # only item 2 is inside the top 2 -> 1/2
    assert metrics.precision_at_k(ranked, {2, 4}, 2) == pytest.approx(0.5)


def test_precision_divides_by_k_even_when_the_list_is_short() -> None:
    """Otherwise a model returning three items scores as if it returned ten."""
    ranked = np.array([1, 2, 3])
    assert metrics.precision_at_k(ranked, {1}, 10) == pytest.approx(0.1)


def test_recall_divides_by_the_relevant_set() -> None:
    ranked = np.array([1, 2, 3, 4, 5])
    # 2 of the 4 relevant items were surfaced
    assert metrics.recall_at_k(ranked, {2, 4, 8, 9}, 5) == pytest.approx(0.5)


def test_recall_of_an_empty_relevant_set_is_zero_not_an_error() -> None:
    assert metrics.recall_at_k(np.array([1, 2]), set(), 5) == 0.0


def test_dcg_weights_earlier_positions_more() -> None:
    # position 0 -> 1/log2(2) = 1.0 ; position 1 -> 1/log2(3) = 0.6309
    assert metrics.dcg_at_k(np.array([7, 8]), {7}, 2) == pytest.approx(1.0)
    assert metrics.dcg_at_k(np.array([8, 7]), {7}, 2) == pytest.approx(0.63093, abs=1e-5)


def test_ndcg_is_one_when_every_relevant_item_is_ranked_first() -> None:
    ranked = np.array([1, 2, 3, 4])
    assert metrics.ndcg_at_k(ranked, {1, 2}, 4) == pytest.approx(1.0)


def test_ndcg_normalises_by_the_achievable_ideal_not_by_k() -> None:
    """A user with two held-out items must be able to score 1.0 at k=10.

    Normalising by k would cap them at 0.2 and turn NDCG into a measure of how
    much history each user happened to have.
    """
    ranked = np.arange(10)
    assert metrics.ndcg_at_k(ranked, {0, 1}, 10) == pytest.approx(1.0)


def test_ndcg_is_between_zero_and_one_for_a_partial_hit() -> None:
    ranked = np.array([9, 8, 1, 7, 6])
    value = metrics.ndcg_at_k(ranked, {1, 2}, 5)
    assert 0.0 < value < 1.0


def test_coverage_counts_distinct_items_across_all_lists() -> None:
    lists = [np.array([1, 2]), np.array([2, 3])]
    # {1,2,3} out of 10 items
    assert metrics.catalogue_coverage(lists, 10, 2) == pytest.approx(0.3)


def test_coverage_exposes_the_recommender_that_shows_everyone_the_same_things() -> None:
    same = [np.array([0, 1, 2]) for _ in range(100)]
    varied = [np.array([i, i + 1, i + 2]) for i in range(100)]
    assert metrics.catalogue_coverage(same, 200, 3) < metrics.catalogue_coverage(varied, 200, 3)


def test_novelty_is_higher_for_less_popular_items() -> None:
    popularity = np.array([100.0, 1.0])
    popular = metrics.novelty([np.array([0])], popularity, 1)
    obscure = metrics.novelty([np.array([1])], popularity, 1)
    assert obscure > popular


def test_novelty_of_an_unplayed_item_is_finite() -> None:
    """A zero-popularity item must not make the mean infinite."""
    value = metrics.novelty([np.array([1])], np.array([5.0, 0.0]), 1)
    assert np.isfinite(value)


def test_intra_list_diversity_of_identical_items_is_zero() -> None:
    assert metrics.intra_list_diversity([np.array([1, 2])], lambda a, b: 1.0, 2) == 0.0
    assert metrics.intra_list_diversity([np.array([1, 2])], lambda a, b: 0.0, 2) == 1.0


def test_intra_list_diversity_ignores_lists_too_short_to_have_a_pair() -> None:
    """Scoring a one-item list 0 would punish a model for a short list."""
    assert metrics.intra_list_diversity([np.array([1])], lambda a, b: 0.0, 5) == 0.0


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ([1, 1, 1, 1], 0.0),  # perfectly equal
        ([0, 0, 0, 4], 0.75),  # one item takes everything: (n-1)/n
        ([0, 0, 0, 0], 0.0),  # nothing at all is not inequality
    ],
)
def test_gini(values, expected) -> None:
    assert metrics.gini(np.array(values, dtype=float)) == pytest.approx(expected)


def test_gini_rejects_negative_values() -> None:
    with pytest.raises(ValueError, match="negative"):
        metrics.gini(np.array([-1.0, 2.0]))


def test_gini_is_scale_invariant() -> None:
    """Which is why fairness across differently-sized groups uses it."""
    base = np.array([1.0, 2.0, 7.0])
    assert metrics.gini(base) == pytest.approx(metrics.gini(base * 1000))
