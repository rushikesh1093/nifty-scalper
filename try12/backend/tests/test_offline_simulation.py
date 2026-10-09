"""
OFFLINE end-to-end simulation (no network, no production DB/Redis):

synthetic 1-min NIFTY candles → compute_all → RegimeDetector (real classifier)
→ StrategyOrchestrator → SignalBus → PaperEngine.handle_signal → PaperPosition
→ option price walk via _check_position → TARGET close → P&L verified by hand.
"""
import random
from datetime import datetime, time as dtime, timedelta

import pytest
from sqlalchemy import select

from app.db.models.paper_trade import DailyPnLSummary, PaperPosition
from app.services.trading import paper_engine as pe_mod
from app.services.trading.option_selector import option_selector
from app.core.trading_params import TradingParams
from app.services.trading import fills
from app.services.trading.paper_engine import paper_engine
from app.services.trading.regime_detector import regime_detector
from app.services.trading.regime_rules import get_regime_rule
from app.services.trading.strategy_orchestrator import strategy_orchestrator
from app.services.trading.strategy_runner import strategy_runner
from app.strategies.base import Direction, SignalBus
from app.strategies.indicators import compute_all
from app.runtime.context import ctx, trading_day, using
from tests.helpers import (
    fake_candidate, make_signal, pin_ist_time, run, seed_config, stub_select, with_cfg,
)


def _synthetic_candles(n=60, seed=7):
    rnd = random.Random(seed)
    start = datetime.now().replace(hour=9, minute=15, second=0, microsecond=0)
    price, out = 22600.0, []
    for i in range(n):
        drift = 1.2 + rnd.uniform(-2.5, 2.5)          # gentle uptrend with noise
        o = price
        c = o + drift
        h = max(o, c) + rnd.uniform(0.5, 3.0)
        lo = min(o, c) - rnd.uniform(0.5, 3.0)
        out.append({
            "timestamp": (start + timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:%S"),
            "open": round(o, 2), "high": round(h, 2), "low": round(lo, 2),
            "close": round(c, 2), "volume": 0,
        })
        price = c
    return out


def test_offline_signal_to_closed_position(sqlite_db, fake_redis, monkeypatch, capsys):
    run(seed_config(sqlite_db))
    pin_ist_time(monkeypatch, dtime(11, 0))
    monkeypatch.setattr(SignalBus, "_handlers", [paper_engine.handle_signal])
    monkeypatch.setattr(strategy_orchestrator, "get_current_regime",
                        regime_detector.get_current_regime)   # in-memory, no Redis

    # ── 1. Candles → detector → orchestrator, candle by candle ───────────────
    candles = _synthetic_candles()
    regimes, active = [], 0
    for n in range(25, len(candles) + 1):
        window = candles[:n]
        ind = compute_all(window)
        flags, state = run(strategy_runner._detect_and_merge_regime(
            "NIFTY", window, ind, False, {},
        ))
        run(strategy_runner._persist_regime_state("NIFTY", state))   # as _run_underlying does
        regimes.append(state.regime)
        if any(flags.values()) and not strategy_orchestrator.is_in_cooldown("NIFTY"):
            active += 1

    final = regimes[-1]
    rule = get_regime_rule(final, state.indicators.atr_percentile)
    print(f"\n[sim] regimes: {regimes}")
    print(f"[sim] final={final} active_candles={active}/{len(regimes)} "
          f"enabled={sorted(rule.enabled_strategies)} dirs={sorted(rule.allowed_directions)}")
    assert final not in ("REGIME_TRANSITION", "EXPIRY_DAY")
    assert active >= len(regimes) // 2
    assert rule.enabled_strategies and rule.allowed_directions

    # regime_states rows were persisted through the real session path
    from app.db.models.regime import RegimeStateModel

    async def _n_states():
        async with sqlite_db() as db:
            return len((await db.execute(select(RegimeStateModel))).scalars().all())
    assert run(_n_states()) == len(regimes)

    # ── 2. Signal → SignalBus → PaperEngine → PaperPosition ──────────────────
    strategy = sorted(rule.enabled_strategies)[0]
    direction = Direction.BUY if "BUY" in rule.allowed_directions else Direction.SELL
    cand = fake_candidate(ltp=12.0, lot_size=65)   # cheap enough for any regime multiplier

    stub_select(monkeypatch, cand)

    sig = make_signal(strategy=strategy, direction=direction, sl_pct=0.15, target_pct=0.30)
    one_lot = with_cfg(ctx(), max_lots_per_trade=1)     # the walk below is hand-computed on 1 lot
    with using(one_lot):
        assert run(SignalBus.publish(sig)) is True

    async def _open():
        async with sqlite_db() as db:
            return (await db.execute(select(PaperPosition))).scalars().all()
    [pos] = run(_open())
    assert pos.is_open and pos.entry_regime == final
    entry, sl0, tgt, qty = pos.actual_entry, pos.initial_stop_loss, pos.target, pos.quantity
    assert entry == round(12.0 * 1.002, 2)            # crossed/missing quote → LTP + 0.2%
    assert sl0 == pos.stop_loss and qty == 65
    print(f"[sim] OPENED {strategy} {direction.value} entry={entry} SL={sl0} TGT={tgt} qty={qty}")

    # ── 3. Option price walk → trailing → TARGET ─────────────────────────────
    price, sl_seen = entry, []
    while True:
        price = round(price + 0.25, 2)
        fake_redis.put_tick(cand.token, price, age_s=1)
        p = run(_open())[0]
        if not p.is_open:
            break
        run(paper_engine._check_position(p, dtime(11, 0), dtime(15, 15)))
        sl_seen.append(run(_open())[0].stop_loss)
        assert price < tgt + 1, "position never closed"
    assert sl_seen == sorted(sl_seen) and sl_seen[-1] > sl0   # trailed up monotonically

    closed = run(_open())[0]
    exit_fill = round(tgt * 0.999, 2)
    gross = round((exit_fill - entry) * qty, 2)
    brokerage = 2 * fills.brokerage(1, TradingParams())
    charges = fills.statutory_charges(entry * qty, exit_fill * qty, brokerage, TradingParams())
    expected_net = round(gross - brokerage - charges, 2)
    print(f"[sim] CLOSED reason={closed.exit_reason} exit={closed.actual_exit} "
          f"gross={closed.gross_pnl} brokerage={closed.brokerage} "
          f"charges={closed.statutory_charges} net={closed.net_pnl} (expected {expected_net})")

    assert closed.exit_reason == "TARGET"
    assert closed.actual_exit == exit_fill
    assert closed.gross_pnl == gross
    assert closed.brokerage == brokerage
    assert closed.statutory_charges == charges
    assert closed.net_pnl == expected_net

    async def _summary():
        async with sqlite_db() as db:
            return (await db.execute(select(DailyPnLSummary))).scalars().all()
    [s] = run(_summary())
    assert s.net_pnl == expected_net and s.total_trades == 1 and s.winning_trades == 1
    # R against the initial risk written once at entry (design §8.4)
    assert pos.initial_risk == pytest.approx(round(entry - sl0, 2))
    assert closed.r_multiple == pytest.approx((exit_fill - entry) / pos.initial_risk)
    assert s.avg_rr == pytest.approx(closed.r_multiple)

    # capital in use and the trade count are DERIVED from positions (design §9.2)
    from app.services.trading import risk_state

    async def _derived():
        async with sqlite_db() as db:
            return (await risk_state.open_capital_used(db, "PAPER"),
                    await risk_state.derived_trade_count(db, "PAPER", trading_day()))
    assert run(_derived()) == (0.0, 1)
