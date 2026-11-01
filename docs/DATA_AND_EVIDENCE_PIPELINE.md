# Data and evidence pipeline (1 Nov 2026)

Three gaps a quant would close first. None of them changes what the system trades: they
change what it records and learns from.

## 1. Option history grows by itself

Before: the recorder kept option ticks (bid/ask, OI, volume) for 30 days, and the archive
command had to be run by hand. A missed month was lost for research.

Now: before the daily tick-retention pass (16:05 IST) deletes anything, the backend runs the
option archive in a separate process (`tick_retention.archive_options`). Every recorded day
not yet archived is added to `OPTION_ARCHIVE_PATH` (default `backups/options_archive.db`):
per-minute NIFTY and BANKNIFTY option quotes with OI and volume, plus spot. If it fails, an
alert fires and the ticks are still in the retention backups, which the archive can read
later. Set `OPTION_ARCHIVE_PATH=` (empty) to switch it off.

To keep the 19 days already archived, copy `research/options_archive.db` to
`try12/backend/backups/options_archive.db` once (or point `OPTION_ARCHIVE_PATH` at it).

## 2. Every refused signal is tested forward (shadow outcomes)

Before: once the risk gate refused a signal (puts only, the SUPERTREND block, the low-volatility
skip, the daily cap, no option, ...), nobody knew what it would have done, so no refusal rule
could ever be proven right or wrong.

Now: signals are saved with their exit plan (`strategy_signals.exit_plan`, migration 0017).
At 15:35 IST, `shadow_outcomes.evaluate_day` replays every refused NIFTY signal's own plan on
that day's real 1-min candles, exactly as the research harness does (fill at the next open,
pessimistic intrabar path, the live exit rules and trails), and stores the net R after the
MEASURED cost model (`shadow_r_net`, `shadow_exit`). It never opens, closes or sizes anything.

`GET /api/v1/analytics/strategy-evidence` returns `shadow`: per strategy and refusing rule,
the count and mean net R. The Trading page's Strategy evidence panel shows it as
"refused: direction policy 12 × -0.18R". How to read it:

* A rule is **working** when what it refuses averages clearly below what the strategy takes.
* A rule is **costing money** when refused signals average above zero. Revisit it then (for
  example, puts only for ORB if refused calls come out positive over 50+ signals).
* Spot-based R is an estimate of the option result, not a fill.

## 3. The iron fly is tested in two shapes

Before: one paper book, 200-point wings.

Now: a second book, `EXPIRY_IRON_FLY_W300` (same rules, 300-point wings), runs beside it in
`paper_spreads`. Research ranked 300-point wings the most cost-robust variant (+0.04R even
with harsh costs; FINDINGS.md section 11). `GET /api/v1/trading/iron-fly?wing=300` shows it;
`wing=200` (the default) is the original. Compare the two after 10+ expiries.

## Before restarting the backend: migrate the database

The live database is at migration 0016; this code needs **0017**. The backend now refuses to
start on an older schema (startup check 8) with the command to run:

```bash
cd try12/backend && python -m alembic upgrade head
```

Migration 0017 only adds four nullable columns to `strategy_signals`; existing rows are
unchanged.
