"""CommonGround recommendation engine.

Pure computation. This package takes arrays and dataframes and returns
rankings; it opens no sockets and no database connections. See
docs/architecture.md for why, and tests/test_engine_isolation.py for the test
that keeps it true.

Algorithms land in M3 (individual recommender) and M4 (group ranking).
"""

# Written onto every playlist row, so a saved ranking can be traced to the code
# that produced it. Bump whenever a change could alter output for identical
# inputs -- a scoring change, a new penalty term, a different tiebreak.
ENGINE_VERSION = "0.1.0"

__all__ = ["ENGINE_VERSION"]
