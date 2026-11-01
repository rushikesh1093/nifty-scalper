# Data quality report (1 Nov 2026)

## Sources

| Data | Source | Kept | Notes |
|---|---|---|---|
| NIFTY / BANKNIFTY spot, VIX, futures 1-min | Feed → Redis (500 candles); Postgres `ohlc` | rolling | Spot has no volume; futures volume used instead |
| Option ticks (LTP, bid/ask, OI, volume) | Feed → Postgres `ticks` | 30 days, then gzip backups | |
| **Option archive** (per-minute quotes, OI, volume) | `app/research/option_archive.py` | **permanent**, auto-built before each retention pass | 19 sessions so far (5-30 Oct 2026) |
| 3-year research history | `research/nifty_history.db` | static | 728 sessions, Oct 2023 - Oct 2026 |
| Chain Greeks | computed from ticks (`chain_snapshots.option_greeks`) | per snapshot | IV / delta implied from the fill, not broker-supplied |
| Market breadth (advance / decline) | **not available** from the feed | — | not added: no source to measure it honestly |

## Measured problems

| Problem | Size | Handling |
|---|---|---|
| Crossed option quotes (bid > ask) | **10 %** of recorded minutes (39,312 of 386,834) | Treated as no valid quote (fill UNAVAILABLE); counted live by data health |
| Out-of-hours option snapshots | evenings, up to 23:24 | Dropped by every loader |
| Thin recording days | 20 and 22 Oct 2026 (feed partly down) | Kept, flagged by day counts |
| Short / holed spot sessions | 7 sessions < 300 min; 797 missing minutes in 58 sessions | Excluded from research; live readiness reports holes |
| VIX gap | 15 Jul - early Aug 2025 | VIX is a recorded feature only |
| Stitched futures history | two tokens, one mislabelled | Highest-volume series per day; ratios only |
| Regime volatility input | 20-minute window (noise) | Fixed to 486 candles (REGIME_REDESIGN.md) |
| Futures candles after the close | cached to 15:39 (spot stops at 15:28), built from after-hours ticks | Found by the live data-health check, 1 Nov. Strategies and research read in-session candles only; not changed |

## Live data-health engine (new)

`app/services/market_data/data_health.py`, `GET /api/v1/market/data-health`, Market page
"Data health" panel. Checks each 15 s on demand:

* **Candles** (NIFTY, BANKNIFTY and their futures): duplicate minutes, impossible OHLC,
  unreadable rows, outlier 1-min moves (> 8 × the typical move and > 0.6 %), age of the newest
  closed candle (FAIL beyond 150 s in session).
* **Quotes** (every subscribed token): no tick, stale (> `stale_tick_seconds`), crossed,
  one-sided, spread > `quote_max_spread_pct`, feed latency p50 / p95 (receive time − exchange
  timestamp; WARN above 3 s).
* Nothing is repaired or replaced. Outside market hours freshness is reported as CLOSED.

Raw data stays immutable (ticks, backups, archive). Derived features are computed on read.
