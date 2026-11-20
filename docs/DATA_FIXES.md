# Data fixes (31 Oct 2026)

## What the data problem actually was

1. **Real option history could never grow.** The live recorder writes every option tick
   (bid/ask, OI, volume) to Postgres, but the retention job keeps only 30 days
   (`TICK_RETENTION_DAYS`) and moves older ticks to gzip backups. Nothing turned those
   ticks into a research-ready history, so real option evidence was capped at about a
   month and earlier days were effectively lost.
2. **The research file was a partial extract:** `live_options.db` held 12 days of NIFTY
   snapshots without OI or volume, plus out-of-hours rows.
3. **The regime's volatility input was computed on a 20-minute window** (see
   REGIME_REDESIGN.md). That was a data-use defect, not missing data.

## What was fixed

### Permanent option archive: `app/research/option_archive.py`

```bash
python -m app.research.option_archive --out ../../../../research/options_archive.db --backups ../../../../try12/backend/backups/ticks
```

* One SQLite file that only grows: one row per contract per in-session minute
  (09:15-15:29 IST) with last LTP, bid, ask, **OI** and cumulative **volume**, for **NIFTY
  and BANKNIFTY** options, plus the spot indices' minute LTP.
* Reads Postgres **read-only** (`SET TRANSACTION READ ONLY`) and the retention job's
  gzip backups. Days already archived are skipped, and `--force` rebuilds them.
  Out-of-hours ticks are dropped. Crossed quotes (bid > ask) are kept as recorded and
  rejected later by the replay.
* First build: **19 sessions, 5-30 Oct 2026** (the old file had 12): 386,834 option-minutes
  (NIFTY 195k, BANKNIFTY 191k), OI on every row, volume on 99 %.
* **Quality finding:** 39,312 minutes (10 %) carry a crossed quote (bid > ask) as recorded
  by the feed. The replay treats them as no valid quote, so a fill there is UNAVAILABLE,
  never assumed. 20 and 22 Oct are thin (feed was partly down). The 29-31 Aug
  backups contain no in-session ticks.
* `app/research/options.Quotes` reads either the old file or the archive (NIFTY by
  default, `underlying=` for BANKNIFTY).
* **Keep it growing:** run the command after each session, or at least every few weeks
  (inside the 30-day window). After ~6 months there will be ~120 sessions of real option
  prices with OI and volume, enough to make Experiment B a real test instead of a cost
  measurement.

### Regime inputs
* ATR-percentile window 20 → 486 candles (validated, REGIME_REDESIGN.md).

## What cannot be fixed in code

* **Expired-contract option history before 5 Oct 2026** is not available from the broker
  API and was not recorded. It cannot be reconstructed, and modelled (Black-Scholes)
  prices are not a substitute for evidence.
* **Spot/VIX gaps in the 3-year history** (VIX missing 15 Jul - early Aug 2025, 7 short
  sessions, 797 missing minutes in 58 sessions) need a broker history call. Running
  `research/backfill_history.py` logs in to AngelOne with your credentials. It was not run
  here, so as not to disturb the live feed's session; run it yourself outside market
  hours if you want those filled. They do not change any conclusion: VIX is used only
  as a recorded feature, and the excluded sessions are 1 % of the history.
