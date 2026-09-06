"""Load the catalogue, fit the model, and generate group playlists.

The engine takes arrays and returns rankings; the database holds rows. This is
the translation, and it is the only place in the API that knows both.

## Why the model is cached

Fitting item-KNN and ALS over the whole catalogue is not a per-request cost. The
catalogue changes when an ingest runs, not when someone opens a room, so the
fitted model is built once and reused, behind a lock because FastAPI runs sync
endpoints in a threadpool and two simultaneous first requests would otherwise
both fit.

`/health` reports how old the snapshot is, and the fit time is measured rather
than assumed -- docs/measurements.md carries the number.

## Cold start

A member who has just onboarded has no row in the fitted matrix. Rather than
refitting the world, their preferences are **folded in**: their onboarding picks
become a pseudo-history over catalogue items, and ALS solves for a user factor
against the fixed item factors. That is the path described in
docs/recommender.md, and it is what makes a room usable five minutes after
someone signs up.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field

import numpy as np
from commonground_engine import ENGINE_VERSION
from commonground_engine import group as group_module
from commonground_engine.dataset import Interactions
from commonground_engine.explain import explain
from commonground_engine.group import GroupScores, ModeConfig
from commonground_engine.recommenders import ALSRecommender, HybridRecommender, ItemKNN
from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger("commonground.recommender")

# The weights M3's sweep selected. Kept here rather than re-fitted at runtime:
# a model whose weights move between deployments cannot be compared to the
# numbers in docs/measurements.md.
HYBRID_WEIGHTS = {"item-knn": 0.15, "als": 0.35}

# Candidate pool sizing.
#
# The veto keeps a track only if *every* member scores it above tau. After
# rank-percentile normalisation tau=0.35 means "in this member's top 65%", so
# for roughly independent tastes the survival rate is 0.65^n -- 42% for a pair,
# 3% for eight, 0.6% for twelve. Measured on the demo catalogue: 2 members left
# 104 survivors, 6 left 29, and **8 left zero, returning an empty playlist**.
#
# Worse, the pool was shrinking as it needed to grow: every member's listening
# history is excluded from it, so a bigger room removed more (282 candidates at
# 6 members, 144 at 12). Both directions are fixed -- the pool scales with the
# room, and RELAXATION_STEPS is the backstop when even that is not enough.
CANDIDATES_PER_MEMBER = 150
POPULARITY_POOL = 150
MIN_POOL_PER_MEMBER = 120
MIN_POOL = 400

# Tried in order when too few candidates survive the veto. Relaxing it is a last
# resort and never silent: the threshold actually used is returned, so the UI can
# say the room was too divided to hold the strict floor rather than quietly
# serving someone a track they would have objected to.
RELAXATION_STEPS = (0.0, -0.10, -0.20, -0.35)


@dataclass
class Snapshot:
    """A fitted model plus the index mappings that make its columns meaningful."""

    model: HybridRecommender
    data: Interactions
    # recording id <-> matrix column, user id <-> matrix row.
    recording_ids: np.ndarray
    recording_to_column: dict[int, int]
    user_to_row: dict[uuid.UUID, int]
    item_artists: dict[int, str]
    item_genres: dict[int, list[str]]
    genre_frequency: dict[str, float]
    built_at: float
    fit_seconds: float
    n_users: int
    n_items: int
    n_interactions: int

    @property
    def age_seconds(self) -> float:
        return time.time() - self.built_at


@dataclass
class GeneratedTrack:
    recording_id: int
    position: int
    group_score: float
    member_scores: dict[str, float]
    contributions: dict[str, float]
    explanation: dict


@dataclass
class Generated:
    tracks: list[GeneratedTrack] = field(default_factory=list)
    mode: str = "consensus"
    seed: int = 0
    engine_version: str = ENGINE_VERSION
    params: dict = field(default_factory=dict)
    duration_ms: int = 0
    candidate_count: int = 0
    vetoed_count: int = 0
    # The veto threshold actually applied, and whether it had to be loosened
    # from the mode's own value in order to fill the playlist.
    tau_applied: float = 0.0
    veto_relaxed: bool = False


class RecommenderService:
    def __init__(self) -> None:
        self._snapshot: Snapshot | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------ loading --

    def snapshot(self, session: Session, *, rebuild: bool = False) -> Snapshot:
        with self._lock:
            if self._snapshot is None or rebuild:
                self._snapshot = self._build(session)
            return self._snapshot

    def invalidate(self) -> None:
        """Drop the cached model. Called after an ingest changes the catalogue."""
        with self._lock:
            self._snapshot = None

    def _build(self, session: Session) -> Snapshot:
        started = time.perf_counter()

        rows = session.execute(
            text(
                """
                SELECT l.user_id, l.recording_id, extract(epoch FROM l.listened_at)::bigint
                FROM listens l
                """
            )
        ).all()

        user_ids = sorted({row[0] for row in rows})
        recording_ids = sorted({row[1] for row in rows})
        user_to_row = {user: index for index, user in enumerate(user_ids)}
        recording_to_column = {rec: index for index, rec in enumerate(recording_ids)}

        data = Interactions(
            users=np.array([user_to_row[r[0]] for r in rows], dtype=np.int32),
            items=np.array([recording_to_column[r[1]] for r in rows], dtype=np.int32),
            timestamps=np.array([r[2] for r in rows], dtype=np.int64),
            n_users=len(user_ids),
            n_items=len(recording_ids),
        )

        metadata = session.execute(
            text(
                """
                SELECT r.id, a.name, coalesce(
                    array_agg(DISTINCT t.name) FILTER (WHERE t.is_genre), '{}'
                )
                FROM recordings r
                JOIN recording_artists ra ON ra.recording_id = r.id
                JOIN artists a ON a.id = ra.artist_id
                LEFT JOIN artist_tags at ON at.artist_id = a.id
                LEFT JOIN tags t ON t.id = at.tag_id
                GROUP BY r.id, a.name
                """
            )
        ).all()
        item_artists = {row[0]: row[1] for row in metadata}
        item_genres = {row[0]: list(row[2] or []) for row in metadata}

        # Share of the catalogue carrying each genre, so the explanation layer
        # can prefer an informative genre over a ubiquitous one.
        genre_counts: dict[str, int] = {}
        for genres in item_genres.values():
            for genre in set(genres):
                genre_counts[genre] = genre_counts.get(genre, 0) + 1
        total_items = max(len(item_genres), 1)
        genre_frequency = {g: c / total_items for g, c in genre_counts.items()}

        model = HybridRecommender(
            components=[
                (ItemKNN(k_neighbours=200), HYBRID_WEIGHTS["item-knn"]),
                (
                    ALSRecommender(factors=64, regularization=0.05, iterations=20, seed=0),
                    HYBRID_WEIGHTS["als"],
                ),
            ],
            name="hybrid-fitted",
        )
        if len(data):
            model.fit(data)

        fit_seconds = time.perf_counter() - started
        logger.info(
            "fitted recommender: %d users, %d items, %d interactions in %.2fs",
            data.n_users,
            data.n_items,
            len(data),
            fit_seconds,
        )
        return Snapshot(
            model=model,
            data=data,
            recording_ids=np.array(recording_ids, dtype=np.int64),
            recording_to_column=recording_to_column,
            user_to_row=user_to_row,
            item_artists=item_artists,
            item_genres=item_genres,
            genre_frequency=genre_frequency,
            built_at=time.time(),
            fit_seconds=fit_seconds,
            n_users=data.n_users,
            n_items=data.n_items,
            n_interactions=len(data),
        )

    # --------------------------------------------------------- generation --

    def _member_scores(
        self, snapshot: Snapshot, session: Session, member_ids: list[uuid.UUID]
    ) -> tuple[np.ndarray, list[set[int]]]:
        """Raw scores per member over every catalogue column, plus what they know.

        A member already in the fitted matrix is scored directly. One who is not
        -- because they signed up after the last fit -- is folded in from their
        onboarding picks, so a brand new account still gets a real ranking
        instead of a popularity fallback.
        """
        n_items = snapshot.data.n_items
        scores: list[np.ndarray] = []
        seen: list[set[int]] = []

        als: ALSRecommender | None = None
        for component, _ in snapshot.model.components:
            if isinstance(component, ALSRecommender):
                als = component

        for member in member_ids:
            row = snapshot.user_to_row.get(member)
            if row is not None:
                scores.append(snapshot.model.score_all(row))
                known = {int(item) for item in snapshot.data.items[snapshot.data.users == row]}
                seen.append(known)
                continue

            # Cold start: fold the onboarding picks in.
            picks = (
                session.execute(
                    text(
                        """
                    SELECT DISTINCT ra.recording_id
                    FROM profile_artists pa
                    JOIN recording_artists ra ON ra.artist_id = pa.artist_id
                    WHERE pa.user_id = :user
                    """
                    ),
                    {"user": member},
                )
                .scalars()
                .all()
            )
            columns = [
                snapshot.recording_to_column[r] for r in picks if r in snapshot.recording_to_column
            ]
            if als is not None and als._model is not None and columns:
                factor = als.fold_in(columns)
                scores.append(factor @ als._model.item_factors.T)
            else:
                # Nothing to go on at all. A flat score is honest: it says the
                # ranking for this member is a prior, not a prediction, and the
                # explanation layer surfaces that rather than inventing taste.
                scores.append(np.zeros(n_items, dtype=np.float64))
            seen.append(set())

        return np.vstack(scores), seen

    def _stated_genres(
        self, session: Session, member_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, set[str]]:
        """Genres a member chose at onboarding, directly or via an artist."""
        if not member_ids:
            return {}
        rows = session.execute(
            text(
                """
                SELECT pt.user_id, t.name
                FROM profile_tags pt
                JOIN tags t ON t.id = pt.tag_id
                WHERE pt.user_id = ANY(:members) AND t.is_genre
                UNION
                SELECT pa.user_id, t.name
                FROM profile_artists pa
                JOIN artist_tags at ON at.artist_id = pa.artist_id
                JOIN tags t ON t.id = at.tag_id
                WHERE pa.user_id = ANY(:members) AND t.is_genre
                """
            ),
            {"members": list(member_ids)},
        ).all()
        found: dict[uuid.UUID, set[str]] = {}
        for user_id, genre in rows:
            found.setdefault(user_id, set()).add(genre)
        return found

    def generate(
        self,
        session: Session,
        *,
        member_ids: list[uuid.UUID],
        member_names: list[str] | None = None,
        mode_name: str = "consensus",
        k: int = 20,
        seed: int = 0,
        exclude_recordings: set[int] | None = None,
    ) -> Generated:
        # The snapshot is fetched *before* the timer starts. Fitting the model
        # is a cache miss, not part of generating a playlist, and charging it to
        # this measurement would put a one-off 0.5s onto whichever request
        # happened to be first -- making rec_runs.duration_ms a record of cache
        # warmth rather than of ranking cost.
        snapshot = self.snapshot(session)
        started = time.perf_counter()
        if snapshot.n_items == 0 or not member_ids:
            return Generated(mode=mode_name, seed=seed)

        mode: ModeConfig = group_module.MODES.get(mode_name, group_module.CONSENSUS)
        raw, seen = self._member_scores(snapshot, session, member_ids)

        popularity = snapshot.data.item_popularity()
        # Per-member depth grows with the room, so the union does not shrink as
        # more histories are excluded from it.
        per_member = max(CANDIDATES_PER_MEMBER, MIN_POOL_PER_MEMBER * len(member_ids))
        candidates: set[int] = set()
        for row in range(raw.shape[0]):
            ranked = np.argsort(-raw[row], kind="stable")[:per_member]
            candidates.update(int(c) for c in ranked)
        candidates.update(int(c) for c in np.argsort(-popularity, kind="stable")[:POPULARITY_POOL])

        already_heard: set[int] = set()
        for known in seen:
            already_heard |= known
        candidates -= already_heard
        if exclude_recordings:
            candidates -= {
                snapshot.recording_to_column[r]
                for r in exclude_recordings
                if r in snapshot.recording_to_column
            }
        # Top up from the wider catalogue if exclusions left the pool thin.
        if len(candidates) < MIN_POOL:
            for column in np.argsort(-popularity, kind="stable"):
                column = int(column)
                if column not in already_heard:
                    candidates.add(column)
                if len(candidates) >= MIN_POOL:
                    break
        if not candidates:
            return Generated(mode=mode.name, seed=seed)

        columns = np.array(sorted(candidates), dtype=np.int64)
        scores = GroupScores(
            matrix=group_module.rank_percentile(raw[:, columns]),
            candidate_ids=columns,
            member_ids=list(range(len(member_ids))),
        )

        candidate_popularity = popularity[columns]
        novelty = group_module.rank_percentile(-candidate_popularity[None, :])[0]
        artists_by_column = {
            int(column): snapshot.item_artists.get(int(snapshot.recording_ids[column]), "")
            for column in columns
        }

        # Relax only as far as needed to fill the playlist, and record where it
        # stopped so the caller can say so.
        playlist = None
        tau_applied = mode.tau_veto
        for adjustment in RELAXATION_STEPS:
            tau_applied = max(0.0, mode.tau_veto + adjustment)
            playlist = group_module.select(
                scores,
                mode.with_params(tau_veto=tau_applied),
                k,
                item_artists=artists_by_column,
                novelty=novelty,
                seed=seed,
            )
            if len(playlist.tracks) >= k:
                break
        assert playlist is not None
        if tau_applied < mode.tau_veto:
            logger.info(
                "relaxed the veto from %.2f to %.2f for a room of %d to fill %d tracks",
                mode.tau_veto,
                tau_applied,
                len(member_ids),
                k,
            )

        # Display names, so a reason reads "closest to Alex's taste" rather than
        # quoting a UUID at someone. Falls back to the id only when the caller
        # did not supply names.
        names = member_names or [str(m) for m in member_ids]
        # Genres a member is known to like, from what they played *and* what they
        # said at onboarding.
        #
        # Listens alone were not enough: a user who has just onboarded has none,
        # so the genre clause never fired and every reason in their playlist fell
        # back to the same "closest to your taste" sentence. That is exactly the
        # user most likely to be looking at this, and twenty identical reasons is
        # worse than no reason at all.
        stated = self._stated_genres(session, member_ids)
        member_genres = [
            {
                genre
                for item in known
                for genre in snapshot.item_genres.get(int(snapshot.recording_ids[item]), [])
            }
            | stated.get(member, set())
            for member, known in zip(member_ids, seen, strict=True)
        ]

        tracks: list[GeneratedTrack] = []
        for track in playlist.tracks:
            recording_id = int(snapshot.recording_ids[track.candidate_id])
            reason = explain(
                track,
                member_names=names,
                item_genres=snapshot.item_genres.get(recording_id, []),
                member_genres=member_genres,
                familiar=[track.candidate_id in known for known in seen],
                tau_veto=mode.tau_veto,
                genre_frequency=snapshot.genre_frequency,
            )
            tracks.append(
                GeneratedTrack(
                    recording_id=recording_id,
                    position=track.position,
                    group_score=track.group_score,
                    member_scores={
                        str(member): float(score)
                        for member, score in zip(member_ids, track.member_scores, strict=True)
                    },
                    contributions=track.contributions,
                    explanation={
                        "sentence": reason.sentence,
                        "clauses": reason.clauses,
                        "sources": reason.sources,
                    },
                )
            )

        return Generated(
            tracks=tracks,
            mode=mode.name,
            seed=seed,
            params={
                "alpha": mode.alpha,
                "tau_veto": mode.tau_veto,
                "lambda_div": mode.lambda_div,
                "lambda_rep": mode.lambda_rep,
                "lambda_nov": mode.lambda_nov,
                "lambda_fair": mode.lambda_fair,
                "weights": HYBRID_WEIGHTS,
            },
            duration_ms=int((time.perf_counter() - started) * 1000),
            candidate_count=len(columns),
            vetoed_count=playlist.n_vetoed,
            tau_applied=tau_applied,
            veto_relaxed=tau_applied < mode.tau_veto,
        )


# One instance per process. The snapshot is a cache, not shared state that
# anything mutates during a request.
recommender_service = RecommenderService()
