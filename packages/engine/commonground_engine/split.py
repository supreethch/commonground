"""Train/test splitting.

Time-ordered, leave-last-N-out per user. Not a random split, and the difference
is not academic: a random split lets a model train on a user's future to predict
their past. Listening data drifts and clusters -- an album release puts fifteen
plays of one artist into one week -- so a random split leaks hard enough to make
the numbers meaningless. It is also the most common way published recommender
results fail to reproduce.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .dataset import Interactions


@dataclass(frozen=True)
class Split:
    train: Interactions
    # Held-out items per user. Users with too little history are absent, not
    # empty: "we could not evaluate this user" and "this user liked nothing"
    # are different facts and averaging over the second would be wrong.
    test: dict[int, np.ndarray]

    @property
    def evaluated_users(self) -> list[int]:
        return sorted(self.test)


def leave_last_n_out(
    data: Interactions,
    n: int = 5,
    min_train_items: int = 5,
) -> Split:
    """Hold out each user's most recent `n` interactions.

    A user is only evaluated if at least `min_train_items` remain for training.
    Evaluating a user with four interactions measures noise, and including them
    mostly measures how many such users the sample happens to contain.

    Ties on timestamp are broken by item id, so the split is deterministic. Real
    dumps contain plenty of identical timestamps -- a bulk import stamps a whole
    library at once -- and without a tiebreak the same input would produce
    different splits on different runs.
    """
    if n < 1:
        raise ValueError("n must be at least 1")

    order = np.lexsort((data.items, data.timestamps, data.users))

    test: dict[int, np.ndarray] = {}
    is_test = np.zeros(len(data), dtype=bool)

    start = 0
    sorted_users = data.users[order]
    while start < len(order):
        stop = start
        while stop < len(order) and sorted_users[stop] == sorted_users[start]:
            stop += 1

        user = int(sorted_users[start])
        positions = order[start:stop]
        if len(positions) >= min_train_items + n:
            held = positions[-n:]
            is_test[held] = True
            test[user] = np.sort(data.items[held])

        start = stop

    return Split(train=data.subset(~is_test), test=test)
