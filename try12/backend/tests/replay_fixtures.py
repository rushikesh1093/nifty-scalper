"""
Shared replay-test datasets (design §14, §18; R13.11).

Everything here runs the PRODUCTION replay path — ReplayEngine.run over a
full-schema scratch SQLite file populated by scripts/make_synthetic_replay_db
.populate. Two test seams are used, and only these two:

  * ``ScriptedStrategy`` — a BaseStrategy subclass that signals at fixed IST
    minutes, so the hand-computed dataset has known entries. It goes through
    BaseStrategy.on_candle, SignalBus, the aggregator, handle_signal, the risk
    gate, the real option selector and the real fills exactly like a
    production strategy (ReplayEngine's ``strategy_factory`` argument).
  * ``pin_regime`` — regime_rules.classify_regime pinned to one regime, so
    the regime map enables the scripted strategies on every candle. The
    detector, the transition logic, the orchestrator merge and the per-candle
    regime_states write all still run.

The four-trade day (R13.11), all premiums identical across the strike grid so
the P&L does not depend on which strike the selector prefers:

  every contract quotes ltp 99.90 / bid 99.85 / ask 99.95 between trades, so
  every entry fills at ceil_tick(99.95 x 1.0005) = 100.00 with stop 85.00,
  target 130.00 (R = 15.00, reward 30.00) and 3 lots of 65 (floor(20,000 / 6,500)).

  T1 10:00 VWAP_EMA_PULLBACK  10:00:59 ltp 84.90             SL      exit 84.80 (bid)
  T2 10:30 SUPERTREND_EMA       115.00 BE -> 123.00 partial 1 lot @ 122.85 + LOCK_40
                          -> 128.00 LOCK_60 (118.00) -> 117.90 SL exit 117.75 (bid)
  T3 11:00 VWAP_EMA_PULLBACK  131.00                          TARGET  exit 129.87
  T4 11:30 SUPERTREND_EMA       80.00                           SL      exit 79.90 (bid)
"""
from __future__ import annotations

import asyncio
import dataclasses
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Callable, Optional

import pytest

from scripts.make_synthetic_replay_db import (
    LOT_SIZE, SyntheticDay, default_quote, populate, weekly_expiries,
)

IST = timezone(timedelta(hours=5, minutes=30))
DAY1 = date(2026, 9, 1)            # Tuesday; the 2026-09-03 weekly expiry is DTE 2
DAY2 = date(2026, 9, 2)
N_CANDLES = 345                    # 09:15 .. 14:59, every close before the 15:05 cutoff
BASE_LTP, HALF_SPREAD = 99.90, 0.05


def run(coro):
    return asyncio.run(coro)


def minute_index(hhmm: str) -> int:
    """Index k of the candle stamped *hhmm* (09:15 is 0). Its tick is at hh:mm:59."""
    h, m = map(int, hhmm.split(":"))
    return (h * 60 + m) - (9 * 60 + 15)


def trend_candles(n: int = N_CANDLES, start: float = 22600.0) -> list[tuple]:
    """A steady drift with a 12.5-point true range, so the spot ATR gates
    (ATR floor 1.5, ATR-vs-SL 7.5) are open on every candle."""
    out = []
    for k in range(n):
        o = start + 0.5 * k
        c = o + 0.5
        out.append((round(o, 2), round(c + 6.0, 2), round(o - 6.0, 2), round(c, 2)))
    return out


def scripted_quote(path: dict, base: float = BASE_LTP) -> Callable:
    """Every contract quotes the same ltp; *path* maps (day, k) or k to a price."""
    def q(day, inst, k, spot):
        ltp = path.get((day, k), path.get(k, base))
        liq = default_quote(day, inst, k, spot)
        return {"ltp": ltp, "bid": round(ltp - HALF_SPREAD, 2), "ask": round(ltp + HALF_SPREAD, 2),
                "oi": liq["oi"], "volume": liq["volume"], "iv": 14.0}
    return q


FOUR_TRADE_PATH = {
    minute_index("10:00"): 84.90,
    minute_index("10:30"): 115.00, minute_index("10:31"): 123.00,
    minute_index("10:32"): 128.00, minute_index("10:33"): 117.90,
    minute_index("11:00"): 131.00,
    minute_index("11:30"): 80.00,
}
FOUR_TRADE_SCHEDULE = {
    "VWAP_EMA_PULLBACK": {"10:00": "BUY", "11:00": "BUY"},
    "SUPERTREND_EMA": {"10:30": "BUY", "11:30": "BUY"},
}


# ── strategies and the regime seam ───────────────────────────────────────────

def scripted_factory(schedule: dict, extra: Optional[Callable] = None) -> Callable:
    """strategy_factory for ReplayEngine: one ScriptedStrategy per schedule entry
    (plus *extra(underlying)*, e.g. real strategies riding along)."""
    from app.runtime.context import now_ist
    from app.strategies.base import BaseStrategy, Direction

    class ScriptedStrategy(BaseStrategy):
        sl_pct = 0.15
        target_pct = 0.30

        def __init__(self, name: str, underlying: str, times: dict):
            self.name = name
            super().__init__(underlying)
            self.times = dict(times)

        async def evaluate(self, candles, indicators):
            d = self.times.get(now_ist().strftime("%H:%M"))
            if d is None:
                return None
            return self._make_signal(Direction(d), float(candles[-1]["close"]), indicators,
                                     notes="scripted")

    def factory(underlying: str):
        strategies = [ScriptedStrategy(n, underlying, t) for n, t in schedule.items()]
        return strategies + (list(extra(underlying)) if extra else [])
    return factory


def pin_regime(mp, regime: str = "BULL_TREND_NORMAL_VOL") -> None:
    import app.services.trading.regime_detector as rd
    mp.setattr(rd, "classify_regime", lambda *a, **k: regime)


def isolate_singletons(mp) -> None:
    """The engine rewires SignalBus and replaces the runner's strategy map;
    keep both from leaking into later tests."""
    from app.services.trading.strategy_runner import strategy_runner
    from app.strategies.base import SignalBus
    mp.setattr(SignalBus, "_handlers", [])
    mp.setattr(strategy_runner, "_strategies", {})


# ── database ─────────────────────────────────────────────────────────────────

def make_db(path):
    """(SessionFactory, async engine, sync url) over a full-schema SQLite file."""
    from app.services.replay.cli import session_factory_for
    url = f"sqlite+aiosqlite:///{path.as_posix()}"
    Session, engine = session_factory_for(url)
    return Session, engine, f"sqlite:///{path.as_posix()}"


def fill_db(sync_url: str, days: list[date], quote_fn: Callable, *,
            candles: Optional[list] = None, prev_day_snapshot: bool = True,
            expiries: Optional[list[date]] = None) -> dict:
    from sqlalchemy import create_engine
    engine = create_engine(sync_url)
    try:
        sdays = [SyntheticDay(d, list(candles or trend_candles())) for d in days]
        return populate(engine, sdays, expiries=expiries or weekly_expiries(days[0], days[-1]),
                        quote_fn=quote_fn, prev_day_snapshot=prev_day_snapshot)
    finally:
        engine.dispose()


def add_rows(Session, *rows) -> None:
    async def _go():
        async with Session() as db:
            for r in rows:
                db.add(r)
            await db.commit()
    run(_go())


def replay(Session, date_from: date, date_to: date, factory: Callable, *,
           overrides: Optional[dict] = None, label: str = "test", out_dir=None,
           iterations: int = 400, seed: int = 20260901, report: bool = False):
    from app.services.replay.engine import ReplayArgs, ReplayEngine
    build_and_write = None
    if report:
        from app.services.replay.report import build_and_write
    # Replay tests script entries at fixed times on hand-built strike ladders; the
    # lunch blackout and the ATM/1-OTM strike limit are tested on their own
    # (test_new_strategies), so they are off here unless a test sets them. So are
    # the evidence gates (test_evidence_gates): scripted trades are not evidence.
    base = {"entry_blackout_windows": [], "max_otm_strikes": 20,
            "evidence_blocked_strategies": [], "evidence_auto_block": False,
            "low_vol_skip_strategies": [], "pe_only_strategies": [],
            "max_option_buys_per_day": 0}
    args = ReplayArgs(date_from=date_from, date_to=date_to, underlyings=("NIFTY",),
                      params_overrides={**base, **(overrides or {})}, seed=seed, label=label,
                      bootstrap_iterations=iterations, out_dir=out_dir)
    eng = ReplayEngine(Session, strategy_factory=factory, report_builder=build_and_write)
    return run(eng.run(args))


def positions(Session, run_id: str) -> list:
    from sqlalchemy import select
    from app.db.models.paper_trade import PaperPosition

    async def _go():
        async with Session() as db:
            return (await db.execute(select(PaperPosition).where(PaperPosition.run_id == run_id)
                                     .order_by(PaperPosition.entry_time))).scalars().all()
    return run(_go())


def config_row(Session):
    from sqlalchemy import select
    from app.db.models.paper_trade import TradingConfig

    async def _go():
        async with Session() as db:
            return (await db.execute(select(TradingConfig))).scalar_one_or_none()
    return run(_go())


# ── the shared four-trade runs (session scope: four replayed days, built once) ─

@pytest.fixture(scope="session")
def four_trade(tmp_path_factory):
    """Runs, in order:
      run1  DAY1, against DB A, which also holds a PAPER trading_config row
            (capital 200,000, used_capital 0) and one OPEN live PAPER position
            using 60,000 of capital;
      run2  DAY1 again, after mutating that row to capital 50,000 /
            used_capital 150,000 and the three run controls (trading_halted,
            study_phase, square_off_time 09:30) that review R1 found leaking;
      run3  DAY2 only (the shifted window);
      runB  DAY1 against DB B: identical market data, no row, no live position.
    """
    from sqlalchemy import update

    from app.db.models.paper_trade import TradingConfig, TradingMode, PaperPosition
    from app.runtime.live import LiveClock
    from tests.helpers import position_fields, reset_singletons

    from app.services.trading.strategy_runner import strategy_runner

    base = tmp_path_factory.mktemp("four_trade")
    quote = scripted_quote(FOUR_TRADE_PATH)
    # ORB_BREAKOUT is user-disabled through the run's frozen flags; no scripted
    # strategy carries that name, so the flag must not change the four trades.
    factory = scripted_factory(FOUR_TRADE_SCHEDULE)
    flags = {"user_strategy_flags": {"ORB_BREAKOUT": False},
             # the hand-computed expectations use per-lot brokerage (not the
             # default flat per-order model)
             "brokerage_flat_per_order": False}
    out = SimpleNamespace()
    with pytest.MonkeyPatch.context() as mp:
        reset_singletons()
        pin_regime(mp)
        isolate_singletons(mp)

        SessA, engA, urlA = make_db(base / "a.db")
        out.fill = fill_db(urlA, [DAY1, DAY2], quote)
        add_rows(SessA,
                 TradingConfig(id=1, mode=TradingMode.PAPER, live_mode_confirmed=False,
                               capital=200_000.0, used_capital=0.0, max_trades_per_day=6,
                               max_open_positions=3, max_lots_per_trade=3,
                               max_daily_loss=6_000.0, max_daily_loss_pct=3.0,
                               square_off_time="15:15", study_phase=False, strategy_flags={}),
                 PaperPosition(**position_fields(run_id="PAPER", trade_amount_used=60_000.0,
                                                 entry_time=LiveClock().now_utc())))
        out.run1 = replay(SessA, DAY1, DAY1, factory, overrides=flags)
        out.row_after_run1 = config_row(SessA)
        out.strategies_run1 = list(strategy_runner._strategies.get("NIFTY", []))

        async def _mutate():
            async with SessA() as db:
                await db.execute(update(TradingConfig).where(TradingConfig.id == 1)
                                 .values(capital=50_000.0, used_capital=150_000.0,
                                         trading_halted=True, halted_reason="live halt",
                                         study_phase=True, square_off_time="09:30"))
                await db.commit()
        run(_mutate())
        out.run2 = replay(SessA, DAY1, DAY1, factory, overrides=flags)
        out.row_after_run2 = config_row(SessA)
        out.run3 = replay(SessA, DAY2, DAY2, factory, overrides=flags)

        SessB, engB, urlB = make_db(base / "b.db")
        fill_db(urlB, [DAY1, DAY2], quote)
        out.runB = replay(SessB, DAY1, DAY1, factory, overrides=flags)
        reset_singletons()
    out.SessA, out.SessB = SessA, SessB
    out.url_a = f"sqlite+aiosqlite:///{(base / 'a.db').as_posix()}"
    out.url_b = f"sqlite+aiosqlite:///{(base / 'b.db').as_posix()}"
    yield out
    run(engA.dispose())
    run(engB.dispose())


def assert_complete(result) -> None:
    assert result.exit_code == 0 and result.status == "COMPLETE", (
        result.status, result.violations, result.error)


def replace_cfg(rc, **kw):
    return dataclasses.replace(rc, cfg=dataclasses.replace(rc.cfg, **kw))


def ist(day: date, hhmm: str, sec: int = 0) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime(day.year, day.month, day.day, h, m, sec, tzinfo=IST)


__all__ = ["four_trade", "LOT_SIZE"]
