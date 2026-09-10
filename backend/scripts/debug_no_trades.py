"""
debug_no_trades.py — Full paper-trading diagnostic.
Checks ALL 12 gate layers that can block a trade from opening.

Run (inside the backend container or venv):
    python scripts/debug_no_trades.py

Or from Docker:
    docker exec nifty_backend python /app/scripts/debug_no_trades.py
"""
import asyncio
import os
import sys
from datetime import date, datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select, func, desc

from app.db.base import AsyncSessionLocal
from app.db.models.paper_trade import (
    TradingConfig, PaperPosition, StrategySignal, DailyPnLSummary,
    SignalStatus, TradingMode,
)
from app.db.models.market_study import MarketDailySnapshot
from app.db.models.instrument import Instrument, InstrumentType, OptionType
from app.services.cache.redis_client import redis_cache
from app.services.broker.instrument_sync import get_active_expiries
from app.strategies.indicators import compute_all
from app.core.config import settings

IST = timezone(timedelta(hours=5, minutes=30))

OK   = "✅"
FAIL = "❌"
WARN = "⚠️ "

blockers: list[str] = []
warnings: list[str] = []


def ok(label: str, detail: str = ""):
    line = f"  {OK}  {label}"
    if detail:
        line += f"  →  {detail}"
    print(line)


def fail(label: str, detail: str = ""):
    line = f"  {FAIL}  {label}"
    if detail:
        line += f"  →  {detail}"
    print(line)
    blockers.append(f"{label}: {detail}" if detail else label)


def warn(label: str, detail: str = ""):
    line = f"  {WARN}  {label}"
    if detail:
        line += f"  →  {detail}"
    print(line)
    warnings.append(f"{label}: {detail}" if detail else label)


def sep(title: str = ""):
    w = 70
    if title:
        pad = max(1, (w - len(title) - 2) // 2)
        print(f"\n{'─'*pad} {title} {'─'*pad}")
    else:
        print("─" * w)


# ─────────────────────────────────────────────────────────────────────────────
async def main():
    ist_now = datetime.now(IST)
    today   = ist_now.date()

    print("\n" + "=" * 70)
    print("  PAPER TRADING — NO TRADES DIAGNOSTIC")
    print(f"  Run at: {ist_now.strftime('%Y-%m-%d %H:%M:%S IST')}")
    print("=" * 70)

    await redis_cache.connect()

    # ═════════════════════════════════════════════════════════════════════
    # GATE 1 — Study phase
    # ═════════════════════════════════════════════════════════════════════
    sep("GATE 1 · study_phase flag")
    async with AsyncSessionLocal() as db:
        cfg = (await db.execute(
            select(TradingConfig).where(TradingConfig.id == 1)
        )).scalar_one_or_none()

    if not cfg:
        fail("TradingConfig row missing", "run ensure_config() or POST /api/v1/trading/config")
        print("\n  Cannot continue without config — exiting.")
        return

    if cfg.study_phase:
        fail("study_phase = True",
             "signals are recorded but NO positions will open. "
             "Fix: POST /api/v1/trading/config  {\"study_phase\": false}")
    else:
        ok("study_phase = False", "paper engine will open positions")

    # ═════════════════════════════════════════════════════════════════════
    # GATE 2 — Mode
    # ═════════════════════════════════════════════════════════════════════
    sep("GATE 2 · trading mode")
    if str(cfg.mode).upper() not in ("PAPER", "TRADINGMODE.PAPER"):
        warn(f"mode = {cfg.mode}", "expected PAPER — live mode may behave differently")
    else:
        ok(f"mode = PAPER")

    # ═════════════════════════════════════════════════════════════════════
    # GATE 3 — Capital & trade amount
    # ═════════════════════════════════════════════════════════════════════
    sep("GATE 3 · capital & trade amount")
    capital      = cfg.capital or 0.0
    used         = cfg.used_capital or 0.0
    available    = capital - used
    trade_amount = cfg.trade_amount or 0.0

    print(f"  capital         = ₹{capital:,.0f}")
    print(f"  used_capital    = ₹{used:,.0f}")
    print(f"  available       = ₹{available:,.0f}")
    print(f"  trade_amount    = ₹{trade_amount:,.0f}")
    print(f"  max_open_pos    = {cfg.max_open_positions}")
    print(f"  max_trades/day  = {cfg.max_trades_per_day}")
    print(f"  daily_trades    = {cfg.daily_trades_count}")
    print(f"  max_daily_loss  = ₹{cfg.max_daily_loss:,.0f}  ({cfg.max_daily_loss_pct}%)")
    print(f"  slippage_pct    = {cfg.slippage_pct}%")
    print(f"  max_lots/trade  = {cfg.max_lots_per_trade}")

    if capital < 5000:
        fail("capital too low", f"₹{capital:,.0f} — minimum viable is ₹5,000")
    elif trade_amount > capital * 0.50:
        fail("trade_amount > 50% of capital",
             f"₹{trade_amount:,.0f} vs capital ₹{capital:,.0f} — "
             "only 1 trade possible and it locks >50% of capital. "
             f"Fix: set trade_amount to ≈ ₹{round(capital*0.20/500)*500:,.0f}")
    elif trade_amount < 1000:
        fail("trade_amount too low", f"₹{trade_amount:,.0f} — too small to buy 1 lot")
    else:
        ok(f"trade_amount = ₹{trade_amount:,.0f}",
           f"~{trade_amount/capital*100:.0f}% of capital")

    if available < trade_amount * 0.5:
        fail("available capital too low",
             f"₹{available:,.0f} < half of trade_amount ₹{trade_amount:,.0f}. "
             "pre_trade_check() will block all trades. "
             "Fix: close open positions or add capital")
    else:
        ok(f"available = ₹{available:,.0f}")

    # ═════════════════════════════════════════════════════════════════════
    # GATE 4 — Daily limits
    # ═════════════════════════════════════════════════════════════════════
    sep("GATE 4 · daily limits")

    # Daily loss
    async with AsyncSessionLocal() as db:
        pnl_r = await db.execute(
            select(func.sum(DailyPnLSummary.net_pnl)).where(
                DailyPnLSummary.date == today,
                DailyPnLSummary.mode == TradingMode.PAPER,
            )
        )
        daily_pnl = pnl_r.scalar() or 0.0

        open_r = await db.execute(
            select(func.count(PaperPosition.id)).where(PaperPosition.is_open == True)
        )
        open_count = open_r.scalar() or 0

    if daily_pnl <= -abs(cfg.max_daily_loss):
        fail("Daily loss limit BREACHED",
             f"today P&L = ₹{daily_pnl:,.0f}  limit = ₹{-cfg.max_daily_loss:,.0f}. "
             "Circuit breaker is ON — no new trades until midnight. "
             "Fix: POST /api/v1/trading/reset_daily_counter  (resets counter only, "
             "does NOT fix the loss)")
    else:
        ok(f"Daily P&L = ₹{daily_pnl:,.0f}",
           f"limit ₹{cfg.max_daily_loss:,.0f} — {abs(daily_pnl)/cfg.max_daily_loss*100:.0f}% used")

    # Max trades per day
    if cfg.daily_trades_count >= cfg.max_trades_per_day:
        fail("Daily trade count limit hit",
             f"{cfg.daily_trades_count} / {cfg.max_trades_per_day}. "
             "Fix: POST /api/v1/trading/reset_daily_counter")
    else:
        ok(f"daily_trades = {cfg.daily_trades_count} / {cfg.max_trades_per_day}")

    # Max open positions
    if open_count >= cfg.max_open_positions:
        fail("Max open positions reached",
             f"{open_count} / {cfg.max_open_positions}. "
             "No new trades until one closes.")
    else:
        ok(f"open_positions = {open_count} / {cfg.max_open_positions}")

    # Daily loss % check
    loss_pct = abs(daily_pnl) / capital * 100 if capital else 0
    if loss_pct >= cfg.max_daily_loss_pct:
        fail("Daily loss % limit breached",
             f"{loss_pct:.2f}% >= limit {cfg.max_daily_loss_pct}%")
    else:
        ok(f"Daily loss % = {loss_pct:.2f}% / {cfg.max_daily_loss_pct}%")

    # ═════════════════════════════════════════════════════════════════════
    # GATE 5 — Market hours
    # ═════════════════════════════════════════════════════════════════════
    sep("GATE 5 · market hours (IST)")
    ist_mins = ist_now.hour * 60 + ist_now.minute
    in_session = 9 * 60 + 15 <= ist_mins <= 15 * 60 + 25
    if not in_session:
        warn("Outside market hours",
             f"current IST = {ist_now.strftime('%H:%M')} — "
             "strategy runner only runs 09:15–15:25. "
             "This is expected if you are running outside market hours.")
    else:
        mins_since_open = ist_mins - (9 * 60 + 15)
        ok(f"In market session — {mins_since_open} min since open",
           f"current IST = {ist_now.strftime('%H:%M')}")
        if mins_since_open < 25:
            warn("Less than 25 minutes since open",
                 f"only {mins_since_open} candles could exist — "
                 "strategy runner needs 25 today-candles before any strategy runs")

    # ═════════════════════════════════════════════════════════════════════
    # GATE 6 — Candle data in Redis
    # ═════════════════════════════════════════════════════════════════════
    sep("GATE 6 · candle data in Redis")
    today_str = str(today)

    for underlying in ["NIFTY", "BANKNIFTY"]:
        token = settings.NIFTY_TOKEN if underlying == "NIFTY" else settings.BANKNIFTY_TOKEN
        all_c = await redis_cache.get_candles(token, "ONE_MINUTE", 500)

        today_c = []
        for c in all_c:
            ts = str(c.get("timestamp", ""))
            if not ts.startswith(today_str):
                continue
            try:
                hh = int(ts[11:13]); mm = int(ts[14:16])
                if hh * 60 + mm < 9 * 60 + 15:
                    continue
            except Exception:
                pass
            today_c.append(c)

        label = f"{underlying} (token {token})"
        if len(all_c) == 0:
            fail(f"{label}: 0 candles total",
                 "market feed is not writing to Redis. "
                 "Check: GET /health — is feed_connected=true?")
        elif len(today_c) < 25:
            fail(f"{label}: only {len(today_c)} today-candles (need ≥ 25)",
                 f"total in Redis = {len(all_c)}. "
                 "Strategy runner exits early — no strategy runs. "
                 "Fix: wait for market to open, or POST /api/v1/market/reseed")
        else:
            latest_ts = today_c[-1].get("timestamp", "?")
            ok(f"{label}: {len(today_c)} today-candles", f"latest = {latest_ts}")

            # ATR check (strategy-level, tighter than runner-level)
            ind = compute_all(today_c)
            atr_v = ind.get("atr") or 0.0
            runner_min = 1.5 if underlying == "NIFTY" else 5.0
            strat_min  = 3.0 if underlying == "NIFTY" else 10.0

            if atr_v < runner_min:
                fail(f"{underlying} ATR runner gate",
                     f"ATR = {atr_v:.2f} < {runner_min} (strategy_runner global gate). "
                     "ALL strategies skipped this cycle.")
            elif atr_v < strat_min:
                warn(f"{underlying} ATR strategy gate",
                     f"ATR = {atr_v:.2f} — passes runner gate (≥{runner_min}) "
                     f"but fails _atr_ok() in each strategy (≥{strat_min}). "
                     "Most strategies will return None this cycle.")
            else:
                ok(f"{underlying} ATR = {atr_v:.2f}", "passes both ATR gates")

    # ═════════════════════════════════════════════════════════════════════
    # GATE 7 — VIX
    # ═════════════════════════════════════════════════════════════════════
    sep("GATE 7 · India VIX")
    VIX_MIN = 10.0
    VIX_MAX = 25.0
    vix = await redis_cache.get_ltp(settings.VIX_TOKEN)
    if vix is None:
        warn("VIX not in Redis",
             "VIX gate will be BYPASSED — strategies run regardless of VIX. "
             "This is acceptable but worth fixing: ensure VIX token is subscribed.")
    elif vix < VIX_MIN:
        fail(f"VIX = {vix:.1f} < {VIX_MIN} (VIX_MIN)",
             "all strategies blocked — market too calm, options too cheap. "
             "Fix: nothing you can do, just wait for volatility.")
    elif vix > VIX_MAX:
        fail(f"VIX = {vix:.1f} > {VIX_MAX} (VIX_MAX)",
             "all strategies blocked — extreme fear/erratic market. "
             "Fix: wait for VIX to drop below 25.")
    else:
        ok(f"VIX = {vix:.1f}", f"in range [{VIX_MIN}, {VIX_MAX}]")

    # ═════════════════════════════════════════════════════════════════════
    # GATE 8 — Strategy flags
    # ═════════════════════════════════════════════════════════════════════
    sep("GATE 8 · strategy enable flags")
    flags = cfg.strategy_flags or {}
    EXPECTED_STRATEGIES = [
        "ORB_VWAP", "VWAP_PULLBACK", "EMA_VWAP", "SUPERTREND_VWAP",
        "PDH_PDL", "VWAP_REVERSAL", "BOLLINGER_BREAKOUT", "RSI_VWAP",
        "OI_SPIKE", "IV_CRUSH", "CPR_VWAP", "GAP_VWAP_FILL",
    ]
    disabled = [s for s, v in flags.items() if v is False]
    if not flags:
        ok("All 12 strategies enabled", "strategy_flags is empty (all default to True)")
    elif len(disabled) == len(EXPECTED_STRATEGIES):
        fail("ALL strategies disabled in strategy_flags",
             f"flags = {flags}. Fix: POST /api/v1/trading/strategy_flag")
    elif disabled:
        warn(f"{len(disabled)} strategies disabled: {disabled}",
             "remaining strategies still active")
    else:
        ok(f"strategy_flags set — {len(flags)} configured, none disabled")

    # ═════════════════════════════════════════════════════════════════════
    # GATE 9 — Instruments in DB
    # ═════════════════════════════════════════════════════════════════════
    sep("GATE 9 · instrument data in DB")
    async with AsyncSessionLocal() as db:
        for underlying in ["NIFTY", "BANKNIFTY"]:
            inst_r = await db.execute(
                select(func.count(Instrument.id)).where(
                    Instrument.name == underlying,
                    Instrument.instrument_type == InstrumentType.OPTIDX,
                    Instrument.is_active == True,
                    Instrument.expiry >= today,
                )
            )
            count = inst_r.scalar() or 0

            expiries = await get_active_expiries(db, underlying)

            if count == 0:
                fail(f"{underlying}: 0 active option instruments in DB",
                     "option selector will return None for every signal. "
                     "Fix: POST /api/v1/instruments/sync  (re-download from AngelOne)")
            else:
                exp_str = str(expiries[0]) if expiries else "none"
                ok(f"{underlying}: {count:,} active option instruments",
                   f"nearest expiry = {exp_str}")

                # Check if nearest expiry has CE+PE at ATM ± 5 strikes
                if expiries:
                    nifty_ltp = await redis_cache.get_ltp(
                        settings.NIFTY_TOKEN if underlying == "NIFTY" else settings.BANKNIFTY_TOKEN
                    )
                    if nifty_ltp:
                        step = 50 if underlying == "NIFTY" else 100
                        atm  = round(nifty_ltp / step) * step
                        strike_r = await db.execute(
                            select(func.count(Instrument.id)).where(
                                Instrument.name == underlying,
                                Instrument.instrument_type == InstrumentType.OPTIDX,
                                Instrument.expiry == expiries[0],
                                Instrument.is_active == True,
                                Instrument.strike >= atm - 5 * step,
                                Instrument.strike <= atm + 5 * step,
                            )
                        )
                        sc = strike_r.scalar() or 0
                        if sc < 4:
                            fail(f"{underlying}: only {sc} strikes near ATM ({atm:.0f})",
                                 "option selector needs CE+PE across ±5 strikes. "
                                 "Fix: re-sync instruments")
                        else:
                            ok(f"{underlying}: {sc} strikes available near ATM {atm:.0f}")

    # ═════════════════════════════════════════════════════════════════════
    # GATE 10 — Option LTPs in Redis
    # ═════════════════════════════════════════════════════════════════════
    sep("GATE 10 · option LTPs in Redis")
    try:
        ltp_keys = await redis_cache._redis.keys("ltp:*")
        tick_keys = await redis_cache._redis.keys("tick:*")
        print(f"  ltp:* keys  = {len(ltp_keys)}")
        print(f"  tick:* keys = {len(tick_keys)}")

        if len(ltp_keys) == 0:
            fail("0 option LTP keys in Redis",
                 "option selector fetches live LTP from Redis. "
                 "Without it, every candidate has ltp=None → hard filter rejects all. "
                 "Fix: ensure WebSocket feed is subscribed to option tokens "
                 "(market_manager._build_watchlist must run after instrument sync). "
                 "Check: GET /api/v1/market/feed/status")
        else:
            # Sample a few
            sample = ltp_keys[:5]
            samples = []
            for k in sample:
                v = await redis_cache._redis.get(k)
                key_str = k.decode() if isinstance(k, bytes) else k
                samples.append(f"{key_str}={v.decode() if isinstance(v, bytes) else v}")
            ok(f"{len(ltp_keys)} LTP keys present", "  ".join(samples[:3]))

        if len(tick_keys) == 0:
            warn("0 tick:* keys in Redis",
                 "tick data (bid/ask/OI/volume) used for hard filters. "
                 "Option selector will fall back to ltp-only path but "
                 "spread and OI hard filters will be based on estimated values.")
        else:
            ok(f"{len(tick_keys)} tick keys present")
    except Exception as e:
        warn(f"Could not list Redis keys: {e}")

    # ═════════════════════════════════════════════════════════════════════
    # GATE 11 — Signal aggregator cooldown
    # ═════════════════════════════════════════════════════════════════════
    sep("GATE 11 · today's signals & skip reasons")
    day_start = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
    async with AsyncSessionLocal() as db:
        sigs = (await db.execute(
            select(StrategySignal)
            .where(StrategySignal.created_at >= day_start)
            .order_by(StrategySignal.created_at)
        )).scalars().all()

    total_sigs    = len(sigs)
    selected_sigs = [s for s in sigs if str(s.status) in ("SELECTED", "SignalStatus.SELECTED")]
    skipped_sigs  = [s for s in sigs if str(s.status) in ("SKIPPED",  "SignalStatus.SKIPPED")]

    print(f"  Total signals today   : {total_sigs}")
    print(f"  SELECTED (traded)     : {len(selected_sigs)}")
    print(f"  SKIPPED               : {len(skipped_sigs)}")

    if total_sigs == 0:
        fail("No signals at all today",
             "Strategies never fired. Root causes:\n"
             "     • Server not running during market hours\n"
             "     • Fewer than 25 today-candles (Gates 5/6)\n"
             "     • ATR gate blocked every cycle (Gate 6)\n"
             "     • VIX gate blocked every cycle (Gate 7)\n"
             "     • None of the 12 strategy conditions were met")
    else:
        ok(f"{total_sigs} signals fired today")
        if len(selected_sigs) == 0 and total_sigs > 0:
            fail("Signals fired but NONE resulted in a trade",
                 "all were SKIPPED — see skip reasons below")

        # Skip reason breakdown
        if skipped_sigs:
            print()
            reason_counts: dict[str, int] = {}
            for s in skipped_sigs:
                key = (s.skip_reason or "unknown")[:90]
                reason_counts[key] = reason_counts.get(key, 0) + 1

            print(f"  Top skip reasons ({len(skipped_sigs)} skipped):")
            for reason, cnt in sorted(reason_counts.items(), key=lambda x: -x[1]):
                icon = FAIL if cnt >= 3 else WARN
                print(f"    {icon} x{cnt:3d}  {reason}")

        # Recent signals table
        if sigs:
            print()
            print(f"  {'Time':6}  {'Strategy':22}  {'Underlying':10}  {'Dir':5}  {'Status':10}  Skip reason")
            print(f"  {'─'*6}  {'─'*22}  {'─'*10}  {'─'*5}  {'─'*10}  {'─'*40}")
            for s in sigs[-20:]:
                ist_t = s.created_at.astimezone(IST).strftime("%H:%M")
                direction = str(s.direction).replace("Direction.", "").replace("SignalDirection.", "")
                status    = str(s.status).replace("SignalStatus.", "")
                reason    = (s.skip_reason or "")[:50]
                print(f"  {ist_t:6}  {s.strategy_name:22}  {s.underlying:10}  {direction:5}  {status:10}  {reason}")

    # ═════════════════════════════════════════════════════════════════════
    # GATE 12 — Previous-day snapshot (PDH/CPR/GAP strategies)
    # ═════════════════════════════════════════════════════════════════════
    sep("GATE 12 · previous-day snapshots (PDH / CPR / GAP strategies)")
    async with AsyncSessionLocal() as db:
        for underlying in ["NIFTY", "BANKNIFTY"]:
            prev = (await db.execute(
                select(MarketDailySnapshot)
                .where(
                    MarketDailySnapshot.underlying == underlying,
                    MarketDailySnapshot.date < today,
                )
                .order_by(desc(MarketDailySnapshot.date))
                .limit(1)
            )).scalar_one_or_none()

            if not prev:
                warn(f"{underlying}: no previous-day snapshot",
                     "PDH_PDL, CPR_VWAP, GAP_VWAP_FILL will not fire today. "
                     "The other 9 strategies are unaffected.")
            else:
                ok(f"{underlying} snapshot: {prev.date}",
                   f"H={prev.high:.0f} L={prev.low:.0f} C={prev.close:.0f}")

    # ═════════════════════════════════════════════════════════════════════
    # SUMMARY
    # ═════════════════════════════════════════════════════════════════════
    sep("SUMMARY")
    if blockers:
        print(f"\n  {FAIL} {len(blockers)} BLOCKER(S) FOUND — nothing will trade until these are fixed:\n")
        for i, b in enumerate(blockers, 1):
            print(f"  [{i}] {b}")
        print()
        print("  Run:  python scripts/fix_no_trades.py")
        print("  to auto-fix any config-level blockers.")
    else:
        print(f"\n  {OK} No config/data blockers found!")
        print()
        if warnings:
            print(f"  {WARN} {len(warnings)} warnings (non-blocking):")
            for w in warnings:
                print(f"    •  {w}")
            print()
        print("  If trades are still not opening:")
        print("  1. Check the backend logs for '[PaperEngine]' and '[OptionSelector]' lines")
        print("  2. Run this during market hours (09:15–15:25 IST)")
        print("  3. Verify strategy entry conditions are being met (momentum/volume)")
        print("  4. Check logs for 'ATR check failed' or 'No option available'")
        print()
        print("  Quick log grep (inside container):")
        print("    docker logs nifty_backend 2>&1 | grep -E 'SKIPPED|OPENED|OptionSelector|PaperEngine' | tail -50")

    print("\n" + "=" * 70)
    await redis_cache.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
