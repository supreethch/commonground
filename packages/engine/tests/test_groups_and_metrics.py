"""Tests for synthetic group construction and the fairness metrics.

The group builder's labels are load-bearing: results are reported per kind, so
if "adversarial" groups are not actually less cohesive than "homogeneous" ones,
every conclusion drawn from that breakdown is wrong. That is checked against
measured cohesion rather than trusted because of the name.
"""

from __future__ import annotations

import numpy as np
import pytest
from commonground_engine import group_metrics
from commonground_engine.dataset import Interactions
from commonground_engine.groups import build_groups, cohesion_of, summarise


@pytest.fixture
def clustered() -> Interactions:
    """Four taste clusters of 15 users each; members share a block of items."""
    users, items, stamps = [], [], []
    stamp = 1000
    for cluster in range(4):
        for offset in range(15):
            user = cluster * 15 + offset
            for item in range(cluster * 25, cluster * 25 + 20):
                users.append(user)
                items.append(item)
                stamps.append(stamp := stamp + 1)
    return Interactions(
        users=np.array(users, dtype=np.int32),
        items=np.array(items, dtype=np.int32),
        timestamps=np.array(stamps, dtype=np.int64),
        n_users=60,
        n_items=120,
    )


# --------------------------------------------------------- group building --


def test_homogeneous_groups_are_more_cohesive_than_adversarial_ones(clustered) -> None:
    """The claim the per-kind results table depends on."""
    eligible = list(range(60))
    groups = build_groups(
        clustered,
        eligible,
        n_groups=80,
        sizes=(3, 4),
        seed=1,
        kinds=("homogeneous", "mixed", "adversarial"),
    )
    summary = summarise(groups)

    assert summary["homogeneous"]["mean_cohesion"] > summary["adversarial"]["mean_cohesion"], (
        f"group labels are not meaningful: {summary}"
    )


def test_group_building_is_deterministic_for_a_seed(clustered) -> None:
    eligible = list(range(60))
    first = build_groups(clustered, eligible, n_groups=40, seed=5)
    second = build_groups(clustered, eligible, n_groups=40, seed=5)

    assert [g.members for g in first] == [g.members for g in second]
    assert [g.kind for g in first] == [g.kind for g in second]


def test_group_building_differs_between_seeds(clustered) -> None:
    first = build_groups(clustered, list(range(60)), n_groups=40, seed=1)
    second = build_groups(clustered, list(range(60)), n_groups=40, seed=2)
    assert [g.members for g in first] != [g.members for g in second]


def test_every_group_has_at_least_two_distinct_members(clustered) -> None:
    for group in build_groups(clustered, list(range(60)), n_groups=60, seed=3):
        assert len(group.members) >= 2
        assert len(set(group.members)) == len(group.members)


def test_unknown_group_kinds_are_rejected(clustered) -> None:
    with pytest.raises(ValueError, match="unknown group kinds"):
        build_groups(clustered, list(range(60)), kinds=("nonsense",))


def test_too_few_eligible_users_is_an_explicit_error(clustered) -> None:
    with pytest.raises(ValueError, match="at least"):
        build_groups(clustered, [0, 1], sizes=(6,))


def test_cohesion_of_identical_members_is_one(clustered) -> None:
    matrix = clustered.to_csr()
    # Users 0 and 1 are in the same cluster with identical item sets.
    assert cohesion_of(matrix, [0, 1]) == pytest.approx(1.0)


def test_cohesion_of_disjoint_members_is_zero(clustered) -> None:
    matrix = clustered.to_csr()
    # Cluster 0 and cluster 3 share no items.
    assert cohesion_of(matrix, [0, 45]) == pytest.approx(0.0)


# ---------------------------------------------------------------- metrics --


def test_per_member_hit_rate_normalises_by_each_members_own_history() -> None:
    """A member with 20 held-out tracks and one with 5 are not comparable on
    raw hits, and comparing them anyway makes 'least satisfied' a measure of
    who listens most."""
    playlist = np.array([1, 2, 3])
    held_out = [{1, 2}, {3, 4, 5, 6, 7, 8, 9, 10}]

    rates = group_metrics.per_member_hit_rate(playlist, held_out)

    assert rates[0] == pytest.approx(1.0)  # 2 of 2
    assert rates[1] == pytest.approx(0.125)  # 1 of 8


def test_min_satisfaction_reports_the_worst_member() -> None:
    assert group_metrics.min_satisfaction(np.array([0.9, 0.8, 0.05])) == pytest.approx(0.05)


def test_gini_is_zero_when_everyone_is_served_equally() -> None:
    assert group_metrics.satisfaction_gini(np.array([0.5, 0.5, 0.5])) == pytest.approx(0.0)


def test_gini_rises_as_one_member_takes_everything() -> None:
    fair = group_metrics.satisfaction_gini(np.array([0.4, 0.5, 0.6]))
    unfair = group_metrics.satisfaction_gini(np.array([0.0, 0.0, 1.0]))
    assert unfair > fair


def test_member_share_catches_one_member_driving_the_playlist() -> None:
    # Member 0 is the top scorer on every track.
    scores = np.array([[0.9, 0.1], [0.8, 0.2], [0.7, 0.3]])
    share = group_metrics.member_share(scores)
    assert share.tolist() == [1.0, 0.0]


def test_repetition_reports_the_largest_artist_share() -> None:
    playlist = np.array([1, 2, 3, 4])
    artists = {1: "A", 2: "A", 3: "A", 4: "B"}
    assert group_metrics.repetition(playlist, artists) == pytest.approx(0.75)


def test_repetition_of_an_empty_playlist_is_zero_not_an_error() -> None:
    assert group_metrics.repetition(np.array([], dtype=int), {}) == 0.0


def test_veto_violations_counts_tracks_that_left_someone_behind() -> None:
    """Zero by construction for any mode with a veto; this is what shows the
    average-score baseline's cost."""
    scores = np.array([[0.9, 0.9], [0.9, 0.1], [0.5, 0.5]])
    assert group_metrics.veto_violations(scores, tau=0.35) == pytest.approx(1 / 3)


def test_evaluate_playlist_reports_held_out_and_proxy_separately() -> None:
    playlist = np.array([1, 2])
    member_scores = np.array([[0.9, 0.4], [0.8, 0.5]])
    held_out = [{1}, {5}]

    result = group_metrics.evaluate_playlist(
        playlist, member_scores, held_out, item_artists={1: "A", 2: "B"}
    )

    assert result["held_mean"] == pytest.approx(0.5)  # member 0 hit, member 1 missed
    assert result["held_min"] == pytest.approx(0.0)
    assert "proxy_mean" in result and "proxy_min" in result
    assert result["max_artist_share"] == pytest.approx(0.5)
    assert result["tracks"] == 2.0


def test_evaluate_playlist_survives_an_empty_playlist() -> None:
    result = group_metrics.evaluate_playlist(np.array([], dtype=int), np.zeros((0, 2)), [{1}, {2}])
    assert result["held_mean"] == 0.0
    assert result["tracks"] == 0.0
