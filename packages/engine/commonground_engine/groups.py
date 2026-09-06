"""Build synthetic groups out of real users.

Real groups do not exist in a listen dump -- nobody records "these five people
shared a car" -- so groups are constructed, and **the construction rule matters
more than the sample size**. A recommender evaluated only on groups of people
who already agree will look excellent and prove nothing, because averaging works
fine when everyone wants the same thing.

So groups are sampled at four difficulty levels, and results are reported per
level rather than pooled. The adversarial level is the one the whole project
exists for.

Sampling is seeded and the seed is recorded, so a group set rebuilds exactly.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import sparse

from .dataset import Interactions

KINDS = ("homogeneous", "mixed", "adversarial", "cold_start")


@dataclass(frozen=True)
class Group:
    members: list[int]
    kind: str
    # Mean pairwise Jaccard similarity of the members' training histories.
    # Recorded so a claim about "adversarial" groups can be checked against a
    # number rather than trusted because of the label.
    cohesion: float

    @property
    def size(self) -> int:
        return len(self.members)


def _jaccard_matrix(matrix: sparse.csr_matrix, users: np.ndarray) -> np.ndarray:
    """Pairwise Jaccard similarity between the given users' item sets."""
    rows = matrix[users].astype(bool).astype(np.float32)
    intersection = (rows @ rows.T).toarray()
    sizes = np.asarray(rows.sum(axis=1)).ravel()
    union = sizes[:, None] + sizes[None, :] - intersection
    with np.errstate(divide="ignore", invalid="ignore"):
        similarity = np.where(union > 0, intersection / union, 0.0)
    np.fill_diagonal(similarity, 0.0)
    return similarity


def cohesion_of(matrix: sparse.csr_matrix, members: list[int]) -> float:
    """Mean pairwise Jaccard of a group's histories."""
    if len(members) < 2:
        return 0.0
    similarity = _jaccard_matrix(matrix, np.array(members))
    n = len(members)
    return float(similarity.sum() / (n * (n - 1)))


def build_groups(
    data: Interactions,
    eligible: list[int],
    *,
    n_groups: int = 200,
    sizes: tuple[int, ...] = (3, 4, 5, 6),
    kinds: tuple[str, ...] = KINDS,
    seed: int = 0,
    candidate_pool: int = 60,
) -> list[Group]:
    """Sample groups of each kind from users that have held-out data.

    The similarity search is over a random pool per group rather than the whole
    user base: an exhaustive nearest/farthest search is quadratic in users and
    would dominate the experiment, while a pool of 60 is more than enough to
    find members that clearly agree or clearly do not.
    """
    unknown = set(kinds) - set(KINDS)
    if unknown:
        raise ValueError(f"unknown group kinds: {sorted(unknown)}")
    if len(eligible) < max(sizes) + 1:
        raise ValueError(f"need at least {max(sizes) + 1} eligible users, got {len(eligible)}")

    matrix = data.to_csr()
    rng = np.random.default_rng(seed)
    eligible_array = np.array(sorted(eligible))

    # Cold-start members are the users with the least history. They are not
    # "bad" users -- they are what a real demo room is full of, and a group
    # recommender that only works for people with deep profiles is not useful.
    history_sizes = np.asarray(matrix[eligible_array].sum(axis=1)).ravel()
    # lexsort, not argsort over a tuple: argsort on a 2-row array sorts *along
    # axis 0*, which returns positions within each column rather than an
    # ordering of the users, and produced repeated ids in cold-start groups.
    sparse_first = eligible_array[np.lexsort((eligible_array, history_sizes))]

    groups: list[Group] = []
    per_kind = max(1, n_groups // len(kinds))

    for kind in kinds:
        for _ in range(per_kind):
            size = int(rng.choice(sizes))
            pool = rng.choice(
                eligible_array, size=min(candidate_pool, len(eligible_array)), replace=False
            )
            pool = np.sort(pool)

            if kind == "cold_start":
                # Majority of members drawn from the shortest histories.
                n_cold = max(1, (size + 1) // 2)
                cold = rng.choice(
                    sparse_first[: max(len(sparse_first) // 4, size)], size=n_cold, replace=False
                )
                rest = rng.choice(np.setdiff1d(pool, cold), size=size - n_cold, replace=False)
                # set(), not a list: the cold pool and the random pool overlap,
                # so the same user can be drawn twice, and a group containing
                # one person three times would silently weight them triple in
                # every aggregate.
                members = sorted({int(m) for m in np.concatenate([cold, rest])})
            else:
                similarity = _jaccard_matrix(matrix, pool)
                seed_index = int(rng.integers(len(pool)))
                order = np.lexsort((pool, -similarity[seed_index]))

                if kind == "homogeneous":
                    picked = order[: size - 1]
                elif kind == "adversarial":
                    # Furthest members: near-disjoint taste, which is where
                    # averaging visibly fails.
                    picked = order[-(size - 1) :]
                else:  # mixed
                    middle = len(order) // 2
                    picked = order[middle : middle + size - 1]

                members = sorted({int(pool[seed_index])} | {int(pool[i]) for i in picked})

            if len(members) < 2:
                continue
            groups.append(
                Group(
                    members=members,
                    kind=kind,
                    cohesion=cohesion_of(matrix, members),
                )
            )

    return groups


def summarise(groups: list[Group]) -> dict[str, dict]:
    """Mean size and cohesion per kind, so the labels can be sanity-checked."""
    summary: dict[str, dict] = {}
    for kind in sorted({group.kind for group in groups}):
        selected = [g for g in groups if g.kind == kind]
        summary[kind] = {
            "groups": len(selected),
            "mean_size": round(float(np.mean([g.size for g in selected])), 2),
            "mean_cohesion": round(float(np.mean([g.cohesion for g in selected])), 5),
        }
    return summary
