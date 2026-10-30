"""Migrations 0010-0013: model columns round-trip on an isolated SQLite DB,
the revision chain, the §11 column contract, the 0013 guard predicates, and a
real downgrade/upgrade of 0011-0013 through alembic Operations on SQLite."""
import importlib.util
import pathlib
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import Float, Numeric, select

from tests.helpers import add_position, get_config_row, get_position, run, seed_config

VERSIONS = pathlib.Path(__file__).resolve().parents[1] / "migrations" / "versions"
NEW_REVISIONS = (
    ("0011", "0011_run_id_and_r_accounting.py"),
    ("0012", "0012_risk_and_replay_tables.py"),
    ("0013", "0013_config_params_and_renames.py"),
)


def _load(filename: str):
    path = VERSIONS / filename
    spec = importlib.util.spec_from_file_location(f"m_{path.stem}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── 0010 (baseline) ──────────────────────────────────────────────────────────

def test_new_columns_round_trip(sqlite_db):
    run(seed_config(sqlite_db, trading_halted=True, halted_reason="test",
                    halted_at=datetime(2026, 10, 6, 4, 0, tzinfo=timezone.utc),
                    daily_trades_date=date(2026, 10, 6)))
    pid = run(add_position(sqlite_db, initial_stop_loss=85.0, statutory_charges=12.5))

    cfg = run(get_config_row(sqlite_db))
    assert cfg.trading_halted is True
    assert cfg.halted_reason == "test"
    assert cfg.daily_trades_date == date(2026, 10, 6)

    pos = run(get_position(sqlite_db, pid))
    assert pos.initial_stop_loss == 85.0
    assert pos.statutory_charges == 12.5


def test_defaults_backward_compatible(sqlite_db):
    run(seed_config(sqlite_db))
    pid = run(add_position(sqlite_db, initial_stop_loss=None, statutory_charges=None))
    cfg = run(get_config_row(sqlite_db))
    assert cfg.trading_halted is False
    assert cfg.daily_trades_date is None
    pos = run(get_position(sqlite_db, pid))
    assert pos.initial_stop_loss is None


def test_migration_0010_chain():
    mod = _load("0010_live_fixes_risk_columns.py")
    assert mod.revision == "0010"
    assert mod.down_revision == "0009"


# ── 0011-0013: chain ─────────────────────────────────────────────────────────

def test_migration_chain_0010_to_0013():
    prev = "0010"
    for rev, filename in NEW_REVISIONS:
        mod = _load(filename)
        assert mod.revision == rev
        assert mod.down_revision == prev
        assert callable(mod.upgrade) and callable(mod.downgrade)
        prev = rev
    heads = sorted(p.name for p in VERSIONS.glob("*.py"))
    assert heads[-1] == "0015_paper_spreads.py"


def test_migration_0014_adds_the_exit_plan_column():
    mod = _load("0014_position_exit_plan.py")
    assert (mod.revision, mod.down_revision) == ("0014", "0013")
    assert callable(mod.upgrade) and callable(mod.downgrade)
    from app.db.models.paper_trade import PaperPosition
    assert PaperPosition.__table__.c.exit_plan.nullable is True


def test_migration_0015_creates_paper_spreads():
    mod = _load("0015_paper_spreads.py")
    assert (mod.revision, mod.down_revision) == ("0015", "0014")
    assert callable(mod.upgrade) and callable(mod.downgrade)
    from app.db.models.paper_spread import PaperSpread
    cols = set(PaperSpread.__table__.c.keys())
    assert {"strategy_name", "trading_day", "legs", "credit_pts", "max_loss", "net_pnl"} <= cols


def test_exit_plan_round_trips(sqlite_db):
    plan = {"strategy": "ORB_BREAKOUT", "side": "BUY", "spot_entry": 22075.0,
            "spot_stop": 22030.0, "spot_target": 22120.0, "t1_done": False}
    pid = run(add_position(sqlite_db, exit_plan=plan))
    assert run(get_position(sqlite_db, pid)).exit_plan == plan
    assert run(get_position(sqlite_db, run(add_position(sqlite_db)))).exit_plan is None


# ── §11 column contract on the ORM ───────────────────────────────────────────

S11_COLUMNS = {
    "paper_positions": {
        "run_id", "original_quantity", "original_lots", "initial_risk", "r_source",
        "exit_value_weighted", "r_multiple", "r_multiple_net", "target_stretched",
        "entry_fill_source", "exit_fill_source", "dte_at_entry", "expiry_band",
        "entry_regime_multiplier", "partial_skipped", "hold_minutes", "sizing_detail",
        "entry_indicators",
    },
    "strategy_signals": {
        "run_id", "expiry_band", "expiry_reason", "selection_rejections",
        "score_breakdown", "no_trade_decision_id",
    },
    "regime_states": {"run_id"},
    "daily_pnl_summary": {
        "run_id", "statutory_charges", "losing_trades", "flat_trades", "sum_r", "sum_r_win",
        "sum_r_loss", "r_sample_size", "expectancy_r", "expectancy_inr", "gross_profit",
        "gross_loss", "max_equity_drawdown", "worst_trade_loss",
    },
    "strategy_perf": {
        "losing_trades", "flat_trades", "sum_r", "sum_r_win", "sum_r_loss", "r_sample_size",
        "winning_trades_r", "losing_trades_r", "avg_win_r", "avg_loss_r", "expectancy_r",
        "expectancy_inr", "gross_profit", "gross_loss", "run_id",
    },
    "regime_performance": {
        "flat_trades", "sum_r", "sum_r_win", "sum_r_loss", "r_sample_size", "avg_r_multiple",
        "expectancy_r", "expectancy_inr", "gross_profit", "gross_loss", "run_id", "avg_rr",
    },
    "trading_config": {
        "open_exposure_cap_pct", "fixed_fraction_pct", "kelly_multiplier",
        "kelly_fraction_cap_pct", "kelly_min_trades", "losing_streak_halt_n",
        "strategy_streak_halt_n", "post_loss_cooldown_minutes", "min_rr",
        "stale_tick_seconds", "entry_cutoff_buffer_min",
    },
    "no_trade_decisions": {
        "id", "run_id", "trading_day", "decided_at", "stage", "code", "underlying",
        "strategy_name", "signal_id", "position_id", "reason", "context",
    },
    "risk_halts": {
        "id", "run_id", "trading_day", "halt_type", "scope", "reason", "context",
        "engaged_at", "released_at", "released_by", "release_note",
    },
    "position_events": {
        "id", "position_id", "run_id", "event_type", "occurred_at", "price",
        "from_value", "to_value", "tier_label", "detail",
    },
    "cycle_stats": {
        "id", "run_id", "trading_day", "underlying", "candles_evaluated",
        "strategies_evaluated", "signals_emitted", "rejections_recorded",
        "oi_iv_context_missing", "first_cycle_at", "last_cycle_at",
    },
    "replay_runs": {
        "run_id", "created_at", "date_from", "date_to", "underlyings", "params",
        "params_hash", "seed", "code_version", "status", "coverage", "metrics",
        "validation", "notes",
    },
}

FLOAT_METRICS = {
    "sum_r", "sum_r_win", "sum_r_loss", "expectancy_r", "expectancy_inr", "gross_profit",
    "gross_loss", "avg_win_r", "avg_loss_r", "avg_r_multiple", "max_equity_drawdown",
    "worst_trade_loss", "initial_risk", "r_multiple", "r_multiple_net", "exit_value_weighted",
}


def _metadata():
    import app.db.models  # noqa: F401  (registers every model)
    from app.db.base import Base
    return Base.metadata


def test_orm_declares_every_section11_column():
    md = _metadata()
    for table, cols in S11_COLUMNS.items():
        assert table in md.tables, f"{table} missing from ORM metadata"
        missing = cols - set(md.tables[table].c.keys())
        assert not missing, f"{table} missing {sorted(missing)}"
    assert "max_drawdown" not in md.tables["daily_pnl_summary"].c


def test_new_metric_columns_are_float_never_numeric():
    md = _metadata()
    for table, cols in S11_COLUMNS.items():
        for name in cols & FLOAT_METRICS:
            col = md.tables[table].c[name]
            assert isinstance(col.type, Float), f"{table}.{name} is {col.type!r}"
            assert not (isinstance(col.type, Numeric) and not isinstance(col.type, Float)), \
                f"{table}.{name} is Numeric"
            assert col.type.asdecimal is False, f"{table}.{name} returns Decimal"


def _unique_sets(table) -> set[tuple[str, ...]]:
    from sqlalchemy import UniqueConstraint
    out = {tuple(c.name for c in uc.columns) for uc in table.constraints
           if isinstance(uc, UniqueConstraint)}
    out |= {tuple(c.name for c in ix.columns) for ix in table.indexes if ix.unique}
    return out


def test_unique_keys_include_run_id():
    md = _metadata()
    assert ("run_id", "underlying", "timestamp") in _unique_sets(md.tables["regime_states"])
    assert ("underlying", "timestamp") not in _unique_sets(md.tables["regime_states"])
    assert ("strategy_name", "run_id") in _unique_sets(md.tables["strategy_perf"])
    assert ("strategy_name",) not in _unique_sets(md.tables["strategy_perf"])
    assert ("regime", "underlying", "strategy_name", "run_id") in \
        _unique_sets(md.tables["regime_performance"])
    assert ("date", "strategy_name", "mode", "run_id") in \
        _unique_sets(md.tables["daily_pnl_summary"])
    assert ("run_id", "trading_day", "underlying") in _unique_sets(md.tables["cycle_stats"])
    assert ("run_id", "trading_day", "halt_type", "scope") in _unique_sets(md.tables["risk_halts"])


def test_degradation_once_predicate_matches_migration():
    from app.db.models.risk import DEGRADATION_ONCE_WHERE
    m12 = _load("0012_risk_and_replay_tables.py")
    assert m12._DEGRADATION_ONCE == DEGRADATION_ONCE_WHERE
    assert "MTM_DEGRADED" in DEGRADATION_ONCE_WHERE     # FALLBACK_FILL may repeat per position


def test_trade_amount_is_nullable():
    md = _metadata()
    assert md.tables["trading_config"].c["trade_amount"].nullable is True


# ── ORM round trip of every new column ───────────────────────────────────────

def test_section11_columns_round_trip(sqlite_db):
    from app.db.models.paper_trade import DailyPnLSummary, StrategyPerf, StrategySignal, TradingMode
    from app.db.models.regime import RegimePerformanceModel, RegimeStateModel
    from app.db.models.replay import ReplayRun
    from app.db.models.risk import CycleStats, NoTradeDecision, PositionEvent, RiskHalt
    from app.db.models.paper_trade import SignalDirection, SignalStatus

    ts = datetime(2026, 9, 1, 4, 30, tzinfo=timezone.utc)
    run(seed_config(sqlite_db, fixed_fraction_pct=12.5, kelly_min_trades=40, min_rr=2.5))
    pid = run(add_position(
        sqlite_db, run_id="R1", original_quantity=130, original_lots=2, initial_risk=15.0,
        r_source="ENTRY", exit_value_weighted=121.0, r_multiple=1.4, r_multiple_net=1.3,
        target_stretched=True, entry_fill_source="ASK", exit_fill_source="BID",
        dte_at_entry=6, expiry_band="2-7", entry_regime_multiplier=0.5, partial_skipped=True,
        hold_minutes=12.5, sizing_detail={"method": "FIXED_FRACTION"},
        entry_indicators={"atr": 3.1},
    ))

    async def _write_and_read():
        async with sqlite_db() as db:
            db.add_all([
                StrategySignal(
                    strategy_name="ORB", underlying="NIFTY", direction=SignalDirection.BUY,
                    status=SignalStatus.SKIPPED, spot_price=1.0, signal_time=ts, run_id="R1",
                    expiry_band="2-7", expiry_reason="nearest band",
                    selection_rejections={"SPREAD_TOO_WIDE": 3}, score_breakdown={"oi": 1.0},
                    no_trade_decision_id=7,
                ),
                RegimeStateModel(underlying="NIFTY", regime="X", timestamp=ts, run_id="R1"),
                DailyPnLSummary(
                    date=date(2026, 9, 1), strategy_name="ORB", mode=TradingMode.PAPER,
                    run_id="R1", statutory_charges=3.5, flat_trades=1, sum_r=1.5, sum_r_win=2.0,
                    sum_r_loss=-0.5, r_sample_size=3, expectancy_r=0.5, expectancy_inr=10.0,
                    gross_profit=50.0, gross_loss=20.0, max_equity_drawdown=20.0,
                    worst_trade_loss=20.0,
                ),
                StrategyPerf(
                    strategy_name="ORB", run_id="R1", losing_trades=1, flat_trades=0,
                    sum_r=1.0, sum_r_win=2.0, sum_r_loss=-1.0, r_sample_size=2,
                    winning_trades_r=1, losing_trades_r=1, avg_win_r=2.0, avg_loss_r=1.0,
                    expectancy_r=0.5, expectancy_inr=5.0, gross_profit=40.0, gross_loss=30.0,
                ),
                RegimePerformanceModel(
                    regime="X", underlying="NIFTY", strategy_name="ORB", run_id="R1",
                    flat_trades=0, sum_r=1.0, sum_r_win=1.0, sum_r_loss=0.0, r_sample_size=1,
                    avg_r_multiple=1.0, expectancy_r=1.0, expectancy_inr=9.0,
                    gross_profit=9.0, gross_loss=0.0,
                ),
                NoTradeDecision(
                    run_id="R1", trading_day=date(2026, 9, 1), decided_at=ts, stage="CYCLE",
                    code="ATR_BELOW_FLOOR", underlying="NIFTY", reason="x" * 1000,
                    context={"atr": 1.0}, position_id=None,
                ),
                RiskHalt(run_id="R1", trading_day=date(2026, 9, 1), halt_type="MANUAL",
                         scope=None, reason="CONFIG_INVALID", context={}, engaged_at=ts),
                PositionEvent(position_id=pid, run_id="R1", event_type="TRAIL_ADVANCE",
                              occurred_at=ts, price=110.0, from_value=85.0, to_value=100.0,
                              tier_label="BREAKEVEN", detail={}),
                CycleStats(run_id="R1", trading_day=date(2026, 9, 1), underlying="NIFTY",
                           candles_evaluated=1, oi_iv_context_missing=1,
                           first_cycle_at=ts, last_cycle_at=ts),
                ReplayRun(run_id="R1", created_at=ts, date_from=date(2026, 9, 1),
                          date_to=date(2026, 9, 2), underlyings=["NIFTY"], params={},
                          params_hash="0" * 64, seed=42, code_version="abc", status="RUNNING"),
            ])
            await db.commit()
        async with sqlite_db() as db:
            return {
                "sig": (await db.execute(select(StrategySignal))).scalar_one(),
                "rs": (await db.execute(select(RegimeStateModel))).scalar_one(),
                "dp": (await db.execute(select(DailyPnLSummary))).scalar_one(),
                "sp": (await db.execute(select(StrategyPerf))).scalar_one(),
                "rp": (await db.execute(select(RegimePerformanceModel))).scalar_one(),
                "nt": (await db.execute(select(NoTradeDecision))).scalar_one(),
                "rh": (await db.execute(select(RiskHalt))).scalar_one(),
                "pe": (await db.execute(select(PositionEvent))).scalar_one(),
                "cs": (await db.execute(select(CycleStats))).scalar_one(),
                "rr": (await db.execute(select(ReplayRun))).scalar_one(),
            }

    got = run(_write_and_read())
    pos = run(get_position(sqlite_db, pid))
    assert (pos.run_id, pos.original_quantity, pos.original_lots, pos.initial_risk) == \
        ("R1", 130, 2, 15.0)
    assert (pos.r_source, pos.exit_value_weighted, pos.r_multiple, pos.r_multiple_net) == \
        ("ENTRY", 121.0, 1.4, 1.3)
    assert pos.target_stretched is True and pos.partial_skipped is True
    assert (pos.entry_fill_source, pos.exit_fill_source, pos.dte_at_entry, pos.expiry_band) == \
        ("ASK", "BID", 6, "2-7")
    assert pos.entry_regime_multiplier == 0.5 and pos.hold_minutes == 12.5
    assert pos.sizing_detail == {"method": "FIXED_FRACTION"} and pos.entry_indicators == {"atr": 3.1}

    assert got["sig"].selection_rejections == {"SPREAD_TOO_WIDE": 3}
    assert got["sig"].no_trade_decision_id == 7 and got["sig"].run_id == "R1"
    assert got["rs"].run_id == "R1"
    assert got["dp"].worst_trade_loss == 20.0 and got["dp"].max_equity_drawdown == 20.0
    assert got["dp"].statutory_charges == 3.5 and got["dp"].r_sample_size == 3
    assert (got["sp"].winning_trades_r, got["sp"].losing_trades_r) == (1, 1)
    assert got["rp"].avg_r_multiple == 1.0 and got["rp"].run_id == "R1"
    assert len(got["nt"].reason) == 1000 and got["nt"].context == {"atr": 1.0}
    assert got["rh"].halt_type == "MANUAL" and got["rh"].released_at is None
    assert got["pe"].tier_label == "BREAKEVEN"
    assert got["cs"].oi_iv_context_missing == 1 and got["cs"].signals_emitted == 0
    assert got["rr"].seed == 42 and got["rr"].underlyings == ["NIFTY"]

    cfg = run(get_config_row(sqlite_db))
    assert (cfg.fixed_fraction_pct, cfg.kelly_min_trades, cfg.min_rr) == (12.5, 40, 2.5)


def test_new_columns_default_on_insert(sqlite_db):
    run(seed_config(sqlite_db))
    pid = run(add_position(sqlite_db))
    pos = run(get_position(sqlite_db, pid))
    assert pos.run_id == "PAPER"
    assert pos.target_stretched is False and pos.partial_skipped is False
    cfg = run(get_config_row(sqlite_db))
    assert (cfg.open_exposure_cap_pct, cfg.fixed_fraction_pct, cfg.kelly_multiplier) == \
        (40.0, 10.0, 0.25)
    assert (cfg.kelly_fraction_cap_pct, cfg.kelly_min_trades, cfg.losing_streak_halt_n) == \
        (15.0, 30, 3)
    assert (cfg.strategy_streak_halt_n, cfg.post_loss_cooldown_minutes, cfg.min_rr) == (2, 15, 2.0)
    assert (cfg.stale_tick_seconds, cfg.entry_cutoff_buffer_min) == (60, 10)


# ── 0013 guarded default updates (pure helpers, no Postgres) ─────────────────

def _m13():
    return _load("0013_config_params_and_renames.py")


def test_0013_post_migration_row_from_realistic_ensure_config_state():
    """§11: the realistic post-ensure_config row is fully migrated."""
    m = _m13()
    row = dict(capital=100000.0, trade_amount=20000.0, max_lots_per_trade=5,
               max_open_positions=6, max_daily_loss=5000.0, max_daily_loss_pct=5.0,
               max_trades_per_day=10)
    updates, divergent = m.plan_default_updates(row)
    assert updates == {
        "max_trades_per_day": 6, "max_lots_per_trade": 3, "max_open_positions": 3,
        "max_daily_loss_pct": 3.0, "max_daily_loss": 3000.0, "trade_amount": None,
    }
    assert divergent == {}
    assert "capital" not in updates


def test_0013_column_defaults_match_too():
    m = _m13()
    row = dict(capital=100000.0, trade_amount=10000.0, max_lots_per_trade=5,
               max_open_positions=6, max_daily_loss=5000.0, max_daily_loss_pct=2.0,
               max_trades_per_day=10)
    updates, divergent = m.plan_default_updates(row)
    assert updates["trade_amount"] is None and updates["max_daily_loss_pct"] == 3.0
    assert updates["max_daily_loss"] == 3000.0 and divergent == {}


def test_0013_operator_values_are_left_alone_and_reported():
    m = _m13()
    row = dict(capital=250000.0, trade_amount=25000.0, max_lots_per_trade=4,
               max_open_positions=2, max_daily_loss=7000.0, max_daily_loss_pct=2.8,
               max_trades_per_day=8)
    updates, divergent = m.plan_default_updates(row)
    assert updates == {}
    assert set(divergent) == {
        "max_trades_per_day", "max_lots_per_trade", "max_open_positions",
        "max_daily_loss_pct", "max_daily_loss", "trade_amount",
    }


@pytest.mark.parametrize("key,value,expected", [
    ("max_trades_per_day", 10, True), ("max_trades_per_day", 9, False),
    ("max_lots_per_trade", 5, True), ("max_lots_per_trade", 4, False),
    ("max_open_positions", 6, True), ("max_open_positions", 5, False),
    ("max_daily_loss_pct", 2.0, True), ("max_daily_loss_pct", 5.0, True),
    ("max_daily_loss_pct", 4.0, False),
    ("max_daily_loss", 5000.0, True), ("max_daily_loss", 4999.0, False),
    ("trade_amount", 10000.0, True), ("trade_amount", 20000.0, True),
    ("trade_amount", 15000.0, False),
])
def test_0013_guard_predicates(key, value, expected):
    m = _m13()
    row = dict(capital=100000.0, trade_amount=20000.0, max_lots_per_trade=5,
               max_open_positions=6, max_daily_loss=5000.0, max_daily_loss_pct=5.0,
               max_trades_per_day=10)
    row[key] = value
    assert m.is_known_bad(key, row) is expected


def test_0013_derived_lot_and_position_predicates():
    m = _m13()
    # capital 30k: ensure_config's lot derivation gives max(1, min(5, 3)) = 3, and
    # positions max(2, int(30000 / 6000 * 0.6)) = 3 — both are "known bad" values.
    row = dict(capital=30000.0, trade_amount=6000.0, max_lots_per_trade=3,
               max_open_positions=3, max_daily_loss=1500.0, max_daily_loss_pct=5.0,
               max_trades_per_day=10)
    assert m.is_known_bad("max_lots_per_trade", row)
    assert m.is_known_bad("max_open_positions", row)
    assert m.is_known_bad("max_daily_loss", row)         # round(30000 × 0.05)
    assert m.plan_default_updates(row)[0]["max_daily_loss"] == 900.0
    row_none = dict(row, trade_amount=None)
    assert m.is_known_bad("max_open_positions", row_none) is False   # no derivation without amount


# ── real downgrade → upgrade of 0011-0013 on SQLite ──────────────────────────

def _run_ops(conn, fn):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    mc = MigrationContext.configure(conn)
    with Operations.context(mc):
        fn()


def _reflect(engine):
    from sqlalchemy import inspect as sa_inspect
    insp = sa_inspect(engine)
    out = {}
    for t in insp.get_table_names():
        uniq = {tuple(u["column_names"]) for u in insp.get_unique_constraints(t)}
        uniq |= {tuple(i["column_names"]) for i in insp.get_indexes(t) if i.get("unique")}
        out[t] = ({c["name"] for c in insp.get_columns(t)}, uniq)
    return out


def test_downgrade_to_0010_then_upgrade_to_head_on_sqlite(tmp_path):
    pytest.importorskip("alembic")
    from sqlalchemy import create_engine, text

    md = _metadata()
    eng = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    md.create_all(eng, tables=[md.tables[t] for t in (
        "trading_config", "paper_positions", "strategy_signals", "daily_pnl_summary",
        "strategy_perf", "regime_states", "regime_performance", "no_trade_decisions",
        "risk_halts", "position_events", "cycle_stats", "replay_runs",
    )])
    head = _reflect(eng)
    mods = [_load(f) for _, f in NEW_REVISIONS]

    with eng.begin() as conn:
        conn.execute(text(
            "INSERT INTO trading_config (id, mode, max_daily_loss, max_daily_loss_pct, "
            "max_open_positions, max_lots_per_trade, capital, slippage_pct, trade_amount, "
            "used_capital, max_trades_per_day, daily_trades_count, square_off_time, "
            "live_mode_confirmed, study_phase, trading_halted) VALUES (1, 'PAPER', 5000, 5.0, "
            "6, 5, 100000, 0.2, 20000, 0, 10, 0, '15:15', 0, 0, 0)"))
    with eng.begin() as conn:
        for m in reversed(mods):
            _run_ops(conn, m.downgrade)
    base = _reflect(eng)
    for t in ("no_trade_decisions", "risk_halts", "position_events", "cycle_stats", "replay_runs"):
        assert t not in base
    assert "run_id" not in base["paper_positions"][0]
    assert "max_drawdown" in base["daily_pnl_summary"][0]
    assert "worst_trade_loss" not in base["daily_pnl_summary"][0]
    assert ("underlying", "timestamp") in base["regime_states"][1]
    assert ("strategy_name",) in base["strategy_perf"][1]
    assert "fixed_fraction_pct" not in base["trading_config"][0]
    with eng.connect() as conn:       # downgrade refilled the NOT NULL trade_amount
        assert conn.execute(text("SELECT trade_amount FROM trading_config")).scalar() is not None

    with eng.begin() as conn:
        conn.execute(text(
            "INSERT INTO paper_positions (strategy_name, token, symbol, underlying, exchange, "
            "direction, lots, lot_size, quantity, entry_price, entry_slippage, actual_entry, "
            "stop_loss, initial_stop_loss, partial_closed, partial_exit_qty, is_open, mode, "
            "entry_time) VALUES ('ORB', 'T', 'S', 'NIFTY', 'NFO', 'BUY', 1, 75, 75, 100, 0, 100, "
            "100, 85, 1, 75, 0, 'PAPER', '2026-09-01 04:00:00')"))
        conn.execute(text(
            "UPDATE trading_config SET trade_amount = 20000, max_daily_loss_pct = 5.0"))
    with eng.begin() as conn:
        for m in mods:
            _run_ops(conn, m.upgrade)
    again = _reflect(eng)
    for t, (cols, uniq) in head.items():
        assert again[t][0] == cols, f"{t}: column drift {again[t][0] ^ cols}"
        assert again[t][1] == uniq, f"{t}: unique drift {again[t][1] ^ uniq}"

    with eng.connect() as conn:
        row = conn.execute(text(
            "SELECT run_id, original_quantity, original_lots, initial_risk, r_source "
            "FROM paper_positions")).one()
        assert tuple(row) == ("PAPER", 150, 2, 15.0, "BACKFILL_UNVERIFIED")
        cfg = conn.execute(text(
            "SELECT trade_amount, max_trades_per_day, max_lots_per_trade, max_open_positions, "
            "max_daily_loss, max_daily_loss_pct, capital, fixed_fraction_pct "
            "FROM trading_config")).one()
        assert tuple(cfg) == (None, 6, 3, 3, 3000.0, 3.0, 100000.0, 10.0)
    eng.dispose()
