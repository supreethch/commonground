"""Recommenders: the baselines, the two component models, and the hybrid.

Every model implements the same tiny interface, so the evaluation harness treats
a random baseline and the hybrid identically and the comparison cannot quietly
be run on different machinery:

    model.fit(train)
    model.score_all(user) -> array of length n_items

`score_all` returns raw scores for *every* item; excluding already-seen items and
taking the top k happens in one shared place, because "did you remember to
exclude the training items" is exactly the kind of difference that makes one
model look better than another for no real reason.

Determinism: every model produces the same output for the same input and seed.
Ties are broken by ascending item id via a stable lexsort, never by whatever
order argpartition happened to produce.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy import sparse
from sklearn.feature_extraction.text import TfidfTransformer
from sklearn.preprocessing import normalize

from .dataset import Interactions


def top_k(scores: np.ndarray, k: int, exclude: set[int] | None = None) -> np.ndarray:
    """Top-k item ids by score, ties broken by ascending item id.

    Excluded items are set to -inf rather than removed, so the returned ids
    still index the original item space.
    """
    scores = np.asarray(scores, dtype=np.float64).copy()
    if exclude:
        scores[np.fromiter(exclude, dtype=np.int64, count=len(exclude))] = -np.inf

    k = min(k, len(scores))
    if k <= 0:
        return np.empty(0, dtype=np.int64)

    # lexsort is stable and sorts by the last key first: primary key is the
    # negated score, secondary is the item id.
    candidates = np.argpartition(-scores, k - 1)[:k]
    order = np.lexsort((candidates, -scores[candidates]))
    chosen = candidates[order]
    return chosen[np.isfinite(scores[chosen])]


class Recommender:
    name = "base"

    def fit(self, data: Interactions) -> Recommender:
        raise NotImplementedError

    def score_all(self, user: int) -> np.ndarray:
        raise NotImplementedError

    def recommend(self, user: int, k: int, exclude: set[int] | None = None) -> np.ndarray:
        return top_k(self.score_all(user), k, exclude)


# ------------------------------------------------------------- baselines --


@dataclass
class RandomRecommender(Recommender):
    """The floor. Anything that cannot beat this is broken."""

    seed: int = 0
    name: str = "random"
    _n_items: int = 0
    _rng: np.random.Generator = field(default_factory=lambda: np.random.default_rng(0))

    def fit(self, data: Interactions) -> RandomRecommender:
        self._n_items = data.n_items
        self._rng = np.random.default_rng(self.seed)
        return self

    def score_all(self, user: int) -> np.ndarray:
        # Seeded per user so a user's recommendations are reproducible
        # independently of the order users are evaluated in.
        return np.random.default_rng([self.seed, user]).random(self._n_items)


@dataclass
class PopularityRecommender(Recommender):
    """Rank by how many users played each item.

    Included because it is genuinely hard to beat, not as a straw man. A group
    recommender that cannot beat "play the hits" has not earned its complexity.
    """

    name: str = "popularity"
    _popularity: np.ndarray = field(default_factory=lambda: np.zeros(0))

    def fit(self, data: Interactions) -> PopularityRecommender:
        self._popularity = data.item_popularity()
        return self

    def score_all(self, user: int) -> np.ndarray:
        return self._popularity


# -------------------------------------------------------- component models --


@dataclass
class ItemKNN(Recommender):
    """Item-item collaborative filtering on the co-occurrence matrix.

    Cosine similarity between item columns, truncated to the `k_neighbours`
    strongest per item. Truncation is not only about speed: a dense similarity
    matrix lets thousands of weak, mostly-noise similarities sum into a score
    that outweighs a handful of strong ones.
    """

    k_neighbours: int = 200
    name: str = "item-knn"
    _similarity: sparse.csr_matrix | None = None
    _user_items: sparse.csr_matrix | None = None

    def fit(self, data: Interactions) -> ItemKNN:
        matrix = data.to_csr()
        self._user_items = matrix

        normalised = normalize(matrix.T.tocsr(), norm="l2", axis=1)
        similarity = (normalised @ normalised.T).tolil()
        similarity.setdiag(0)  # an item is not its own neighbour
        similarity = similarity.tocsr()
        similarity.eliminate_zeros()

        self._similarity = _truncate_rows(similarity, self.k_neighbours)
        return self

    def score_all(self, user: int) -> np.ndarray:
        assert self._similarity is not None and self._user_items is not None
        profile = self._user_items[user]
        return np.asarray((profile @ self._similarity).todense()).ravel()


@dataclass
class ContentKNN(Recommender):
    """Content-based scoring over item features.

    Features are supplied by the caller as a sparse item x feature matrix, so
    the engine stays ignorant of where they came from. In this project they are
    artist identity for every item plus genre tags for the third of items whose
    artist is in the tagged catalogue -- measured, not assumed. TF-IDF weighting
    stops "rock", which half the catalogue carries, from swamping the genres
    that actually distinguish anything.
    """

    features: sparse.csr_matrix | None = None
    k_neighbours: int = 200
    name: str = "content-knn"
    _similarity: sparse.csr_matrix | None = None
    _user_items: sparse.csr_matrix | None = None

    def fit(self, data: Interactions) -> ContentKNN:
        if self.features is None:
            raise ValueError("ContentKNN needs an item x feature matrix")
        if self.features.shape[0] != data.n_items:
            raise ValueError(
                f"features have {self.features.shape[0]} rows but the dataset has "
                f"{data.n_items} items"
            )

        self._user_items = data.to_csr()
        weighted = TfidfTransformer().fit_transform(self.features)
        normalised = normalize(weighted, norm="l2", axis=1)
        similarity = (normalised @ normalised.T).tolil()
        similarity.setdiag(0)
        similarity = similarity.tocsr()
        similarity.eliminate_zeros()
        self._similarity = _truncate_rows(similarity, self.k_neighbours)
        return self

    def score_all(self, user: int) -> np.ndarray:
        assert self._similarity is not None and self._user_items is not None
        return np.asarray((self._user_items[user] @ self._similarity).todense()).ravel()


@dataclass
class ALSRecommender(Recommender):
    """Implicit-feedback matrix factorisation (alternating least squares).

    Uses the `implicit` library's conjugate-gradient ALS. Not TensorFlow or
    PyTorch: this is a least-squares problem with a closed-form update and no
    need for autograd, and a deep-learning dependency would cost ~600MB, slower
    CI and the free-tier deployment for no measurable gain at this scale. See
    docs/decisions.md #3.
    """

    factors: int = 64
    regularization: float = 0.05
    iterations: int = 20
    alpha: float = 40.0
    seed: int = 0
    name: str = "als"
    _model: object | None = None
    _user_items: sparse.csr_matrix | None = None

    def fit(self, data: Interactions) -> ALSRecommender:
        from implicit.als import AlternatingLeastSquares

        matrix = data.to_csr()
        self._user_items = matrix

        model = AlternatingLeastSquares(
            factors=self.factors,
            regularization=self.regularization,
            iterations=self.iterations,
            random_state=self.seed,
            # Threads pinned to 1 so results are reproducible: the parallel
            # solver sums float updates in a nondeterministic order, which
            # makes a "deterministic" ranking quietly untrue.
            num_threads=1,
            calculate_training_loss=False,
        )
        # alpha scales the confidence of an observed interaction, which is what
        # distinguishes implicit ALS from plain least squares on a binary matrix.
        model.fit((matrix * self.alpha).astype(np.float32), show_progress=False)
        self._model = model
        return self

    def score_all(self, user: int) -> np.ndarray:
        assert self._model is not None
        return self._model.user_factors[user] @ self._model.item_factors.T

    def fold_in(self, item_ids: list[int]) -> np.ndarray:
        """A user factor for someone not in the training matrix.

        Least squares against the fixed item factors. This is what makes a
        member who onboarded five minutes ago scoreable without retraining --
        the cold-start path described in docs/recommender.md.
        """
        assert self._model is not None
        if not item_ids:
            return np.zeros(self._model.item_factors.shape[1], dtype=np.float32)

        factors = np.asarray(self._model.item_factors[item_ids], dtype=np.float64)
        confidence = self.alpha
        gram = factors.T @ factors * confidence
        gram += np.eye(gram.shape[0]) * self.regularization
        target = factors.sum(axis=0) * confidence
        return np.linalg.solve(gram, target).astype(np.float32)


# ---------------------------------------------------------------- hybrid --


@dataclass
class HybridRecommender(Recommender):
    """Weighted combination of component models.

    Scores are rank-percentile normalised per user before combining. Raw scores
    from ALS, item-KNN and popularity live on completely different scales -- a
    dot product of factors, a sum of cosine similarities, and a play count --
    so a weighted sum of the raw values is really a weighted sum of whichever
    component happens to have the largest numbers. Percentiles make the weights
    mean what they say.

    This is the same normalisation the group layer depends on in M4, for a
    related reason: comparing members is only meaningful once their scores are
    on one scale.
    """

    components: list[tuple[Recommender, float]] = field(default_factory=list)
    name: str = "hybrid"

    def fit(self, data: Interactions) -> HybridRecommender:
        for model, _ in self.components:
            model.fit(data)
        return self

    def score_all(self, user: int) -> np.ndarray:
        total: np.ndarray | None = None
        for model, weight in self.components:
            if weight == 0:
                continue
            contribution = _rank_percentile(model.score_all(user)) * weight
            total = contribution if total is None else total + contribution
        if total is None:
            raise ValueError("a hybrid needs at least one component with a non-zero weight")
        return total

    def contributions(self, user: int) -> dict[str, np.ndarray]:
        """Per-component normalised scores, for explanations.

        The explanation text in M4 is built from these named terms rather than
        written next to the ranking, which is what makes it checkable.
        """
        return {
            model.name: _rank_percentile(model.score_all(user)) * weight
            for model, weight in self.components
        }


# --------------------------------------------------------------- helpers --


def _rank_percentile(scores: np.ndarray) -> np.ndarray:
    """Map scores to [0, 1] by rank. Ties share the average rank.

    Rank rather than min-max because min-max is destroyed by a single outlier,
    and score distributions here are long-tailed by construction.
    """
    scores = np.asarray(scores, dtype=np.float64)
    n = scores.size
    if n == 0:
        return scores
    if n == 1:
        return np.ones(1)

    order = np.argsort(scores, kind="stable")
    ranks = np.empty(n, dtype=np.float64)
    ranks[order] = np.arange(n, dtype=np.float64)

    # Average the ranks of tied values, so that a model returning the same score
    # for everything (popularity, for a cold item) does not impose an arbitrary
    # ordering that later looks like a real preference.
    unique, inverse, counts = np.unique(scores, return_inverse=True, return_counts=True)
    if len(unique) < n:
        sums = np.zeros(len(unique), dtype=np.float64)
        np.add.at(sums, inverse, ranks)
        ranks = (sums / counts)[inverse]

    return ranks / (n - 1)


def _truncate_rows(matrix: sparse.csr_matrix, k: int) -> sparse.csr_matrix:
    """Keep only the k largest entries in each row."""
    matrix = matrix.tocsr()
    if k <= 0:
        return matrix

    data, indices, indptr = [], [], [0]
    for row in range(matrix.shape[0]):
        start, stop = matrix.indptr[row], matrix.indptr[row + 1]
        row_data = matrix.data[start:stop]
        row_indices = matrix.indices[start:stop]
        if len(row_data) > k:
            keep = np.argpartition(-row_data, k - 1)[:k]
            row_data, row_indices = row_data[keep], row_indices[keep]
        order = np.argsort(row_indices)
        data.append(row_data[order])
        indices.append(row_indices[order])
        indptr.append(indptr[-1] + len(row_data))

    return sparse.csr_matrix(
        (
            np.concatenate(data) if data else np.zeros(0),
            np.concatenate(indices) if indices else np.zeros(0, dtype=int),
            np.array(indptr),
        ),
        shape=matrix.shape,
    )
