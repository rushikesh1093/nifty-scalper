"""Replay coverage, measured before any metric (design §14.4, D11, §18 item 2a)."""
import pathlib
import re
from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine, text

from app.core.trading_params import TradingParams
from app.services.replay import coverage
from app.services.replay.coverage import CoverageCause
from tests.replay_fixtures import (
    DAY1, add_rows, fill_db, make_db, run, scripted_quote, trend_candles,
)

REPLAY = pathlib.Path(__file__).resolve().parents[1] / "app" / "services" / "replay"


@pytest.fixture
def cov_db(tmp_path):
    Session, engine, url = make_db(tmp_path / "cov.db")
    yield Session, url
    run(engine.dispose())


def _classify(Session, day=DAY1, params=None):
    async def _go():
        async with Session() as db:
            return await coverage.classify_day(db, "NIFTY", day, params or TradingParams())
    return run(_go())


def _sql(url, stmt, **kw):
    eng = create_engine(url)
    try:
        with eng.begin() as conn:
            conn.execute(text(stmt), kw)
    finally:
        eng.dispose()


def test_a_full_day_is_tradeable_and_reports_its_facts(cov_db):
    Session, url = cov_db
    fill_db(url, [DAY1], scripted_quote({}))
    d = _classify(Session)
    assert (d.status, d.cause) == ("TRADEABLE", None)
    assert d.spot_candles == 345 and d.option_tick_minutes == 345
    assert d.option_tokens_with_ticks == 34 and d.unattributed_tokens == 0
    assert d.snapshot_cadence_s == 300.0                      # observed, not assumed
    assert d.chosen_expiry == "2026-09-03" and d.nearest_dte == 2
    assert d.ce_strikes >= 8 and d.pe_strikes >= 8 and d.vix_gate == "evaluated"


def test_no_spot_candles(cov_db):
    Session, _ = cov_db
    assert _classify(Session).cause == CoverageCause.NO_SPOT_CANDLES.value


def test_insufficient_candles(cov_db):
    Session, url = cov_db
    fill_db(url, [DAY1], scripted_quote({}), candles=trend_candles(200))
    d = _classify(Session)
    assert d.cause == CoverageCause.INSUFFICIENT_CANDLES.value and d.spot_candles == 200


def test_no_prev_day_snapshot(cov_db):
    Session, url = cov_db
    fill_db(url, [DAY1], scripted_quote({}), prev_day_snapshot=False)
    assert _classify(Session).cause == CoverageCause.NO_PREV_DAY_SNAPSHOT.value


def test_no_option_data_when_ticks_cover_too_few_minutes(cov_db):
    Session, url = cov_db
    fill_db(url, [DAY1], scripted_quote({}))
    # keep only the first 200 minutes of option ticks (09:15-12:34 IST = 03:45-07:05 UTC)
    _sql(url, "DELETE FROM ticks WHERE timestamp >= :cut", cut="2026-09-01 07:05:00")
    d = _classify(Session)
    assert d.cause == CoverageCause.NO_OPTION_DATA.value and d.option_tick_minutes == 200


def test_no_instrument_master_is_a_data_gap(cov_db):
    Session, url = cov_db
    fill_db(url, [DAY1], scripted_quote({}))
    _sql(url, "DELETE FROM instruments")
    d = _classify(Session)
    assert d.cause == CoverageCause.NO_INSTRUMENT_MASTER.value
    assert d.universe_size == 0 and d.unattributed_tokens == 34     # measured (§26 item 4)


def test_no_banded_expiry_is_a_policy_exclusion(cov_db):
    Session, url = cov_db
    far = DAY1 + timedelta(days=30)                                  # DTE 30: outside (2,7),(8,14)
    fill_db(url, [DAY1], scripted_quote({}), expiries=[far])
    d = _classify(Session)
    assert d.cause == CoverageCause.NO_BANDED_EXPIRY.value
    assert d.expiries == [[far.isoformat(), 30]] and d.nearest_dte == 30
    assert d.dte_bands == [[2, 7], [8, 14]]                          # printed beside it


def test_report_walks_weekdays_only(cov_db):
    Session, url = cov_db
    fill_db(url, [DAY1], scripted_quote({}))

    async def _go():
        async with Session() as db:
            return await coverage.report(db, ("NIFTY",), DAY1, date(2026, 9, 7), TradingParams())
    rep = run(_go())
    assert [d.day for d in rep.days] == [date(2026, 9, d) for d in (1, 2, 3, 4, 7)]
    assert rep.tradeable_days() == [DAY1]
    s = rep.summary()["NIFTY"]
    assert s["tradeable"] == 1 and s["skipped"] == {"NO_SPOT_CANDLES": 4}


def test_regime_histogram_flags_unclassified_above_40_percent(cov_db):
    from datetime import datetime, timezone
    from app.db.models.regime import RegimeStateModel
    Session, _ = cov_db
    at = datetime(2026, 9, 1, 5, 0, tzinfo=timezone.utc)
    rows = [RegimeStateModel(underlying="NIFTY", regime=r, timestamp=at + timedelta(minutes=i),
                             run_id=rid)
            for rid, regimes in (("RPL-A", ["UNCLASSIFIED"] * 5 + ["BULL_TREND_NORMAL_VOL"] * 5),
                                 ("RPL-B", ["UNCLASSIFIED"] * 4 + ["HIGH_VOL_CHOP"] * 6))
            for i, r in enumerate(regimes)]
    add_rows(Session, *rows)

    async def _hist(rid):
        async with Session() as db:
            return await coverage.regime_histogram(db, rid)
    a, b = run(_hist("RPL-A"))["NIFTY"], run(_hist("RPL-B"))["NIFTY"]
    assert a["total"] == 10 and a["structurally_ineligible"] == 5
    assert a["finding"] and "UNCLASSIFIED dominates" in a["finding"]
    assert b["finding"] is None and b["structurally_ineligible"] == 10     # 40% is not > 40%


_REF = re.compile(r"\bCoverageCause\.([A-Za-z_][A-Za-z0-9_]*)")


def test_coverage_cause_is_referenced_in_both_directions():
    """Every CoverageCause.<MEMBER> referenced under app/services/replay/ is declared,
    and every declared member is referenced (the ReasonCode rule, §14.4)."""
    refs = set()
    for path in REPLAY.rglob("*.py"):
        src = path.read_text(encoding="utf-8")
        if path.name == "coverage.py":
            src = src.split("class CoverageCause", 1)[0] + src.split("TRADEABLE = ", 1)[1]
        refs |= set(_REF.findall(src))
    assert refs == set(CoverageCause.__members__)
