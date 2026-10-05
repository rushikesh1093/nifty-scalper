"""
fix_no_trades.py — Auto-fix all config-level blockers that prevent paper trades.

What this script fixes automatically:
  [1]  study_phase = True            → set to False
  [2]  mode != PAPER                 → set to PAPER
  [3]  daily_trades_count at limit   → reset to 0
  [4]  max_daily_loss hit today      → reset daily_trades_count + warn (can't erase P&L)
  [5]  used_capital > actual open positions cost  → reconcile used_capital
  [6]  trade_amount > 40% of capital → reset to 20% of capital (rounded ₹500)
  [7]  trade_amount < ₹1000          → reset to 20% of capital
  [8]  max_open_positions * trade_amount > capital  → fix to 60% deployment rule
  [9]  max_daily_loss < ₹100 or > 20% of capital   → reset to 5% of capital
  [10] max_lots_per_trade < 1        → fix to max(1, floor(capital/10000))
  [11] All strategy_flags disabled   → re-enable all

What it CANNOT fix (requires external action):
  •  No candles in Redis             → market feed must be running
  •  VIX out of range                → market condition, not a config issue
  •  No instruments in DB            → run instrument sync
  •  No option LTPs in Redis         → WebSocket feed must be subscribed

Run:
    python scripts/fix_no_trades.py
    # or with --dry-run to preview changes without writing
    python scripts/fix_no_trades.py --dry-run

Docker:
    docker exec nifty_backend python /app/scripts/fix_no_trades.py
"""
import asyncio
import os
import sys
from datetime import date, datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DRY_RUN = "--dry-run" in sys.argv

from sqlalchemy import select, update, func

from app.db.base import AsyncSessionLocal
from app.db.models.paper_trade import (
    TradingConfig, PaperPosition, DailyPnLSummary, TradingMode,
)
from app.services.cache.redis_client import redis_cache
from app.core.config import settings

IST = timezone(timedelta(hours=5, minutes=30))

# ── helpers ───────────────────────────────────────────────────────────────────

fixes_applied:  list[str] = []
fixes_skipped:  list[str] = []


def _applied(msg: str):
    tag = "[DRY-RUN]" if DRY_RUN else "[FIXED]"
    print(f"  {tag}  {msg}")
    fixes_applied.append(msg)


def _ok(msg: str):
    print(f"  [OK]     {msg}")
    fixes_skipped.append(msg)


def _info(msg: str):
    print(f"  [INFO]   {msg}")


def _warn(msg: str):
    print(f"  [WARN]   {msg}")


def sep(title: str = ""):
    w = 68
    if title:
        pad = max(1, (w - len(title) - 2) // 2)
        print(f"\n{'─'*pad} {title} {'─'*pad}")
    else:
        print("─" * w)


async def _write(db, patches: dict):
    """Apply patches to TradingConfig row 1 unless dry-run."""
    if DRY_RUN:
        return
    await db.execute(
        update(TradingConfig).where(TradingConfig.id == 1).values(**patches)
    )
    await db.commit()


# ═════════════════════════════════════════════════════════════════════════════
async def main():
    ist_now = datetime.now(IST)
    today   = ist_now.date()

    mode_str = "  *** DRY-RUN MODE — no changes will be written ***" if DRY_RUN else ""
    print("\n" + "=" * 68)
    print("  PAPER TRADING — AUTO FIX SCRIPT")
    print(f"  Run at: {ist_now.strftime('%Y-%m-%d %H:%M:%S IST')}")
    if mode_str:
        print(mode_str)
    print("=" * 68)

    await redis_cache.connect()

    # ── Load config ───────────────────────────────────────────────────────────
    async with AsyncSessionLocal() as db:
        cfg = (await db.execute(
            select(TradingConfig).where(TradingConfig.id == 1)
        )).scalar_one_or_none()

    if not cfg:
        print("\n  ❌  No TradingConfig row found.")
        print("      Run the app once (it calls ensure_config() on startup), or:")
        print("      python scripts/seed_config.py")
        await redis_cache.disconnect()
        return

    capital      = cfg.capital or 100_000.0
    trade_amount = cfg.trade_amount or capital * 0.20

    # ─────────────────────────────────────────────────────────────────────────
    # FIX 1 — study_phase
    # ─────────────────────────────────────────────────────────────────────────
    sep("FIX 1 · study_phase")
    if cfg.study_phase:
        _applied("study_phase: True → False  (paper engine will now open positions)")
        async with AsyncSessionLocal() as db:
            await _write(db, {"study_phase": False})
    else:
        _ok("study_phase is already False")

    # ─────────────────────────────────────────────────────────────────────────
    # FIX 2 — mode
    # ─────────────────────────────────────────────────────────────────────────
    sep("FIX 2 · trading mode")
    mode_val = str(cfg.mode).upper()
    if "PAPER" not in mode_val:
        _applied(f"mode: {cfg.mode} → PAPER")
        async with AsyncSessionLocal() as db:
            await _write(db, {"mode": TradingMode.PAPER, "live_mode_confirmed": False})
    else:
        _ok(f"mode is already PAPER")

    # ─────────────────────────────────────────────────────────────────────────
    # FIX 3 — daily_trades_count
    # ─────────────────────────────────────────────────────────────────────────
    sep("FIX 3 · daily trade counter")
    if cfg.daily_trades_count >= cfg.max_trades_per_day:
        _applied(
            f"daily_trades_count: {cfg.daily_trades_count} → 0  "
            f"(was at/over max {cfg.max_trades_per_day})"
        )
        async with AsyncSessionLocal() as db:
            await _write(db, {"daily_trades_count": 0})
    else:
        _ok(f"daily_trades_count = {cfg.daily_trades_count} / {cfg.max_trades_per_day}  (not at limit)")

    # ─────────────────────────────────────────────────────────────────────────
    # FIX 4 — daily loss circuit breaker
    # ─────────────────────────────────────────────────────────────────────────
    sep("FIX 4 · daily loss circuit breaker")
    async with AsyncSessionLocal() as db:
        pnl_r = await db.execute(
            select(func.sum(DailyPnLSummary.net_pnl)).where(
                DailyPnLSummary.date == today,
                DailyPnLSummary.mode == TradingMode.PAPER,
            )
        )
    daily_pnl = pnl_r.scalar() or 0.0
    loss_pct  = abs(daily_pnl) / capital * 100 if capital else 0

    if daily_pnl <= -abs(cfg.max_daily_loss) or loss_pct >= cfg.max_daily_loss_pct:
        _warn(
            f"Daily loss limit breached: today P&L = ₹{daily_pnl:,.0f}  "
            f"limit = ₹{cfg.max_daily_loss:,.0f} / {cfg.max_daily_loss_pct}%"
        )
        _warn("Cannot erase realised P&L — but resetting daily_trades_count")
        _warn("Consider increasing max_daily_loss_pct if this keeps triggering")
        _applied("daily_trades_count → 0  (allows new trades to attempt)")
        async with AsyncSessionLocal() as db:
            await _write(db, {"daily_trades_count": 0})

        # Suggest a better loss limit for the current capital
        better_loss     = round(capital * 0.10 / 100) * 100   # 10% of capital
        better_loss_pct = 10.0
        _info(
            f"Suggestion: set max_daily_loss=₹{better_loss:,.0f} ({better_loss_pct:.0f}% of capital) "
            f"so a single full SL hit doesn't immediately halt trading.\n"
            f"  POST /api/v1/trading/config  "
            f'{{\"max_daily_loss\": {better_loss}, \"max_daily_loss_pct\": {better_loss_pct}}}'
        )
    else:
        _ok(f"Daily P&L = ₹{daily_pnl:,.0f}  (limit not breached)")

    # ─────────────────────────────────────────────────────────────────────────
    # FIX 5 — reconcile used_capital
    # ─────────────────────────────────────────────────────────────────────────
    sep("FIX 5 · reconcile used_capital vs open positions")
    async with AsyncSessionLocal() as db:
        pos_r = await db.execute(
            select(PaperPosition).where(PaperPosition.is_open == True)
        )
        open_positions = pos_r.scalars().all()

    actual_used = sum(p.trade_amount_used or 0.0 for p in open_positions)
    db_used     = cfg.used_capital or 0.0
    diff        = abs(db_used - actual_used)

    _info(f"Open positions: {len(open_positions)}")
    _info(f"DB used_capital:     ₹{db_used:,.2f}")
    _info(f"Actual from pos:     ₹{actual_used:,.2f}")

    if open_positions:
        for p in open_positions:
            _info(f"  pos #{p.id}: {p.symbol:30s}  locked=₹{p.trade_amount_used or 0:,.0f}")

    if diff > 50:   # allow ₹50 rounding tolerance
        _applied(
            f"used_capital: ₹{db_used:,.2f} → ₹{actual_used:,.2f}  "
            f"(drift of ₹{diff:,.2f} — likely from server restart mid-session)"
        )
        async with AsyncSessionLocal() as db:
            await _write(db, {"used_capital": round(actual_used, 2)})
    else:
        _ok(f"used_capital matches open positions (diff=₹{diff:.2f})")

    # ─────────────────────────────────────────────────────────────────────────
    # FIX 6 & 7 — trade_amount sanity
    # ─────────────────────────────────────────────────────────────────────────
    sep("FIX 6–7 · trade_amount vs capital")
    ideal_ta = round(capital * 0.20 / 500) * 500   # 20% of capital, ₹500 steps

    if trade_amount > capital * 0.40:
        _applied(
            f"trade_amount: ₹{trade_amount:,.0f} → ₹{ideal_ta:,.0f}  "
            f"(was >{40}% of capital ₹{capital:,.0f} — would block after 2 positions)"
        )
        trade_amount = ideal_ta
        async with AsyncSessionLocal() as db:
            await _write(db, {"trade_amount": trade_amount})
    elif trade_amount < 1000:
        _applied(
            f"trade_amount: ₹{trade_amount:,.0f} → ₹{ideal_ta:,.0f}  "
            f"(was too low — can't buy 1 lot with ₹{trade_amount:,.0f})"
        )
        trade_amount = ideal_ta
        async with AsyncSessionLocal() as db:
            await _write(db, {"trade_amount": trade_amount})
    else:
        _ok(f"trade_amount = ₹{trade_amount:,.0f}  (~{trade_amount/capital*100:.0f}% of capital)")

        # Warn if trade_amount is less than a typical 1-lot cost
        # NIFTY option near ATM ≈ ₹80–₹200; lot size = 50 → cost = ₹4,000–₹10,000
        nifty_min_lot = 4_000   # rough floor: ₹80 × 50 lots
        if trade_amount < nifty_min_lot:
            _warn(
                f"trade_amount ₹{trade_amount:,.0f} < typical 1-lot cost ₹{nifty_min_lot:,}. "
                f"lot-sizing will always round UP to 1 lot, spending ₹{nifty_min_lot:,}–₹10,000 "
                f"per trade regardless of trade_amount. "
                f"Recommended minimum: ₹{nifty_min_lot:,}"
            )

    # ─────────────────────────────────────────────────────────────────────────
    # FIX 8 — max_open_positions
    # ─────────────────────────────────────────────────────────────────────────
    sep("FIX 8 · max_open_positions")
    ideal_pos = max(2, int(capital / trade_amount * 0.6))

    if (cfg.max_open_positions or 0) < 1:
        _applied(f"max_open_positions: {cfg.max_open_positions} → {ideal_pos}")
        async with AsyncSessionLocal() as db:
            await _write(db, {"max_open_positions": ideal_pos})
    elif (cfg.max_open_positions or 0) * trade_amount > capital:
        _applied(
            f"max_open_positions: {cfg.max_open_positions} → {ideal_pos}  "
            f"(old value would require ₹{cfg.max_open_positions * trade_amount:,.0f} "
            f"but capital is only ₹{capital:,.0f})"
        )
        async with AsyncSessionLocal() as db:
            await _write(db, {"max_open_positions": ideal_pos})
    else:
        _ok(
            f"max_open_positions = {cfg.max_open_positions}  "
            f"(max exposure = ₹{cfg.max_open_positions * trade_amount:,.0f} "
            f"vs capital ₹{capital:,.0f})"
        )

    # ─────────────────────────────────────────────────────────────────────────
    # FIX 9 — max_daily_loss
    # ─────────────────────────────────────────────────────────────────────────
    sep("FIX 9 · max_daily_loss")
    mdl = cfg.max_daily_loss or 0
    ideal_loss     = round(capital * 0.10 / 100) * 100   # 10%
    ideal_loss_pct = 10.0

    if mdl < 100:
        _applied(
            f"max_daily_loss: ₹{mdl:,.0f} → ₹{ideal_loss:,.0f}  "
            f"(was implausibly low)"
        )
        async with AsyncSessionLocal() as db:
            await _write(db, {"max_daily_loss": ideal_loss, "max_daily_loss_pct": ideal_loss_pct})
    elif mdl < capital * 0.05:
        # Less than 5% of capital — a single full SL will trip it immediately
        _applied(
            f"max_daily_loss: ₹{mdl:,.0f} → ₹{ideal_loss:,.0f}  "
            f"(was {mdl/capital*100:.1f}% of capital — a single SL hit would "
            f"breach this and halt trading for the day)"
        )
        async with AsyncSessionLocal() as db:
            await _write(db, {"max_daily_loss": ideal_loss, "max_daily_loss_pct": ideal_loss_pct})
    elif mdl > capital * 0.25:
        _warn(
            f"max_daily_loss = ₹{mdl:,.0f} ({mdl/capital*100:.0f}% of capital) — "
            f"very high risk tolerance. Consider ₹{ideal_loss:,.0f} (10%)."
        )
        _ok(f"max_daily_loss left at ₹{mdl:,.0f} (not auto-changed — risk config)")
    else:
        _ok(f"max_daily_loss = ₹{mdl:,.0f}  ({mdl/capital*100:.1f}% of capital)")

    # ─────────────────────────────────────────────────────────────────────────
    # FIX 10 — max_lots_per_trade
    # ─────────────────────────────────────────────────────────────────────────
    sep("FIX 10 · max_lots_per_trade")
    ideal_lots = max(1, min(5, int(capital / 10_000)))
    if (cfg.max_lots_per_trade or 0) < 1:
        _applied(f"max_lots_per_trade: {cfg.max_lots_per_trade} → {ideal_lots}")
        async with AsyncSessionLocal() as db:
            await _write(db, {"max_lots_per_trade": ideal_lots})
    else:
        _ok(f"max_lots_per_trade = {cfg.max_lots_per_trade}")

    # ─────────────────────────────────────────────────────────────────────────
    # FIX 11 — strategy_flags (all disabled = no trades ever)
    # ─────────────────────────────────────────────────────────────────────────
    sep("FIX 11 · strategy_flags")
    flags = cfg.strategy_flags or {}
    disabled = [s for s, v in flags.items() if v is False]
    all_strategies = [
        "ORB_VWAP", "VWAP_PULLBACK", "EMA_VWAP", "SUPERTREND_VWAP",
        "PDH_PDL", "VWAP_REVERSAL", "BOLLINGER_BREAKOUT", "RSI_VWAP",
        "OI_SPIKE", "IV_CRUSH", "CPR_VWAP", "GAP_VWAP_FILL",
    ]

    if not flags:
        _ok("strategy_flags not set — all strategies enabled by default")
    elif len(disabled) == len(all_strategies) and len(disabled) > 0:
        _applied("All strategies were disabled → re-enabling all")
        async with AsyncSessionLocal() as db:
            await _write(db, {"strategy_flags": {s: True for s in all_strategies}})
    elif disabled:
        _warn(f"Some strategies disabled: {disabled}")
        _ok("Not auto-changing partial disable — may be intentional")
    else:
        _ok(f"strategy_flags: {len(flags)} configured, none disabled")

    # ─────────────────────────────────────────────────────────────────────────
    # FINAL STATE — re-read config
    # ─────────────────────────────────────────────────────────────────────────
    sep("FINAL STATE AFTER FIXES")
    async with AsyncSessionLocal() as db:
        cfg2 = (await db.execute(
            select(TradingConfig).where(TradingConfig.id == 1)
        )).scalar_one_or_none()

    if cfg2:
        used2      = cfg2.used_capital or 0
        available2 = (cfg2.capital or 0) - used2
        print(f"  study_phase         = {cfg2.study_phase}")
        print(f"  mode                = {cfg2.mode}")
        print(f"  capital             = ₹{cfg2.capital:,.0f}")
        print(f"  used_capital        = ₹{used2:,.0f}")
        print(f"  available_capital   = ₹{available2:,.0f}")
        print(f"  trade_amount        = ₹{cfg2.trade_amount:,.0f}")
        print(f"  max_open_positions  = {cfg2.max_open_positions}")
        print(f"  max_lots_per_trade  = {cfg2.max_lots_per_trade}")
        print(f"  max_daily_loss      = ₹{cfg2.max_daily_loss:,.0f}  ({cfg2.max_daily_loss_pct}%)")
        print(f"  daily_trades_count  = {cfg2.daily_trades_count} / {cfg2.max_trades_per_day}")
        print(f"  slippage_pct        = {cfg2.slippage_pct}%")
        print(f"  strategy_flags      = {cfg2.strategy_flags or 'all enabled'}")

    # ─────────────────────────────────────────────────────────────────────────
    # SUMMARY
    # ─────────────────────────────────────────────────────────────────────────
    sep("SUMMARY")
    if fixes_applied:
        tag = "Would apply" if DRY_RUN else "Applied"
        print(f"\n  {tag} {len(fixes_applied)} fix(es):")
        for f in fixes_applied:
            print(f"    ✅  {f}")
    else:
        print("\n  No config fixes were needed.")

    print()
    print("  Next steps:")
    print("  1. Run the diagnostic to confirm no remaining blockers:")
    print("       python scripts/debug_no_trades.py")
    print()
    print("  2. If candles/feed are still an issue:")
    print("       GET  /api/v1/market/feed/status      — check WebSocket connection")
    print("       POST /api/v1/market/reseed            — re-seed NIFTY/BANKNIFTY candles")
    print("       POST /api/v1/instruments/sync         — re-download instrument list")
    print()
    print("  3. Watch live logs for trade decisions:")
    print("       docker logs -f nifty_backend 2>&1 | grep -E")
    print("         'OPENED|SKIPPED|OptionSelector|PaperEngine|SignalBus'")
    print()
    if DRY_RUN:
        print("  *** DRY-RUN: re-run without --dry-run to apply fixes ***")

    print("=" * 68)
    await redis_cache.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
