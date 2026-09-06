"""CommonGround recommendation engine.

Pure computation. This package takes arrays and dataframes and returns
rankings; it opens no sockets and no database connections. See
docs/architecture.md for why, and tests/test_engine_isolation.py for the test
that keeps it true.

M3 delivers the individual recommender and its evaluation. The group ranking
layer -- aggregation, fairness, vetoes, explanations -- lands in M4.
"""

from . import dataset, evaluate, metrics, recommenders, split

# Written onto every playlist row, so a saved ranking can be traced to the code
# that produced it. Bump whenever a change could alter output for identical
# inputs -- a scoring change, a new penalty term, a different tiebreak.
ENGINE_VERSION = "0.2.0"

__all__ = [
    "ENGINE_VERSION",
    "dataset",
    "evaluate",
    "metrics",
    "recommenders",
    "split",
]
