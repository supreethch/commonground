"""Split tests.

The split is the single easiest place to accidentally invent a good result, so
these check the properties that make the evaluation honest rather than just that
the function returns something.
"""

from __future__ import annotations

import numpy as np
import pytest
from commonground_engine.dataset import Interactions
from commonground_engine.split import leave_last_n_out


def make(rows: list[tuple[int, int, int]], n_users: int = 3, n_items: int = 30) -> Interactions:
    users, items, stamps = zip(*rows, strict=True)
    return Interactions(
        users=np.array(users, dtype=np.int32),
        items=np.array(items, dtype=np.int32),
        timestamps=np.array(stamps, dtype=np.int64),
        n_users=n_users,
        n_items=n_items,
    )


def test_the_held_out_items_are_the_most_recent_ones() -> None:
    """The whole point: a model must not train on a user's future."""
    data = make([(0, item, 1000 + item) for item in range(10)])

    result = leave_last_n_out(data, n=3, min_train_items=5)

    assert set(result.test[0]) == {7, 8, 9}
    assert 9 not in set(result.train.items.tolist())


def test_training_keeps_everything_that_was_not_held_out() -> None:
    data = make([(0, item, 1000 + item) for item in range(10)])

    result = leave_last_n_out(data, n=3, min_train_items=5)

    assert sorted(result.train.items.tolist()) == list(range(7))
    assert len(result.train) == 7


def test_a_user_without_enough_history_is_absent_rather_than_empty() -> None:
    """'We could not evaluate this user' and 'this user liked nothing' are
    different facts, and averaging over the second would be wrong."""
    data = make(
        [(0, item, 1000 + item) for item in range(10)]
        + [(1, item, 2000 + item) for item in range(4)]
    )

    result = leave_last_n_out(data, n=3, min_train_items=5)

    assert 0 in result.test
    assert 1 not in result.test
    # Their interactions still train the model, they are just not scored.
    assert (result.train.users == 1).sum() == 4


def test_the_split_is_deterministic_when_timestamps_tie() -> None:
    """Bulk imports stamp a whole library at once, so ties are the normal case.

    Without an explicit tiebreak the same input would produce different splits
    on different runs, and every 'reproducible' result built on it would be a
    lie.
    """
    rows = [(0, item, 5000) for item in range(10)]
    first = leave_last_n_out(make(rows), n=3, min_train_items=5)
    shuffled = list(reversed(rows))
    second = leave_last_n_out(make(shuffled), n=3, min_train_items=5)

    assert set(first.test[0]) == set(second.test[0])


def test_train_and_test_never_overlap() -> None:
    data = make([(u, item, 1000 + item) for u in range(3) for item in range(12)])

    result = leave_last_n_out(data, n=4, min_train_items=5)

    for user, held in result.test.items():
        trained = set(result.train.items[result.train.users == user].tolist())
        assert not trained & set(held.tolist()), f"user {user} leaks between train and test"


def test_the_index_space_is_preserved_so_item_ids_still_mean_the_same_thing() -> None:
    data = make([(0, item, 1000 + item) for item in range(10)], n_items=500)

    result = leave_last_n_out(data, n=3, min_train_items=5)

    assert result.train.n_items == 500
    assert result.train.n_users == 3


def test_every_user_with_enough_history_is_evaluated() -> None:
    data = make([(u, item, 1000 + item) for u in range(3) for item in range(9)])

    result = leave_last_n_out(data, n=2, min_train_items=5)

    assert sorted(result.test) == [0, 1, 2]
    assert all(len(held) == 2 for held in result.test.values())


def test_n_must_be_positive() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        leave_last_n_out(make([(0, 1, 1)]), n=0)
