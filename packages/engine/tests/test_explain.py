"""Explanation tests.

The project claims explanations are *derived from the arithmetic*, not written
alongside it. That claim is only worth making if something enforces it, so these
check the correspondence in both directions:

  * every clause traces to a term that actually moved the score, and
  * a term that did not fire produces no clause.

Without the second direction a template could quietly always say "introduces a
new artist", and it would pass any test that only looked at what was rendered.
"""

from __future__ import annotations

import numpy as np
import pytest
from commonground_engine.explain import explain, group_facts
from commonground_engine.group import CONSENSUS, DISCOVERY, GroupScores, SelectedTrack, select


def track(contributions: dict[str, float], member_scores: list[float]) -> SelectedTrack:
    return SelectedTrack(
        candidate_id=1,
        position=0,
        group_score=sum(contributions.values()),
        member_scores=np.array(member_scores),
        contributions=contributions,
    )


NAMES = ["Alex", "Sam", "Rio"]


# ------------------------------------------------- clause <-> contribution --


def test_every_clause_traces_to_a_contribution_that_fired() -> None:
    result = explain(
        track({"group_score": 0.8, "novelty_bonus": 0.3}, [0.9, 0.7, 0.8]),
        member_names=NAMES,
        familiar=[False, False, False],
    )

    assert result.clauses
    for source in result.sources:
        assert source in {"group_score", "novelty_bonus"}


def test_a_term_that_did_not_fire_produces_no_clause() -> None:
    """The direction that catches a template asserting something untrue."""
    result = explain(
        track({"group_score": 0.8}, [0.9, 0.7, 0.8]),
        member_names=NAMES,
        familiar=[True, True, True],
    )

    assert "novelty_bonus" not in result.sources
    assert "fairness_bonus" not in result.sources
    assert "new artist" not in result.sentence
    assert "turn to be served" not in result.sentence


def test_a_zero_valued_contribution_is_treated_as_not_fired() -> None:
    result = explain(
        track({"group_score": 0.8, "novelty_bonus": 0.0}, [0.9, 0.7, 0.8]),
        member_names=NAMES,
        familiar=[False, False, False],
    )
    assert "novelty_bonus" not in result.sources


def test_clauses_are_ordered_by_how_much_the_term_moved_the_score() -> None:
    """So the sentence leads with the reason that mattered, not the prettiest
    template."""
    result = explain(
        track({"group_score": 0.1, "novelty_bonus": 0.9}, [0.5, 0.5, 0.5]),
        member_names=NAMES,
        familiar=[False, False, False],
    )
    assert result.sources[0] == "novelty_bonus"


def test_penalties_are_reported_not_hidden() -> None:
    """A reason that only lists what helped is a sales pitch."""
    result = explain(
        track({"group_score": 0.7, "repetition_penalty": -0.5}, [0.8, 0.8, 0.8]),
        member_names=NAMES,
    )
    assert "repetition_penalty" in result.sources
    assert "repeats an artist" in result.sentence


# ----------------------------------------------------------- group facts --


def test_genre_counts_are_exact_not_inferred() -> None:
    facts = group_facts(
        track({"group_score": 0.5}, [0.5, 0.5, 0.5]),
        member_names=NAMES,
        item_genres=["indie rock", "shoegaze"],
        member_genres=[{"indie rock"}, {"indie rock", "jazz"}, {"shoegaze"}],
    )

    assert facts["shared_genres"] == {"indie rock": 2, "shoegaze": 1}
    assert facts["top_genre"] == "indie rock"
    assert facts["top_genre_members"] == 2


def test_the_genre_clause_reports_the_real_count() -> None:
    result = explain(
        track({"group_score": 0.8}, [0.9, 0.7, 0.8]),
        member_names=NAMES,
        item_genres=["indie rock"],
        member_genres=[{"indie rock"}, {"indie rock"}, {"jazz"}],
    )
    assert "2 members like indie rock" in result.sentence


def test_a_genre_everyone_shares_is_phrased_as_everyone() -> None:
    """And with the right verb: an earlier version emitted "everyone like"."""
    result = explain(
        track({"group_score": 0.8}, [0.9, 0.7, 0.8]),
        member_names=NAMES,
        item_genres=["indie rock"],
        member_genres=[{"indie rock"}] * 3,
    )
    assert "everyone likes indie rock" in result.sentence


def test_the_fairness_clause_names_the_member_the_ranker_actually_served() -> None:
    """Not the lowest scorer on this track -- the two differ, and naming the
    wrong one would describe a quantity that never moved the score."""
    selected = track({"group_score": 0.5, "fairness_bonus": 0.4}, [0.2, 0.9, 0.5])
    selected.served_member = 1  # Sam, who is the *highest* scorer here

    result = explain(selected, member_names=NAMES)

    assert "Sam's turn to be served" in result.sentence
    assert "Alex's turn" not in result.sentence


def test_no_fairness_clause_when_the_ranker_recorded_no_served_member() -> None:
    selected = track({"group_score": 0.5, "fairness_bonus": 0.4}, [0.2, 0.9, 0.5])
    selected.served_member = None

    result = explain(selected, member_names=NAMES)

    assert "turn to be served" not in result.sentence


def test_best_and_worst_member_are_identified_by_score() -> None:
    facts = group_facts(
        track({"group_score": 0.5}, [0.2, 0.9, 0.5]),
        member_names=NAMES,
    )
    assert facts["best_member"] == "Sam"
    assert facts["worst_member"] == "Alex"


def test_without_genres_the_explanation_falls_back_to_whose_taste_it_matches() -> None:
    result = explain(
        track({"group_score": 0.8}, [0.2, 0.9, 0.5]),
        member_names=NAMES,
    )
    assert "closest to Sam's taste" in result.sentence


def test_new_to_everyone_is_distinguished_from_merely_less_familiar() -> None:
    unknown = explain(
        track({"group_score": 0.5, "novelty_bonus": 0.4}, [0.5, 0.5, 0.5]),
        member_names=NAMES,
        familiar=[False, False, False],
    )
    known = explain(
        track({"group_score": 0.5, "novelty_bonus": 0.4}, [0.5, 0.5, 0.5]),
        member_names=NAMES,
        familiar=[True, False, False],
    )

    assert "new to everyone" in unknown.sentence
    assert "new to everyone" not in known.sentence


# ------------------------------------------------------------- veto note --


def test_the_veto_note_appears_only_when_the_track_actually_cleared_it() -> None:
    cleared = explain(
        track({"group_score": 0.8}, [0.9, 0.7, 0.8]), member_names=NAMES, tau_veto=0.35
    )
    borderline = explain(
        track({"group_score": 0.8}, [0.9, 0.7, 0.1]), member_names=NAMES, tau_veto=0.35
    )

    assert "without strongly conflicting" in cleared.sentence
    assert "without strongly conflicting" not in borderline.sentence


def test_a_solo_member_gets_no_group_veto_language() -> None:
    result = explain(track({"group_score": 0.8}, [0.9]), member_names=["Alex"])
    assert "anyone's dislikes" not in result.sentence


# -------------------------------------------------- against a real ranking --


def test_explanations_match_the_ranker_that_produced_them() -> None:
    """End to end: rank a real candidate set, then check every rendered clause
    against the contributions the ranker actually recorded."""
    rng = np.random.default_rng(11)
    matrix = rng.random((3, 50))
    scores = GroupScores(matrix=matrix, candidate_ids=np.arange(50))
    novelty = rng.random(50)

    for mode in (CONSENSUS, DISCOVERY):
        playlist = select(scores, mode, k=10, novelty=novelty, seed=3)
        assert playlist.tracks

        for selected in playlist.tracks:
            result = explain(
                selected,
                member_names=NAMES,
                familiar=[False, False, False],
                tau_veto=mode.tau_veto,
            )
            for source in result.sources:
                assert source in selected.contributions, (
                    f"clause cited {source!r}, which the ranker never recorded"
                )
                assert selected.contributions[source] != 0.0


def test_a_track_with_no_contributions_still_renders_something_sane() -> None:
    result = explain(track({}, [0.5, 0.5, 0.5]), member_names=NAMES)
    assert result.sentence == "Recommended for this group."
    assert result.clauses == []


@pytest.mark.parametrize("max_clauses", [1, 2, 3])
def test_clause_count_is_capped(max_clauses: int) -> None:
    result = explain(
        track(
            {
                "group_score": 0.8,
                "novelty_bonus": 0.6,
                "fairness_bonus": 0.4,
                "repetition_penalty": -0.2,
            },
            [0.9, 0.7, 0.8],
        ),
        member_names=NAMES,
        familiar=[False, False, False],
        max_clauses=max_clauses,
    )
    assert len(result.clauses) <= max_clauses
