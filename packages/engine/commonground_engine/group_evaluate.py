"""Evaluate group strategies against each other on identical machinery.

The claim this harness has to test is **not** "highest mean satisfaction".
Average-score will often win that, and saying so is the point. The claim is:

    a better floor and a fairer distribution, at an acceptable cost to the mean

which is a trade, so it is reported as a trade -- in both directions, including
where CommonGround loses.

Every strategy sees the same candidate set, the same per-member score matrix and
the same held-out data. Only the aggregation and selection differ, so a
difference in the results is a difference in the strategy rather than in the
plumbing around it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np

from . import group as group_module
from . import group_metrics
from .dataset import Interactions
from .group import GroupScores, ModeConfig
from .groups import Group
from .recommenders import Recommender
from .split import Split


@dataclass
class GroupResult:
    strategy: str
    kind_scores: dict[str, dict[str, float]] = field(default_factory=dict)
    overall: dict[str, float] = field(default_factory=dict)
    groups_evaluated: int = 0
    seconds: float = 0.0

    def as_dict(self) -> dict:
        return {
            "strategy": self.strategy,
            "groups_evaluated": self.groups_evaluated,
            "seconds": round(self.seconds, 3),
            "overall": {k: round(v, 6) for k, v in sorted(self.overall.items())},
            "by_kind": {
                kind: {k: round(v, 6) for k, v in sorted(scores.items())}
                for kind, scores in sorted(self.kind_scores.items())
            },
        }


def build_candidates(
    model: Recommender,
    members: list[int],
    seen: list[set[int]],
    *,
    per_member: int = 200,
    popularity: np.ndarray | None = None,
    popularity_pool: int = 200,
) -> np.ndarray:
    """Union of each member's top items, plus a popularity pool.

    The popularity pool is what stops a room of cold-start members getting an
    empty candidate set -- and it is added for every group, not only cold ones,
    so the candidate construction does not itself differ between group kinds.
    """
    candidates: set[int] = set()
    for member in members:
        ranked = model.recommend(member, per_member, exclude=seen[member])
        candidates.update(int(item) for item in ranked)

    if popularity is not None and popularity_pool:
        top = np.argsort(-popularity, kind="stable")[:popularity_pool]
        candidates.update(int(item) for item in top)

    # Nothing any member has already heard can be a recommendation.
    for member in members:
        candidates -= seen[member]

    return np.array(sorted(candidates), dtype=np.int64)


def score_members(model: Recommender, members: list[int], candidates: np.ndarray) -> GroupScores:
    """Per-member satisfaction over the candidate set, rank-percentile normalised.

    Normalising *within the candidate set* rather than over the whole catalogue
    matters: the question at this point is "where does this track sit among the
    things we could actually play you", and including 12,000 items nobody is
    considering would compress every real difference into the top percentile.
    """
    raw = np.vstack([model.score_all(member)[candidates] for member in members])
    return GroupScores(
        matrix=group_module.rank_percentile(raw),
        candidate_ids=candidates,
        member_ids=list(members),
    )


def _strategies(modes: dict[str, ModeConfig], include_baselines: bool = True) -> dict[str, object]:
    strategies: dict[str, object] = dict(modes)
    if include_baselines:
        # Expressed in the same machinery (alpha=1 / alpha=0, no penalties) so a
        # difference cannot be an artefact of two implementations.
        strategies["average-score"] = ModeConfig(
            name="average-score", alpha=1.0, tau_veto=0.0, lambda_div=0.0, lambda_rep=0.0
        )
        strategies["least-misery"] = ModeConfig(
            name="least-misery", alpha=0.0, tau_veto=0.0, lambda_div=0.0, lambda_rep=0.0
        )
        strategies["popularity"] = "popularity"
    return strategies


def evaluate_groups(
    model: Recommender,
    data: Interactions,
    split: Split,
    groups: list[Group],
    *,
    modes: dict[str, ModeConfig],
    k: int = 20,
    per_member_candidates: int = 200,
    item_artists: dict[int, str] | None = None,
    seed: int = 0,
    include_baselines: bool = True,
) -> list[GroupResult]:
    """Run every strategy over every group and collect the metrics."""
    popularity = split.train.item_popularity()
    seen = split.train.seen_by_user()
    strategies = _strategies(modes, include_baselines)

    accumulated: dict[str, dict[str, list[float]]] = {name: {} for name in strategies}
    by_kind: dict[str, dict[str, dict[str, list[float]]]] = {name: {} for name in strategies}
    timings: dict[str, float] = dict.fromkeys(strategies, 0.0)
    evaluated = 0

    for group in groups:
        members = [m for m in group.members if m in split.test]
        if len(members) < 2:
            continue

        candidates = build_candidates(
            model, members, seen, per_member=per_member_candidates, popularity=popularity
        )
        if len(candidates) < k:
            continue

        scores = score_members(model, members, candidates)
        held_out = [{int(i) for i in split.test[m]} for m in members]
        candidate_popularity = popularity[candidates]

        # Novelty over the candidate set: low global popularity, expressed on
        # the same [0, 1] scale as everything else the ranker adds up.
        novelty = group_module.rank_percentile(-candidate_popularity[None, :])[0]

        for name, strategy in strategies.items():
            started = time.perf_counter()
            if strategy == "popularity":
                playlist = group_module.select_popularity(scores, candidate_popularity, k)
            else:
                playlist = group_module.select(
                    scores,
                    strategy,
                    k,
                    item_artists=item_artists,
                    novelty=novelty,
                    seed=seed,
                )
            timings[name] += time.perf_counter() - started

            member_scores = (
                np.vstack([t.member_scores for t in playlist.tracks])
                if playlist.tracks
                else np.zeros((0, len(members)))
            )
            metrics = group_metrics.evaluate_playlist(
                playlist.item_ids,
                member_scores,
                held_out,
                item_artists=item_artists,
                popularity=data.item_popularity(),
                tau=modes.get("consensus", group_module.CONSENSUS).tau_veto,
            )
            metrics["vetoed_candidates"] = float(playlist.n_vetoed) / max(len(candidates), 1)

            for key, value in metrics.items():
                accumulated[name].setdefault(key, []).append(value)
                by_kind[name].setdefault(group.kind, {}).setdefault(key, []).append(value)

        evaluated += 1

    results = []
    for name in strategies:
        results.append(
            GroupResult(
                strategy=name,
                overall={key: float(np.mean(values)) for key, values in accumulated[name].items()},
                kind_scores={
                    kind: {k: float(np.mean(v)) for k, v in scores.items()}
                    for kind, scores in by_kind[name].items()
                },
                groups_evaluated=evaluated,
                seconds=timings[name],
            )
        )
    return results


def summary_table(results: list[GroupResult]) -> str:
    """Markdown, so docs/measurements.md stays generated rather than typed."""
    columns = [
        ("held mean", "held_mean"),
        ("**held served**", "held_served"),
        ("held gini", "held_gini"),
        ("proxy mean", "proxy_mean"),
        ("proxy min", "proxy_min"),
        ("veto viol.", "veto_violations"),
        ("max artist", "max_artist_share"),
        ("novelty", "novelty"),
    ]
    lines = [
        "| strategy | " + " | ".join(name for name, _ in columns) + " |",
        "| --- | " + " | ".join("---" for _ in columns) + " |",
    ]
    for result in results:
        cells = [f"{result.overall.get(key, 0.0):.4f}" for _, key in columns]
        lines.append(f"| {result.strategy} | " + " | ".join(cells) + " |")
    return "\n".join(lines)
