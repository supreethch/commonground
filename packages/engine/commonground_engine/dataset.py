"""Interaction data, in the shape the models want.

One container for the user-item matrix plus the timestamps a time-ordered split
needs. Deliberately dumb: no I/O beyond loading a prepared .npz, no database, no
opinion about where the numbers came from. That is what lets the same code run
over a ListenBrainz slice, a seeded demo database, or a fixture in a test.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy import sparse


@dataclass(frozen=True)
class Interactions:
    """Implicit feedback: a user played an item, once, at a time.

    Stored as parallel arrays (COO-like) rather than a matrix, because the split
    needs per-interaction timestamps and a CSR matrix has nowhere to put them.
    Matrices are built on demand and cached by the caller.
    """

    users: np.ndarray  # int32, row index
    items: np.ndarray  # int32, column index
    timestamps: np.ndarray  # int64, unix seconds
    n_users: int
    n_items: int
    item_labels: list[str] | None = None
    item_artists: list[str] | None = None
    # Per-item tags, when the dataset carries them directly. MovieLens does;
    # the music slice reaches genres through the artist instead, because the
    # listen dump has no per-track tags. Carrying both means the content model
    # can use whichever the dataset actually has rather than assuming an artist
    # is always the route to a genre.
    item_tags: list[list[str]] | None = None

    def __post_init__(self) -> None:
        if not (len(self.users) == len(self.items) == len(self.timestamps)):
            raise ValueError(
                f"users, items and timestamps must align: got {len(self.users)}, "
                f"{len(self.items)}, {len(self.timestamps)}"
            )
        if len(self.users) and (
            self.users.max() >= self.n_users or self.items.max() >= self.n_items
        ):
            raise ValueError("an index exceeds the declared dimensions")

    def __len__(self) -> int:
        return len(self.users)

    @property
    def density(self) -> float:
        return len(self) / max(self.n_users * self.n_items, 1)

    def to_csr(self) -> sparse.csr_matrix:
        """Binary user-item matrix.

        Binary, not play counts: the dataset already collapses repeated plays of
        one item into a single interaction, and a count would in any case be
        dominated by whoever left one album on overnight.
        """
        return sparse.csr_matrix(
            (np.ones(len(self), dtype=np.float32), (self.users, self.items)),
            shape=(self.n_users, self.n_items),
        )

    def item_popularity(self) -> np.ndarray:
        """How many distinct users played each item."""
        return np.bincount(self.items, minlength=self.n_items).astype(np.float64)

    def seen_by_user(self) -> list[set[int]]:
        seen: list[set[int]] = [set() for _ in range(self.n_users)]
        for user, item in zip(self.users, self.items, strict=True):
            seen[user].add(int(item))
        return seen

    def subset(self, mask: np.ndarray) -> Interactions:
        """A new Interactions over the same index space.

        The dimensions are kept deliberately: a train split must address the
        same item ids as the test split, so dropping unused columns here would
        silently renumber everything.
        """
        return Interactions(
            users=self.users[mask],
            items=self.items[mask],
            timestamps=self.timestamps[mask],
            n_users=self.n_users,
            n_items=self.n_items,
            item_labels=self.item_labels,
            item_artists=self.item_artists,
            item_tags=self.item_tags,
        )


def load(directory: str | Path) -> Interactions:
    """Load a dataset written by scripts/build_dataset.py."""
    directory = Path(directory)
    arrays = np.load(directory / "interactions.npz")
    rows = arrays["rows"].astype(np.int32)
    cols = arrays["cols"].astype(np.int32)

    labels: list[str] | None = None
    artists: list[str] | None = None
    tags: list[list[str]] | None = None
    items_path = directory / "items.json"
    if items_path.exists():
        items = json.loads(items_path.read_text())
        labels = [f"{item['artist']} - {item['track']}" for item in items]
        artists = [item["artist"] for item in items]
        if any("genres" in item for item in items):
            tags = [list(item.get("genres") or []) for item in items]

    return Interactions(
        users=rows,
        items=cols,
        timestamps=arrays["timestamps"].astype(np.int64),
        n_users=int(rows.max()) + 1 if len(rows) else 0,
        n_items=len(labels) if labels else (int(cols.max()) + 1 if len(cols) else 0),
        item_labels=labels,
        item_artists=artists,
        item_tags=tags,
    )
