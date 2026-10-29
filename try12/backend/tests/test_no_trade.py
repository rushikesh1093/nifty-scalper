"""Persisted no-trade decisions and cycle_stats (design §12, R12.1-R12.8)."""
import ast
import logging
import pathlib
import re
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from tests.helpers import IST, make_signal, run, seed_config

APP = pathlib.Path(__file__).resolve().parents[1] / "app"
N_STRATEGIES = 7                       # app.strategies.names.ALL_STRATEGY_NAMES
NO_TRADE = APP / "services" / "trading" / "no_trade.py"

SECTION12 = {
    "CYCLE": {"OUTSIDE_SESSION_WINDOW", "AFTER_ENTRY_CUTOFF", "INSUFFICIENT_CANDLES",
              "ATR_BELOW_FLOOR", "VIX_ABOVE_MAX", "VIX_BELOW_MIN",
              "REGIME_UNAVAILABLE", "REGIME_NO_TRADE", "TRANSITION_COOLDOWN",
              "FEED_UNAVAILABLE"},
    "STRATEGY_GATE": {"STRATEGY_USER_DISABLED", "STRATEGY_REGIME_DISABLED",
                      "STRATEGY_DAILY_HALT", "EXPIRY_DAY_SL_TOO_TIGHT", "STRATEGY_COOLDOWN",
                      "PREV_SESSION_UNAVAILABLE"},
    "SIGNAL_GATE": {"AGGREGATOR_SAME_DIRECTION", "AGGREGATOR_CONFLICT",
                    "REGIME_DIRECTION_MISMATCH", "REGIME_STATE_UNAVAILABLE", "RR_INSUFFICIENT"},
    "RISK_GATE": {"NOT_PAPER_MODE", "KILL_SWITCH", "DAILY_LOSS_CAP", "LOSING_STREAK_HALT",
                  "POST_LOSS_COOLDOWN", "DAILY_TRADE_CAP", "MAX_OPEN_POSITIONS", "EXPOSURE_CAP",
                  "REGIME_MULTIPLIER_ZERO", "CONFIG_INVALID"},
    "SELECTION": {"NO_SUITABLE_EXPIRY", "NO_INSTRUMENTS", "TICK_UNAVAILABLE",
                  "QUOTE_UNAVAILABLE", "QUOTE_STALE", "OI_UNAVAILABLE", "VOLUME_UNAVAILABLE",
                  "LIQUIDITY_TOO_LOW", "VOLUME_INSUFFICIENT", "SPREAD_TOO_WIDE",
                  "SLIPPAGE_TOO_HIGH", "PREMIUM_ABOVE_AFFORDABLE", "CONTRACT_UNSUITABLE",
                  "SCORE_BELOW_MIN", "NO_OPTION_AVAILABLE", "OPTION_MOMENTUM_FAIL",
                  "ATR_VS_SL_DISTANCE"},
    "SIZING": {"KELLY_NO_EDGE", "SIZED_TO_ZERO", "LOT_UNAFFORDABLE", "INSUFFICIENT_CAPITAL",
               "INITIAL_RISK_NON_POSITIVE", "COSTS_EXCEED_EDGE"},
    "ENTRY": {"DUPLICATE_OPEN_POSITION", "CAPITAL_RACE_LOST", "DAILY_TRADE_CAP_RACE", "HALT_ENGAGED_MID_ENTRY",
              "UNEXPECTED_ENTRY_ERROR"},
    "DEGRADATION": {"PERF_WRITE_FAILED", "MTM_DEGRADED", "FALLBACK_FILL", "DATA_GAP",
                    "DETECTOR_ERROR", "REGIME_MULTIPLIER_ERROR"},
}


async def _rows(Session, **where):
    from app.db.models.risk import NoTradeDecision
    async with Session() as db:
        q = select(NoTradeDecision).order_by(NoTradeDecision.id)
        for k, v in where.items():
            q = q.where(getattr(NoTradeDecision, k) == v)
        return (await db.execute(q)).scalars().all()


async def _cycle_stats(Session):
    from app.db.models.risk import CycleStats
    async with Session() as db:
        return (await db.execute(select(CycleStats))).scalars().all()


# ── enumerations and static guards ───────────────────────────────────────────

def test_enums_are_exactly_section12():
    from app.services.trading.no_trade import ReasonCode, Stage
    assert {s.value for s in Stage} == set(SECTION12)
    declared = {c.value for c in ReasonCode}
    expected = set().union(*SECTION12.values())
    assert declared == expected
    assert all(c.name == c.value for c in ReasonCode)


def test_no_trade_imports_nothing_from_strategies_or_sibling_services():
    tree = ast.parse(NO_TRADE.read_text(encoding="utf-8"))
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    assert not [n for n in names if n.startswith("app.strategies")]
    assert not [n for n in names if n.startswith("app.services.trading")
                and n != "app.services.trading.no_trade"]


_REF = re.compile(r"\bReasonCode\.([A-Za-z_][A-Za-z0-9_]*)")


def test_every_referenced_reason_code_is_declared():
    from app.services.trading.no_trade import ReasonCode
    declared = set(ReasonCode.__members__)
    refs: dict[str, list[str]] = {}
    for path in APP.rglob("*.py"):
        if path == NO_TRADE:
            continue
        for m in _REF.finditer(path.read_text(encoding="utf-8")):
            refs.setdefault(m.group(1), []).append(str(path.relative_to(APP)))
    assert refs, "no ReasonCode references found under app/"
    unknown = {k: v for k, v in refs.items() if k not in declared}
    assert not unknown, f"undeclared ReasonCode members referenced: {unknown}"


def test_reference_scan_catches_a_typo(tmp_path):
    from app.services.trading.no_trade import ReasonCode
    src = "x = ReasonCode.ATR_BELOW_FLOR\n"
    assert {m.group(1) for m in _REF.finditer(src)} - set(ReasonCode.__members__) == \
        {"ATR_BELOW_FLOR"}


# ── recorder ─────────────────────────────────────────────────────────────────

def test_record_writes_run_scoped_untruncated_row(runtime_ctx, sqlite_db):
    from app.runtime.context import ctx
    from app.services.trading import no_trade
    from app.services.trading.no_trade import ReasonCode, Stage

    async def _go():
        async with sqlite_db() as db:
            rid = await no_trade.record(db, ctx(), stage=Stage.CYCLE,
                                        code=ReasonCode.ATR_BELOW_FLOOR, reason="r" * 900,
                                        context={"atr": 1.2}, underlying="NIFTY")
            await db.commit()
            return rid

    rid = run(_go())
    (row,) = run(_rows(sqlite_db))
    assert row.id == rid and row.run_id == "PAPER"
    assert row.trading_day == runtime_ctx.clock.trading_day()
    assert row.stage == "CYCLE" and row.code == "ATR_BELOW_FLOOR"
    assert len(row.reason) == 900 and row.context == {"atr": 1.2}


def test_record_degradation_once_dedupes_per_position_and_day(runtime_ctx, sqlite_db):
    from app.services.trading import no_trade
    from app.services.trading.no_trade import ReasonCode

    first = run(no_trade.record_degradation_once("PAPER", ReasonCode.MTM_DEGRADED, position_id=7,
                                                 context={"mark_source": "LAST_KNOWN"}))
    repeats = [run(no_trade.record_degradation_once("PAPER", ReasonCode.MTM_DEGRADED,
                                                    position_id=7)) for _ in range(100)]
    other = run(no_trade.record_degradation_once("PAPER", ReasonCode.MTM_DEGRADED, position_id=8))
    assert first is not None and other is not None and set(repeats) == {None}
    rows = run(_rows(sqlite_db, code="MTM_DEGRADED"))
    assert [r.position_id for r in rows] == [7, 8]
    assert {r.stage for r in rows} == {"DEGRADATION"}


def test_record_degradation_writes_one_row_each(runtime_ctx, sqlite_db):
    from app.services.trading import no_trade
    from app.services.trading.no_trade import ReasonCode

    run(no_trade.record_degradation("PAPER", ReasonCode.FALLBACK_FILL, position_id=3))
    run(no_trade.record_degradation("PAPER", ReasonCode.FALLBACK_FILL, position_id=3))
    assert len(run(_rows(sqlite_db, code="FALLBACK_FILL"))) == 2


# ── SignalAggregator / SignalBus ─────────────────────────────────────────────

def test_should_act_returns_the_rule_that_fired(runtime_ctx):
    from app.services.trading.no_trade import ReasonCode
    from app.strategies.base import Direction, SignalAggregator

    agg = SignalAggregator()
    buy, sell = make_signal(direction=Direction.BUY), make_signal(direction=Direction.SELL)
    assert agg.should_act(buy) == (True, None, "")
    agg.record_acted(buy)
    ok, code, reason = agg.should_act(buy)
    assert (ok, code) == (False, ReasonCode.AGGREGATOR_SAME_DIRECTION) and reason
    ok, code, _ = agg.should_act(sell)
    assert (ok, code) == (False, ReasonCode.AGGREGATOR_CONFLICT)


def test_two_same_direction_signals_in_one_candle_write_one_row(runtime_ctx, sqlite_db, monkeypatch):
    from app.strategies.base import SignalBus

    async def _opened(signal):
        return True

    monkeypatch.setattr(SignalBus, "_handlers", [_opened])
    a = make_signal(strategy="ORB_BREAKOUT")
    b = make_signal(strategy="VWAP_EMA_PULLBACK")
    assert run(SignalBus.publish(a)) is True
    assert run(SignalBus.publish(b)) is False
    rows = run(_rows(sqlite_db))
    assert [(r.stage, r.code, r.strategy_name) for r in rows] == \
        [("SIGNAL_GATE", "AGGREGATOR_SAME_DIRECTION", "VWAP_EMA_PULLBACK")]
    assert rows[0].underlying == "NIFTY" and rows[0].run_id == "PAPER"


def test_publish_returns_false_when_the_recorder_fails(monkeypatch, caplog):
    """No sqlite_db: the session factory raises. publish keeps its contract."""
    from app.strategies.base import SignalBus, signal_aggregator

    async def _opened(signal):
        return True

    monkeypatch.setattr(SignalBus, "_handlers", [_opened])
    sig = make_signal()
    signal_aggregator.record_acted(sig)
    caplog.set_level(logging.WARNING)
    assert run(SignalBus.publish(sig)) is False
    assert any(r.levelno == logging.WARNING and "no-trade" in r.getMessage().lower()
               for r in caplog.records)


# ── orchestrator returns the code ────────────────────────────────────────────

def test_should_allow_signal_returns_a_code(runtime_ctx):
    from app.services.trading.no_trade import ReasonCode
    from app.services.trading.regime_persistence import RegimeState
    from app.services.trading.strategy_orchestrator import StrategyOrchestrator
    from app.strategies.base import Direction
    from tests.test_regime_state_machine import _ind

    orch = StrategyOrchestrator()
    state = RegimeState(regime="BEAR_TREND_NORMAL_VOL", underlying="NIFTY",
                        timestamp=runtime_ctx.clock.now_utc(), indicators=_ind())
    ok, code, reason = orch.should_allow_signal(make_signal(direction=Direction.BUY), state)
    assert (ok, code) == (False, ReasonCode.REGIME_DIRECTION_MISMATCH) and reason
    assert orch.should_allow_signal(make_signal(direction=Direction.SELL), state)[:2] == (True, None)


# ── strategy_runner: CYCLE gates, skip loop, cycle_stats ─────────────────────

class _Stub:
    sl_pct = 0.15

    def __init__(self, name):
        self.name = name
        self.calls = 0

    async def on_candle(self, candles, indicators):
        self.calls += 1


class _NoInstruments:
    async def instruments_for(self, db, underlying):
        return []

    def invalidate(self):
        pass


def _candle(i, start):
    ts = start + timedelta(minutes=i)
    px = 22600.0 + (i % 7)
    return {"timestamp": ts.strftime("%Y-%m-%d %H:%M:%S"), "open": px, "high": px + 3,
            "low": px - 3, "close": px + 1, "volume": 0}


@pytest.fixture
def quiet_runner(runtime_ctx, sqlite_db, fake_redis, monkeypatch):
    """A runner over one stub per strategy (N_STRATEGIES) with every CYCLE gate open."""
    from dataclasses import replace

    from app.core.config import settings
    from app.runtime.context import using
    from app.services.trading import strategy_runner as sr
    from app.strategies.names import ALL_STRATEGY_NAMES

    run(seed_config(sqlite_db))
    monkeypatch.setattr(sr, "MIN_TODAY_CANDLES", 1)
    monkeypatch.setattr(sr, "ATR_MIN_NIFTY", 0.0)
    runner = sr.StrategyRunner()
    stubs = [_Stub(n) for n in ALL_STRATEGY_NAMES]
    runner._strategies["NIFTY"] = stubs

    async def _merge(underlying, candles, indicators, is_expiry_day, flags, history=None):
        return {n: flags.get(n, True) for n in ALL_STRATEGY_NAMES}, None

    monkeypatch.setattr(runner, "_detect_and_merge_regime", _merge)
    rc = replace(runtime_ctx, instruments=_NoInstruments())
    start = datetime(2026, 9, 1, 9, 15)           # naive IST, like the feed

    def _feed(i):
        """Put candles 0..i in Redis and move the clock to candle i's close."""
        fake_redis.kv[f"ohlc:{settings.NIFTY_TOKEN}:ONE_MINUTE"] = \
            [_candle(k, start) for k in range(i + 1)]
        # the bar is closed one minute after it opens: the runner never reads a forming bar
        runtime_ctx.clock.set((start + timedelta(minutes=i + 1)).replace(tzinfo=IST))

    with using(rc):
        yield runner, stubs, _feed


def test_quiet_session_cycle_stats_is_positive_evidence(quiet_runner, sqlite_db):
    """R12.8b: nothing blocks and nothing emits — cycle_stats alone says so."""
    runner, stubs, feed = quiet_runner
    n = 349                                        # bars 09:15 .. 15:03: each closes before the 15:05 cutoff
    for i in range(n):
        feed(i)
        run(runner.run_cycle("NIFTY"))

    (cs,) = run(_cycle_stats(sqlite_db))
    assert (cs.run_id, cs.underlying, str(cs.trading_day)) == ("PAPER", "NIFTY", "2026-09-01")
    assert cs.candles_evaluated == n
    assert cs.strategies_evaluated == N_STRATEGIES * n
    assert cs.signals_emitted == 0
    assert cs.rejections_recorded == 0
    assert cs.first_cycle_at is not None and cs.last_cycle_at > cs.first_cycle_at
    assert all(s.calls == n for s in stubs)
    rejections = [r for r in run(_rows(sqlite_db)) if r.stage != "DEGRADATION"]
    assert rejections == []
    # the three-strategy runner reads no OI/IV context (7840af9): nothing is missing
    assert cs.oi_iv_context_missing == 0
    assert run(_rows(sqlite_db, code="DATA_GAP")) == []


def test_cycle_gate_writes_one_row_per_candle_not_per_strategy(quiet_runner, sqlite_db, monkeypatch):
    from app.services.trading import strategy_runner as sr

    runner, stubs, feed = quiet_runner
    monkeypatch.setattr(sr, "ATR_MIN_NIFTY", 1e9)       # ATR gate always fires
    feed(5)
    run(runner.run_cycle("NIFTY"))
    run(runner.run_cycle("NIFTY"))                       # same candle again: deduped
    feed(6)
    run(runner.run_cycle("NIFTY"))
    rows = run(_rows(sqlite_db, code="ATR_BELOW_FLOOR"))
    assert len(rows) == 2 and {r.stage for r in rows} == {"CYCLE"}
    assert all(s.calls == 0 for s in stubs)
    (cs,) = run(_cycle_stats(sqlite_db))
    assert cs.candles_evaluated == 3 and cs.rejections_recorded == 2
    assert cs.strategies_evaluated == 0


def test_insufficient_candles_and_feed_unavailable(quiet_runner, sqlite_db, monkeypatch, fake_redis):
    from app.core.config import settings
    from app.services.trading import strategy_runner as sr

    runner, _, feed = quiet_runner
    monkeypatch.setattr(sr, "MIN_TODAY_CANDLES", 25)
    feed(3)
    run(runner.run_cycle("NIFTY"))
    fake_redis.kv.pop(f"ohlc:{settings.NIFTY_TOKEN}:ONE_MINUTE")
    run(runner.run_cycle("NIFTY"))
    codes = [r.code for r in run(_rows(sqlite_db)) if r.stage == "CYCLE"]
    assert codes == ["INSUFFICIENT_CANDLES", "FEED_UNAVAILABLE"]


def test_outside_session_window_is_one_row_per_day_and_no_cycle_stats(quiet_runner, sqlite_db,
                                                                       runtime_ctx):
    runner, _, _ = quiet_runner
    runtime_ctx.clock.set(datetime(2026, 9, 1, 8, 0, tzinfo=IST))
    for _ in range(3):
        run(runner.run_cycle("NIFTY"))
        runtime_ctx.clock.advance(60)
    rows = run(_rows(sqlite_db))
    assert [r.code for r in rows] == ["OUTSIDE_SESSION_WINDOW"]
    assert run(_cycle_stats(sqlite_db)) == []


def test_after_entry_cutoff_is_recorded(quiet_runner, sqlite_db, runtime_ctx):
    runner, _, _ = quiet_runner
    runtime_ctx.clock.set(datetime(2026, 9, 1, 15, 10, tzinfo=IST))
    run(runner.run_cycle("NIFTY"))
    assert [r.code for r in run(_rows(sqlite_db))] == ["AFTER_ENTRY_CUTOFF"]


def test_skip_loop_records_user_and_regime_disables_once(quiet_runner, sqlite_db, monkeypatch,
                                                         runtime_ctx):
    from dataclasses import replace

    from app.runtime.context import ctx, using
    from app.runtime.live import FrozenUserFlags
    from app.strategies.names import ALL_STRATEGY_NAMES

    runner, stubs, feed = quiet_runner

    async def _merge(underlying, candles, indicators, is_expiry_day, flags, history=None):
        merged = {n: flags.get(n, True) for n in ALL_STRATEGY_NAMES}
        merged["PIVOT_BOUNCE"] = False                 # regime says no
        return merged, None

    monkeypatch.setattr(runner, "_detect_and_merge_regime", _merge)
    with using(replace(ctx(), user_flags=FrozenUserFlags({"MOMENTUM_BURST": False}))):
        for i in range(3):
            feed(i)
            run(runner.run_cycle("NIFTY"))
    rows = [(r.stage, r.code, r.strategy_name) for r in run(_rows(sqlite_db))
            if r.stage == "STRATEGY_GATE"]
    assert sorted(rows) == [("STRATEGY_GATE", "STRATEGY_REGIME_DISABLED", "PIVOT_BOUNCE"),
                            ("STRATEGY_GATE", "STRATEGY_USER_DISABLED", "MOMENTUM_BURST")]
    (cs,) = run(_cycle_stats(sqlite_db))
    assert cs.strategies_evaluated == 3 * (N_STRATEGIES - 2) and cs.rejections_recorded == 2


def test_cycle_stats_write_failure_never_aborts_the_cycle(quiet_runner, monkeypatch, caplog):
    from app.services.trading import no_trade

    runner, stubs, feed = quiet_runner

    async def _boom(*a, **k):
        raise RuntimeError("cycle_stats write failed")

    monkeypatch.setattr(no_trade, "upsert_cycle_stats", _boom)
    caplog.set_level(logging.WARNING)
    feed(0)
    run(runner.run_cycle("NIFTY"))
    assert all(s.calls == 1 for s in stubs)
    assert any("cycle_stats" in r.getMessage() for r in caplog.records
               if r.levelno == logging.WARNING)


def test_signals_emitted_counts_published_signals(quiet_runner, sqlite_db, monkeypatch):
    from app.strategies.base import SignalBus

    runner, stubs, feed = quiet_runner

    async def _rejected(signal):
        return False

    monkeypatch.setattr(SignalBus, "_handlers", [_rejected])

    async def _emit(candles, indicators):
        await SignalBus.publish(make_signal(strategy="ORB_BREAKOUT"))

    stubs[0].on_candle = _emit
    feed(0)
    run(runner.run_cycle("NIFTY"))
    (cs,) = run(_cycle_stats(sqlite_db))
    assert cs.signals_emitted == 1


# ── completeness, the other direction (plan decision 2, item 6.4) ────────────

def test_every_declared_reason_code_is_referenced():
    """Every declared member has at least one write site (a ReasonCode.<MEMBER>
    reference under app/, outside the declaration itself)."""
    from app.services.trading.no_trade import ReasonCode
    refs = set()
    for path in APP.rglob("*.py"):
        if path == NO_TRADE:
            continue
        refs |= {m.group(1) for m in _REF.finditer(path.read_text(encoding="utf-8"))}
    unreferenced = set(ReasonCode.__members__) - refs
    assert not unreferenced, f"declared but never written: {sorted(unreferenced)}"


def test_a_strategy_in_its_own_cooldown_is_recorded_once_per_episode(
        quiet_runner, sqlite_db, runtime_ctx):
    """STRATEGY_COOLDOWN: BaseStrategy._cooldown_ok is False -> one STRATEGY_GATE
    row per cooldown episode, and the strategy is not counted as evaluated."""
    runner, stubs, feed = quiet_runner
    cooling = {"on": True}
    stubs[0]._cooldown_minutes = 5
    stubs[0]._cooldown_ok = lambda: not cooling["on"]
    for i in range(3):
        feed(i)
        run(runner.run_cycle("NIFTY"))
    cooling["on"] = False
    feed(3)
    run(runner.run_cycle("NIFTY"))
    rows = [(r.code, r.strategy_name) for r in run(_rows(sqlite_db, stage="STRATEGY_GATE"))]
    assert rows == [("STRATEGY_COOLDOWN", stubs[0].name)]
    assert stubs[0].calls == 1                      # evaluated only once the cooldown ended
    (cs,) = run(_cycle_stats(sqlite_db))
    assert cs.strategies_evaluated == 4 * N_STRATEGIES - 3


# ── R12.8a: every gate type fires; the rows alone explain the empty book ────

def test_r12_8a_every_gate_type_fires_and_the_rows_account_for_no_trades(
        quiet_runner, sqlite_db, runtime_ctx, monkeypatch):
    """A synthetic session with zero trades. Each stage-level gate fires at least
    once, and every emitted signal is matched by exactly one signal-level
    rejection row, so the empty book is explained without reading a log."""
    from dataclasses import replace

    from app.db.models.paper_trade import PaperPosition
    from app.runtime.context import ctx, using
    from app.runtime.live import FrozenUserFlags
    from app.services.trading import strategy_runner as sr
    from app.services.trading.mode_manager import mode_manager
    from app.services.trading.wiring import wire_signal_handlers
    from app.strategies.base import Direction, SignalBus
    from tests.helpers import allow_regime, stub_select

    runner, stubs, feed = quiet_runner
    monkeypatch.setattr(sr, "MIN_TODAY_CANDLES", 3)          # candles 0-1: CYCLE gate
    monkeypatch.setattr(SignalBus, "_handlers", [])
    wire_signal_handlers()                                   # the real handle_signal
    allow_regime(monkeypatch)                                # BULL_TREND_NORMAL_VOL: BUY only
    stub_select(monkeypatch, None)                           # selection finds nothing
    script = {3: Direction.BUY, 4: Direction.SELL, 5: Direction.BUY}
    current = {"i": -1}

    async def _emit(candles, indicators):
        d = script.get(current["i"])
        if d is not None:
            await SignalBus.publish(make_signal(strategy="VWAP_EMA_PULLBACK", direction=d))

    emitter = next(s for s in stubs if s.name == "VWAP_EMA_PULLBACK")
    emitter.on_candle = _emit
    with using(replace(ctx(), user_flags=FrozenUserFlags({"ORB_BREAKOUT": False}))):
        for i in range(6):
            current["i"] = i
            if i == 5:
                monkeypatch.setattr(mode_manager, "_halted", True)    # kill switch
            feed(i)
            run(runner.run_cycle("NIFTY"))

    rows = run(_rows(sqlite_db))
    pairs = {(r.stage, r.code) for r in rows}
    assert {s for s, _ in pairs} >= {"CYCLE", "STRATEGY_GATE", "SIGNAL_GATE", "RISK_GATE",
                                     "SELECTION"}
    assert {("CYCLE", "INSUFFICIENT_CANDLES"), ("STRATEGY_GATE", "STRATEGY_USER_DISABLED"),
            ("SELECTION", "NO_OPTION_AVAILABLE"), ("SIGNAL_GATE", "REGIME_DIRECTION_MISMATCH"),
            ("RISK_GATE", "KILL_SWITCH")} <= pairs

    async def _n_positions():
        async with sqlite_db() as db:
            return len((await db.execute(select(PaperPosition))).scalars().all())
    assert run(_n_positions()) == 0
    (cs,) = run(_cycle_stats(sqlite_db))
    signal_level = [r for r in rows
                    if r.stage in ("SIGNAL_GATE", "RISK_GATE", "SELECTION", "SIZING", "ENTRY")]
    assert cs.signals_emitted == len(signal_level) == 3
