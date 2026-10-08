"""
Runtime context (design §14.1).

Every clock read, market-data read, DB session and operator-owned number that
trading decision code uses resolves through the bound ``RuntimeContext``. Live
paper trading binds ``live_context()``; the replay engine binds its own. The
decision code is the same in both — there is no replay branch in it.
"""
