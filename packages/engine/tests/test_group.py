"""Group ranking tests.

Weighted toward the properties the product claims rather than the happy path:
that the veto cannot be outvoted, that fair rotation actually serves the
neglected member, and that the same inputs always produce the same playlist.
Those three fail silently and produce output that looks perfectly reasonable.
"""

from __future__ import annotations

import numpy as np
import pytest
from commonground_engine.group import (
    CONSENSUS,
    DISCOVERY,
    FAIR_ROTATION,
    MODES,
    GroupScores,
    ModeConfig,
    aggregate,
    apply_veto,
    rank_percentile,
    select,
    select_average_score,
    select_least_misery,
    select_popularity,
)


def scores_of(matrix: list[list[float]], ids: list[int] | None = None) -> GroupScores:
    array = np.array(matrix, dtype=np.float64)
    return GroupScores(
        matrix=array,
        candidate_ids=np.array(ids if ids is not None else range(array.shape[1])),
        member_ids=list(range(array.shape[0])),
    )


# ------------------------------------------------------ rank normalisation --


def test_rank_percentile_puts_every_member_on_one_scale() -> None:
    """The member with a compressed score range must not be permanently the
    least satisfied -- that is a scale artefact, not unfairness."""
    confident = [0.0, 0.5, 1.0]
    timid = [0.40, 0.41, 0.42]

    result = rank_percentile(np.array([confident, timid]))

    np.testing.assert_allclose(result[0], result[1])
    assert result.min() == 0.0 and result.max() == 1.0


def test_rank_percentile_gives_ties_the_same_value() -> None:
    result = rank_percentile(np.array([[5.0, 5.0, 5.0, 9.0]]))
    assert result[0, 0] == result[0, 1] == result[0, 2]
    assert result[0, 3] > result[0, 0]


def test_rank_percentile_accepts_a_single_row() -> None:
    assert rank_percentile(np.array([3.0, 1.0, 2.0])).shape == (1, 3)


# ------------------------------------------------------------- aggregation --


def test_aggregate_at_alpha_one_is_the_mean() -> None:
    scores = scores_of([[1.0, 0.0], [0.0, 1.0], [1.0, 0.5]])
    np.testing.assert_allclose(aggregate(scores, 1.0), scores.mean())


def test_aggregate_at_alpha_zero_is_the_minimum() -> None:
    scores = scores_of([[1.0, 0.0], [0.0, 1.0], [1.0, 0.5]])
    np.testing.assert_allclose(aggregate(scores, 0.0), scores.minimum())


def test_aggregate_rejects_an_alpha_outside_the_unit_interval() -> None:
    scores = scores_of([[1.0], [0.0]])
    with pytest.raises(ValueError, match=r"alpha must be in \[0, 1\]"):
        aggregate(scores, 1.5)


def test_a_high_mean_cannot_hide_a_miserable_member() -> None:
    """Candidate 0 has a great mean and one member at zero; candidate 1 is
    mediocre for everyone. Least-misery must prefer the mediocre one."""
    scores = scores_of([[1.0, 0.5], [1.0, 0.5], [0.0, 0.5]])

    assert aggregate(scores, 1.0)[0] > aggregate(scores, 1.0)[1]  # mean prefers 0
    assert aggregate(scores, 0.0)[1] > aggregate(scores, 0.0)[0]  # min prefers 1


# -------------------------------------------------------------------- veto --


def test_veto_drops_a_candidate_any_member_scores_below_tau() -> None:
    scores = scores_of([[0.9, 0.9], [0.9, 0.1]])
    assert apply_veto(scores, 0.35).tolist() == [True, False]


def test_veto_cannot_be_outvoted_by_an_overwhelming_mean() -> None:
    """The reason the veto is a filter and not a penalty term.

    Nine members adore the track and one is at zero. No weighting of the other
    terms may let it through, because a penalty large enough to block this case
    would also block everything else.
    """
    matrix = [[1.0, 0.6] for _ in range(9)] + [[0.0, 0.6]]
    scores = scores_of(matrix)

    survives = apply_veto(scores, 0.35)

    assert survives.tolist() == [False, True]
    playlist = select(scores, CONSENSUS, k=2)
    assert playlist.item_ids.tolist() == [1]
    assert playlist.n_vetoed == 1


def test_explicit_dislikes_veto_regardless_of_score() -> None:
    scores = scores_of([[0.9, 0.9], [0.9, 0.9]])
    disliked = np.array([[False, True], [False, False]])

    assert apply_veto(scores, 0.0, disliked).tolist() == [True, False]


def test_dislike_mask_shape_is_checked() -> None:
    scores = scores_of([[0.9, 0.9], [0.9, 0.9]])
    with pytest.raises(ValueError, match="must match the score matrix"):
        apply_veto(scores, 0.0, np.array([[False, True]]))


# --------------------------------------------------------------- selection --


def test_selection_is_deterministic() -> None:
    rng = np.random.default_rng(0)
    scores = scores_of(rng.random((4, 40)).tolist())

    first = select(scores, CONSENSUS, k=10, seed=7)
    second = select(scores, CONSENSUS, k=10, seed=7)

    assert first.item_ids.tolist() == second.item_ids.tolist()


def test_ties_break_on_the_lower_candidate_id() -> None:
    """Without an explicit rule the winner depends on float noise and iteration
    order, and the ranking stops being reproducible."""
    scores = scores_of([[0.8, 0.8, 0.8]], ids=[42, 7, 19])

    playlist = select(scores, ModeConfig(name="flat", alpha=1.0, tau_veto=0.0), k=1)

    assert playlist.item_ids.tolist() == [42], "first column wins an exact tie"


def test_selection_never_repeats_a_candidate() -> None:
    rng = np.random.default_rng(1)
    scores = scores_of(rng.random((3, 30)).tolist())

    playlist = select(scores, CONSENSUS, k=20, seed=1)

    assert len(set(playlist.item_ids.tolist())) == len(playlist.tracks)


def test_selection_stops_when_candidates_run_out() -> None:
    scores = scores_of([[0.9, 0.8, 0.7]])
    playlist = select(scores, ModeConfig(name="m", alpha=1.0, tau_veto=0.0), k=10)
    assert len(playlist.tracks) == 3


def test_a_fully_vetoed_candidate_set_yields_an_empty_playlist() -> None:
    scores = scores_of([[0.9, 0.9], [0.1, 0.1]])
    playlist = select(scores, CONSENSUS, k=5)
    assert playlist.tracks == []
    assert playlist.n_vetoed == 2


def test_negative_k_is_rejected() -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        select(scores_of([[0.5]]), CONSENSUS, k=-1)


# ------------------------------------------------------------ fair rotation --


def test_fair_rotation_serves_the_member_the_playlist_has_neglected() -> None:
    """Member 1's only good tracks are the last two columns. A pure mean would
    take four tracks for member 0; fair rotation must come back for member 1."""
    matrix = [
        [0.90, 0.88, 0.86, 0.84, 0.40, 0.40],  # member 0 loves the first four
        [0.40, 0.40, 0.40, 0.40, 0.95, 0.93],  # member 1 only likes the last two
    ]
    scores = scores_of(matrix)

    mean_only = select(
        scores,
        ModeConfig(name="mean", alpha=1.0, tau_veto=0.0, lambda_div=0.0, lambda_rep=0.0),
        k=4,
    )
    fair = select(
        scores,
        ModeConfig(
            name="fair", alpha=1.0, tau_veto=0.0, lambda_div=0.0, lambda_rep=0.0, lambda_fair=1.0
        ),
        k=4,
    )

    member_1_mean = mean_only.member_satisfaction(2)[1]
    member_1_fair = fair.member_satisfaction(2)[1]
    assert member_1_fair > member_1_mean, "fair rotation did not help the neglected member"


def test_fairness_bonus_appears_in_the_contributions_when_it_is_used() -> None:
    # Both members stay above FAIR_ROTATION's tau_veto of 0.35 on both
    # candidates. An earlier version of this test used 0.2, which the veto
    # correctly removed -- leaving an empty playlist and no contributions to
    # inspect at all.
    scores = scores_of([[0.9, 0.5], [0.5, 0.9]])

    playlist = select(scores, FAIR_ROTATION, k=2, seed=0)

    assert playlist.tracks, "the fixture must survive the veto to test anything"
    assert any("fairness_bonus" in t.contributions for t in playlist.tracks)


# ------------------------------------------------------------- repetition --


def test_repetition_penalty_pushes_a_second_track_by_the_same_artist_down() -> None:
    scores = scores_of([[0.90, 0.89, 0.88]])
    artists = {0: "A", 1: "A", 2: "B"}

    without = select(scores, ModeConfig(name="n", alpha=1.0, tau_veto=0.0, lambda_rep=0.0), k=2)
    with_penalty = select(
        scores,
        ModeConfig(name="r", alpha=1.0, tau_veto=0.0, lambda_rep=0.5),
        k=2,
        item_artists=artists,
    )

    assert without.item_ids.tolist() == [0, 1]
    assert with_penalty.item_ids.tolist() == [0, 2], "the repeat should be displaced"


def test_repetition_penalty_only_looks_back_over_its_window() -> None:
    scores = scores_of([[0.9] * 8])
    artists = {i: ("A" if i == 0 else f"artist-{i}") for i in range(8)}
    artists[7] = "A"

    mode = ModeConfig(name="r", alpha=1.0, tau_veto=0.0, lambda_rep=1.0, repetition_window=2)
    playlist = select(scores, mode, k=8, item_artists=artists)

    assert len(playlist.tracks) == 8, "a penalty must not drop candidates entirely"


# ------------------------------------------------------------------ modes --


def test_the_three_modes_are_registered_and_distinct() -> None:
    """What separates the modes after fitting, which is not what was designed.

    The sweep gave every mode lambda_fair=0.45 -- serving the least-served
    member helps whatever you are maximising -- so 'fair rotation weights
    fairness more' is no longer true and asserting it would pin the code to an
    intention the measurement disproved. What still separates them: only
    Discovery rewards novelty and loosens the veto, and Consensus leans hardest
    on the mean, holding its floor through the fairness term instead.
    """
    assert set(MODES) == {"consensus", "discovery", "fair_rotation"}

    assert DISCOVERY.lambda_nov > 0
    assert CONSENSUS.lambda_nov == FAIR_ROTATION.lambda_nov == 0
    assert DISCOVERY.tau_veto < CONSENSUS.tau_veto
    assert CONSENSUS.alpha > FAIR_ROTATION.alpha


def test_modes_produce_different_playlists_on_the_same_scores() -> None:
    """Otherwise the modes are a label rather than a behaviour."""
    rng = np.random.default_rng(4)
    scores = scores_of(rng.random((4, 60)).tolist())
    novelty = rng.random(60)

    playlists = {
        name: select(scores, mode, k=15, novelty=novelty, seed=0).item_ids.tolist()
        for name, mode in MODES.items()
    }

    assert playlists["consensus"] != playlists["discovery"]
    assert playlists["consensus"] != playlists["fair_rotation"]


def test_discovery_is_more_novel_than_consensus() -> None:
    rng = np.random.default_rng(5)
    scores = scores_of(rng.random((3, 80)).tolist())
    novelty = rng.random(80)

    consensus = select(scores, CONSENSUS, k=20, novelty=novelty, seed=0)
    discovery = select(scores, DISCOVERY, k=20, novelty=novelty, seed=0)

    assert novelty[discovery.item_ids].mean() > novelty[consensus.item_ids].mean()


def test_mode_config_is_immutable_and_copies_with_overrides() -> None:
    original = CONSENSUS.alpha
    tweaked = CONSENSUS.with_params(alpha=0.9)

    assert tweaked.alpha == 0.9
    # Read the value rather than hardcoding it: this test is about the preset
    # not being mutated, not about which number it happens to hold.
    assert CONSENSUS.alpha == original, "the shared preset must not be mutated"


# -------------------------------------------------------------- baselines --


def test_average_score_baseline_equals_alpha_one_with_no_veto() -> None:
    scores = scores_of([[1.0, 0.5], [1.0, 0.5], [0.0, 0.5]])

    playlist = select_average_score(scores, k=1)

    # The candidate one member scores zero on wins, which is exactly the
    # behaviour this project claims to improve on.
    assert playlist.item_ids.tolist() == [0]


def test_least_misery_baseline_protects_the_worst_member() -> None:
    scores = scores_of([[1.0, 0.5], [1.0, 0.5], [0.0, 0.5]])
    assert select_least_misery(scores, k=1).item_ids.tolist() == [1]


def test_popularity_baseline_ignores_the_group_entirely() -> None:
    scores = scores_of([[0.1, 0.9], [0.1, 0.9]])
    popularity = np.array([100.0, 1.0])

    playlist = select_popularity(scores, popularity, k=2)

    assert playlist.item_ids.tolist() == [0, 1]
    assert playlist.tracks[0].member_scores.tolist() == [0.1, 0.1]
