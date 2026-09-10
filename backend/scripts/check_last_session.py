"""
Post-session diagnostic: shows every signal fired today, why each was
skipped or executed, open positions, and config state.
Run:  cd backend && python scripts/check_last_session.py
"""
import asyncio, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date, datetime, timezone, timedelta
from app.db.base import AsyncSessionLocal
from app.db.models.paper_trade import (
    StrategySignal, PaperPosition, TradingConfig, SignalStatus,
)
from app.services.cache.redis_client import redis_cache
from app.core.config import settings
from sqlalchemy import select, func, desc


def sep(t=""):
    w = 68
    if t:
        pad = (w - len(t) - 2) // 2
        print(f"\n{'─'*pad} {t} {'─'*pad}")
    else:
        print("─" * w)


async def main():
    print("\n" + "=" * 68)
    print("  LAST SESSION DIAGNOSTIC")
    print("=" * 68)

    await redis_cache.connect()

    async with AsyncSessionLocal() as db:

        # ── Config ────────────────────────────────────────────────────────────
        sep("CONFIG")
        cfg = (await db.execute(
            select(TradingConfig).where(TradingConfig.id == 1)
        )).scalar_one_or_none()

        if not cfg:
            print("  ❌  NO TradingConfig row found — run ensure_config()")
            return

        available = cfg.capital - (cfg.used_capital or 0.0)
        print(f"  study_phase      : {cfg.study_phase}  {'❌ BLOCKER — no trades placed' if cfg.study_phase else '✅'}")
        print(f"  mode             : {cfg.mode}")
        print(f"  capital          : ₹{cfg.capital:,.0f}")
        print(f"  used_capital     : ₹{cfg.used_capital or 0:,.0f}")
        print(f"  available        : ₹{available:,.0f}  {'❌ too low' if available < cfg.trade_amount * 0.5 else '✅'}")
        print(f"  trade_amount     : ₹{cfg.trade_amount:,.0f}")
        print(f"  daily_trades     : {cfg.daily_trades_count} / {cfg.max_trades_per_day}  {'❌ limit hit' if cfg.daily_trades_count >= cfg.max_trades_per_day else '✅'}")
        print(f"  max_open_pos     : {cfg.max_open_positions}")
        print(f"  max_daily_loss   : ₹{cfg.max_daily_loss:,.0f}")
        print(f"  strategy_flags   : {cfg.strategy_flags or 'all enabled'}")

        # ── Today's signals ───────────────────────────────────────────────────
        today = date.today()
        day_start = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)

        sigs = (await db.execute(
            select(StrategySignal)
            .where(StrategySignal.created_at >= day_start)
            .order_by(StrategySignal.created_at)
        )).scalars().all()

        sep(f"SIGNALS TODAY — {today}  ({len(sigs)} total)")

        if not sigs:
            print("  ⚠️   No signals recorded today.")
            print("  This means either:")
            print("    1. The server was not running during market hours")
            print("    2. All strategy gate checks (ATR/VIX/candle count) blocked every cycle")
            print("    3. No strategy entry condition was met today")
        else:
            # Group by status
            status_counts: dict = {}
            for s in sigs:
                k = str(s.status)
                status_counts[k] = status_counts.get(k, 0) + 1

            print("  Status breakdown:")
            for status, cnt in sorted(status_counts.items()):
                icon = "✅" if "SELECTED" in status else "⚠️ " if "SKIPPED" in status else "📋"
                print(f"    {icon}  {status:20s} : {cnt}")

            print()
            print(f"  {'Time':6s}  {'Strategy':22s}  {'Sym':10s}  {'Dir':5s}  {'Status':12s}  Reason")
            print(f"  {'─'*6}  {'─'*22}  {'─'*10}  {'─'*5}  {'─'*12}  {'─'*40}")
            for s in sigs:
                ist_time = s.created_at.astimezone(
                    timezone(timedelta(hours=5, minutes=30))
                ).strftime("%H:%M")
                direction = str(s.direction).replace("Direction.", "")
                status    = str(s.status).replace("SignalStatus.", "")
                reason    = (s.skip_reason or "")[:55]
                print(f"  {ist_time:6s}  {s.strategy_name:22s}  {s.underlying:10s}  {direction:5s}  {status:12s}  {reason}")

        # ── Skip reason analysis ──────────────────────────────────────────────
        skipped = [s for s in sigs if str(s.status) in ("SignalStatus.SKIPPED", "SKIPPED")]
        if skipped:
            sep(f"SKIP REASON ANALYSIS  ({len(skipped)} skipped)")
            reason_counts: dict = {}
            for s in skipped:
                key = (s.skip_reason or "unknown")[:80]
                reason_counts[key] = reason_counts.get(key, 0) + 1
            for reason, cnt in sorted(reason_counts.items(), key=lambda x: -x[1]):
                print(f"  x{cnt:3d}  {reason}")

        # ── Open positions ────────────────────────────────────────────────────
        open_pos = (await db.execute(
            select(PaperPosition).where(PaperPosition.is_open == True)
        )).scalars().all()

        sep(f"OPEN POSITIONS  ({len(open_pos)})")
        if not open_pos:
            print("  None")
        for p in open_pos:
            print(f"  [{p.id}] {p.strategy_name:22s}  {p.symbol:28s}  entry=₹{p.actual_entry:.1f}  sl=₹{p.stop_loss:.1f}  tgt=₹{p.target:.1f}")

        # ── Positions opened today ────────────────────────────────────────────
        all_pos = (await db.execute(
            select(PaperPosition)
            .where(PaperPosition.entry_time >= day_start)
            .order_by(PaperPosition.entry_time)
        )).scalars().all()

        sep(f"POSITIONS OPENED TODAY  ({len(all_pos)})")
        if not all_pos:
            print("  None — no trades were executed today")
        for p in all_pos:
            status  = "OPEN" if p.is_open else f"CLOSED({p.exit_reason})"
            pnl_str = f"pnl=₹{p.net_pnl:,.0f}" if p.net_pnl is not None else ""
            print(f"  [{p.id}] {p.strategy_name:22s}  {p.symbol:28s}  {status:15s}  {pnl_str}")

        # ── Redis gate checks ─────────────────────────────────────────────────
        sep("LIVE GATE CHECKS (Redis)")

        # VIX
        vix = await redis_cache.get_ltp("26017")
        if vix is None:
            print(f"  VIX             : not in Redis (gate bypassed)")
        elif vix < 11.0:
            print(f"  VIX             : {vix:.1f}  ❌ < 11.0 (VIX_MIN) — all strategies blocked")
        elif vix > 25.0:
            print(f"  VIX             : {vix:.1f}  ❌ > 25.0 (VIX_MAX) — all strategies blocked")
        else:
            print(f"  VIX             : {vix:.1f}  ✅ in range")

        # Candle counts
        for underlying in ["NIFTY", "BANKNIFTY"]:
            token = settings.NIFTY_TOKEN if underlying == "NIFTY" else settings.BANKNIFTY_TOKEN
            all_candles = await redis_cache.get_candles(token, "ONE_MINUTE", 500)
            today_str   = str(datetime.now(timezone(timedelta(hours=5, minutes=30))).date())
            today_c     = [c for c in all_candles if str(c.get("timestamp","")).startswith(today_str)]
            icon = "✅" if len(today_c) >= 25 else "❌"
            print(f"  {underlying:10s} candles : total={len(all_candles):3d}  today={len(today_c):3d}  {icon} {'(< 25 required — all strategies skipped)' if len(today_c) < 25 else ''}")

            if len(today_c) >= 25:
                from app.strategies.indicators import compute_all
                ind  = compute_all(today_c)
                atr_v = ind.get("atr") or 0
                atr_min = 2.5 if underlying == "NIFTY" else 8.0
                icon2 = "✅" if atr_v >= atr_min else "❌"
                print(f"  {underlying:10s} ATR     : {atr_v:.2f}  {icon2} {'(< ' + str(atr_min) + ' — choppy market gate)' if atr_v < atr_min else ''}")

        # LTP keys
        ltp_keys = await redis_cache._redis.keys("ltp:*")
        icon = "✅" if len(ltp_keys) > 0 else "❌"
        print(f"  Option LTPs     : {len(ltp_keys)} keys in Redis  {icon} {'— option selector will reject everything' if len(ltp_keys) == 0 else ''}")

    print()
    print("=" * 68)
    await redis_cache.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
