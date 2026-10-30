"""
Offline research harness (no broker, no Redis, no Postgres).

Two-stage evaluation of the live strategies on historical data:

  Experiment A  the live strategy classes, driven minute by minute over
                recorded NIFTY spot candles, and the underlying move after
                each signal (MFE / MAE / R) — does the signal predict?
  Experiment B  the same signals replayed on recorded option quotes (entry at
                the ask, exit at the bid, statutory charges) — what does
                execution cost?

Everything here reads SQLite files opened read-only and writes only to the
output directory it is given. Nothing imports a broker client, and nothing
writes trading_config, Postgres or Redis.
"""
