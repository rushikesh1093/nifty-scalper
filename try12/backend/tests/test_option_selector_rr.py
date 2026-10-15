"""Bug 10 (design §10): a real, un-stretched 1:2 R:R gate; no -1 sentinel passes; one
quote read feeds the filter and the fill; an affordability ceiling; per-underlying
DTE bands; and one source for the R:R floor."""
import inspect
from datetime import date, time as dtime, timedelta

import pytest
from sqlalchemy import select

from app.core.trading_params import TradingParams
from app.db.models.paper_trade import PaperPosition
from app.db.models.risk import NoTradeDecision
from app.runtime.context import using
from app.services.trading import fills, startup_checks
from app.services.trading.no_trade import ReasonCode
from app.services.trading.option_selector import (
    FilterResult, RR_EPSILON, _hard_filter, choose_expiry, option_selector,
)
from app.services.trading.paper_engine import paper_engine
from app.strategies.base import Direction
from app.strategies.strategies import build_strategies
from tests.helpers import (
    allow_regime, fake_candidate, make_signal, option_row, pin_ist_time, run, seed_config,
    with_instruments, with_params,
)

P = TradingParams()
GOOD = dict(ltp=100.0, bid=99.5, ask=100.5, oi=200_000, volume=5_000, tick_age_s=1.0)


def hf(**over) -> FilterResult:
    kw = dict(GOOD, sl_pct=0.15, target_pct=0.30, underlying="NIFTY", min_rr=2.0)
    kw.update(over)
    return _hard_filter(**kw)


@pytest.mark.parametrize("field, value, code", [
    ("ltp", float("nan"), "TICK_UNAVAILABLE"),
    ("ltp", float("inf"), "TICK_UNAVAILABLE"),
    ("ask", float("inf"), "QUOTE_UNAVAILABLE"),
    ("bid", float("nan"), "QUOTE_UNAVAILABLE"),
    ("tick_age_s", float("nan"), "QUOTE_STALE"),
    ("tick_age_s", -1.0, "QUOTE_STALE"),
    ("sl_pct", float("nan"), "RR_INSUFFICIENT"),
    ("sl_pct", 1.0, "RR_INSUFFICIENT"),
    ("target_pct", float("inf"), "RR_INSUFFICIENT"),
    ("min_rr", float("nan"), "RR_INSUFFICIENT"),
])
def test_nonfinite_market_and_risk_inputs_fail_closed(field, value, code):
    result = hf(**{field: value})
    assert not result.passed and result.filter == code


def test_price_behavior_never_rewards_a_larger_drop():
    from app.services.trading.option_selector import _score_price_behavior

    prices = [95.0, 98.0, 98.99, 99.0, 99.5, 100.0, 100.5, 101.1]
    scores = [_score_price_behavior(price, [100.0, price]) for price in prices]
    assert scores == sorted(scores)
    assert all(0 <= score <= 100 for score in scores)


@pytest.mark.parametrize("previous_oi", [None, 0, -1])
def test_unknown_oi_baseline_does_not_invent_fresh_positioning(runtime_ctx, fake_redis, previous_oi):
    from app.services.trading.option_selector import score_candidate

    row = _chain(runtime_ctx, strikes=(22600,))[0]
    _tick(fake_redis, row.token, 100.0)
    candidate, result = score_candidate(
        row, 22600.0, run(fake_redis.get_tick(row.token)), row.expiry,
        Direction.BUY, 50, prev_oi=previous_oi, sl_pct=0.15, target_pct=0.30, tp=P,
    )
    assert result.passed
    assert candidate.oi_change == 0
    assert candidate.score_oi_change == 50.0


def test_call_scan_preserves_put_history(runtime_ctx, fake_redis):
    rows = _chain(runtime_ctx, strikes=(22600,))
    expiry = rows[0].expiry
    oi_key = f"prev_oi:NIFTY:{expiry}"
    vol_key = f"avg_vol:NIFTY:{expiry}"
    run(fake_redis.set(oi_key, {"put-token": 200_000}))
    run(fake_redis.set(vol_key, {"put-token": 10_000.0}))
    _tick(fake_redis, rows[0].token, 100.0)
    with using(with_instruments(runtime_ctx, rows)):
        run(option_selector.select(None, Direction.BUY, "NIFTY", 22600.0, **KW))
    assert run(fake_redis.get(oi_key))["put-token"] == 200_000
    assert run(fake_redis.get(vol_key))["put-token"] == 10_000.0
    assert run(fake_redis.get(oi_key))[rows[0].token] == 300_000


# ── the R:R gate is real ─────────────────────────────────────────────────────

def test_a_one_to_one_point_five_setup_is_rejected_where_the_stretch_used_to_pass_it():
    r = hf(sl_pct=0.10, target_pct=0.15)
    assert not r.passed and r.filter == "RR_INSUFFICIENT"
    assert "never stretched" in r.reason


def test_the_canonical_setup_passes_including_a_float_noise_shortfall():
    assert hf(sl_pct=0.15, target_pct=0.30).passed
    assert hf(sl_pct=0.15, target_pct=0.30 - 1e-9).passed        # inside RR_EPSILON
    assert not hf(sl_pct=0.15, target_pct=0.30 - 1e-3).passed
    assert RR_EPSILON == 1e-6


def test_min_rr_is_a_required_argument_with_no_default():
    sig = inspect.signature(_hard_filter)
    assert sig.parameters["min_rr"].default is inspect.Parameter.empty
    with pytest.raises(TypeError):
        _hard_filter(ltp=100.0, bid=99.5, ask=100.5, oi=200_000, volume=5_000,
                     sl_pct=0.15, target_pct=0.30)


def test_the_floor_is_whatever_the_caller_passes():
    assert not hf(sl_pct=0.15, target_pct=0.30, min_rr=2.5).passed       # 2.0 < 2.5
    assert hf(sl_pct=0.15, target_pct=0.45, min_rr=2.5).passed


def test_entry_levels_round_gate_safe_and_stretch_only_after_the_gate():
    # on-tick fill: stop 85.00, target 130.00, no stretch
    assert fills.entry_levels(100.0, 0.15, 0.30, 2.0)[:2] == (85.0, 130.0)
    assert fills.entry_levels(100.0, 0.15, 0.30, 2.0)[3] is False
    # off-tick fills: the stop rounds DOWN and the target UP. Usually that only helps…
    sl, tgt, rr, stretched = fills.entry_levels(40.08, 0.15, 0.30, 2.0)
    assert (sl, tgt, stretched) == (34.05, 52.15, False) and rr >= 2.0
    # …but at 100.02 the stop loses a full tick of risk while the target gains less than
    # two: the realised R:R is 1.9994. Only then is the target lifted, and it is recorded.
    sl, tgt, rr, stretched = fills.entry_levels(100.02, 0.15, 0.30, 2.0)
    assert sl == 85.0 and stretched is True and rr >= 2.0 - RR_EPSILON
    assert (tgt - 100.02) / (100.02 - sl) >= 2.0 - RR_EPSILON
    # and a zero stop distance has no levels at all
    assert fills.entry_levels(40.0, 0.0, 0.30, 2.0)[2] == 0.0


# ── missing data is a rejection ──────────────────────────────────────────────

@pytest.mark.parametrize("over, code", [
    (dict(oi=-1), "OI_UNAVAILABLE"),
    (dict(oi=None), "OI_UNAVAILABLE"),
    (dict(volume=-1), "VOLUME_UNAVAILABLE"),
    (dict(volume=None), "VOLUME_UNAVAILABLE"),
    (dict(oi=10_000), "LIQUIDITY_TOO_LOW"),
    (dict(volume=10), "VOLUME_INSUFFICIENT"),
    (dict(bid=None, ask=None), "QUOTE_UNAVAILABLE"),
    (dict(bid=0.0, ask=0.0), "QUOTE_UNAVAILABLE"),
    (dict(bid=101.0, ask=100.0), "QUOTE_UNAVAILABLE"),              # crossed
    (dict(tick_age_s=61.0), "QUOTE_STALE"),
    (dict(tick_age_s=None), "QUOTE_STALE"),                          # age unknown = stale
    (dict(bid=90.0, ask=110.0), "SPREAD_TOO_WIDE"),
    (dict(ltp=5.0, bid=4.9, ask=5.1), "CONTRACT_UNSUITABLE"),
    (dict(ltp=800.0, bid=799.0, ask=801.0), "CONTRACT_UNSUITABLE"),
])
def test_missing_or_bad_data_is_a_named_rejection(over, code):
    r = hf(**over)
    assert not r.passed and r.filter == code
    assert r.filter in {c.value for c in ReasonCode}


def test_banknifty_has_its_own_premium_ceiling():
    assert hf(ltp=900.0, bid=899.0, ask=901.0, underlying="BANKNIFTY").passed
    assert hf(ltp=900.0, bid=899.0, ask=901.0, underlying="NIFTY").filter == "CONTRACT_UNSUITABLE"


def test_a_quote_less_contract_passes_only_when_ltp_only_fills_are_allowed():
    quote_less = dict(bid=None, ask=None, tick_age_s=1.0)
    assert hf(**quote_less).filter == "QUOTE_UNAVAILABLE"
    assert hf(**quote_less, tp=TradingParams(allow_ltp_only_fill=True)).passed


def test_premium_above_the_affordable_ceiling_is_rejected():
    # ₹20,000 budget / 65 = ₹307.69 for one lot
    assert hf(ltp=300.0, bid=299.5, ask=300.5, max_premium=20_000 / 65).passed
    r = hf(ltp=320.0, bid=319.5, ask=320.5, max_premium=20_000 / 65)
    assert r.filter == "PREMIUM_ABOVE_AFFORDABLE"


# ── end to end through select(): one quote read, the ceiling, the ladder ─────

def _chain(ctx, *, days=7, strikes=(22600, 22650, 22700)):
    return [option_row(k, ctx.clock.trading_day() + timedelta(days=days)) for k in strikes]


KW = dict(sl_pct=0.15, target_pct=0.30, params=P, base_amount=20_000.0, regime_multiplier=1.0)


def _tick(redis, token, ltp, **kw):
    redis.put_tick(token, ltp, bid=round(ltp * 0.995, 2), ask=round(ltp * 1.005, 2),
                   oi=kw.pop("oi", 300_000), volume=kw.pop("volume", 20_000), **kw)


def test_sentinel_oi_and_volume_candidates_are_rejected_with_named_codes(runtime_ctx, fake_redis):
    rows = _chain(runtime_ctx)
    _tick(fake_redis, rows[0].token, 120.0, oi=-1)
    _tick(fake_redis, rows[1].token, 100.0, volume=-1)
    _tick(fake_redis, rows[2].token, 80.0)
    with using(with_instruments(runtime_ctx, rows)):
        res = run(option_selector.select(None, Direction.BUY, "NIFTY", 22600.0, **KW))
    assert res.rejections == {"OI_UNAVAILABLE": 1, "VOLUME_UNAVAILABLE": 1}
    assert res.candidate is not None and res.candidate.token == rows[2].token
    assert set(res.rejections) <= {c.value for c in ReasonCode}
    assert res.evaluated == 3


def test_the_filter_and_the_fill_judge_one_tick_read(runtime_ctx, fake_redis):
    """QUOTE_STALE uses the age of THE quote the fill will use — and the selector
    reads each instrument's tick exactly once (§10.3)."""
    rows = _chain(runtime_ctx, strikes=(22600, 22650))
    _tick(fake_redis, rows[0].token, 120.0, age_s=300)          # stale quote
    _tick(fake_redis, rows[1].token, 100.0, age_s=2)
    reads = []
    real = fake_redis.get_tick

    async def _spy(token):
        reads.append(token)
        return await real(token)
    fake_redis.get_tick = _spy
    import app.services.cache.redis_client as rc
    rc.redis_cache.get_tick = _spy
    with using(with_instruments(runtime_ctx, rows)):
        res = run(option_selector.select(None, Direction.BUY, "NIFTY", 22600.0, **KW))
    assert res.rejections == {"QUOTE_STALE": 1}
    assert sorted(reads) == sorted(r.token for r in rows)       # once each, no second pass
    c = res.candidate
    assert c.token == rows[1].token and c.tick_age_s == pytest.approx(2.0)
    # the fill off this candidate's own age is the ASK fill, not the LTP fallback
    price, source = fills.entry_fill(c.ltp, c.bid, c.ask, c.tick_age_s, P)
    assert source == "ASK"


def test_an_unaffordable_top_scorer_is_skipped_for_the_next_candidate(runtime_ctx, fake_redis):
    """ATM scores highest but costs ₹300 × 65 > the ₹10,000 the 0.5 regime leaves."""
    rows = _chain(runtime_ctx)
    _tick(fake_redis, rows[0].token, 300.0)       # ATM: best score, unaffordable at 0.5×
    _tick(fake_redis, rows[1].token, 140.0)
    _tick(fake_redis, rows[2].token, 90.0)
    with using(with_instruments(runtime_ctx, rows)):
        full = run(option_selector.select(None, Direction.BUY, "NIFTY", 22600.0, **KW))
        half = run(option_selector.select(None, Direction.BUY, "NIFTY", 22600.0,
                                          **dict(KW, regime_multiplier=0.5)))
    assert full.candidate.token == rows[0].token and full.rejections == {}
    assert half.rejections == {"PREMIUM_ABOVE_AFFORDABLE": 1}     # ₹300 > ₹10,000 / 65 = ₹153.8
    assert half.candidate.token != rows[0].token
    assert half.candidate.ltp * 65 <= 10_000.0


def test_ltp_only_fallbacks_are_counted_when_allowed(runtime_ctx, fake_redis):
    rows = _chain(runtime_ctx, strikes=(22600,))
    fake_redis.put_tick(rows[0].token, 100.0, oi=300_000, volume=20_000)    # no bid/ask
    with using(with_instruments(runtime_ctx, rows)):
        strict = run(option_selector.select(None, Direction.BUY, "NIFTY", 22600.0, **KW))
        loose = run(option_selector.select(
            None, Direction.BUY, "NIFTY", 22600.0,
            **dict(KW, params=TradingParams(allow_ltp_only_fill=True))))
    assert strict.candidate is None and strict.rejections == {"QUOTE_UNAVAILABLE": 1}
    assert loose.candidate is not None and loose.ltp_only_fallbacks == 1


def test_the_result_carries_the_score_table_and_expiry_rationale(runtime_ctx, fake_redis):
    rows = _chain(runtime_ctx)
    for r, px in zip(rows, (120.0, 100.0, 80.0)):
        _tick(fake_redis, r.token, px)
    with using(with_instruments(runtime_ctx, rows)):
        res = run(option_selector.select(None, Direction.BUY, "NIFTY", 22600.0, **KW))
    assert res.expiry.band == "PREFERRED" and res.expiry.dte == 7
    assert 1 <= len(res.score_table) <= 8
    assert res.score_table[0]["score"] == res.candidate.score
    assert [c.score for c in res.ranked] == sorted((c.score for c in res.ranked), reverse=True)


# ── the expiry ladder ────────────────────────────────────────────────────────

TODAY = date(2026, 9, 1)


def _choice(und, dtes, now=dtime(10, 0)):
    return choose_expiry([TODAY + timedelta(days=d) for d in dtes], today=TODAY, now_t=now,
                         underlying=und, tp=P)


@pytest.mark.parametrize("und, dtes, expiry_dte, band", [
    ("NIFTY", [7], 7, "PREFERRED"),
    ("NIFTY", [3, 10], 3, "PREFERRED"),
    ("NIFTY", [10], 10, "FALLBACK_1"),
    ("NIFTY", [10, 5], 5, "PREFERRED"),                 # the earlier BAND wins, not the earlier date
    ("BANKNIFTY", [22], 22, "FALLBACK_2"),              # the third band, BANKNIFTY only
])
def test_expiry_bands_per_underlying(und, dtes, expiry_dte, band):
    c = _choice(und, dtes)
    assert c.expiry == TODAY + timedelta(days=expiry_dte) and c.band == band
    assert c.dte == expiry_dte


def test_nifty_dte_22_has_no_banded_expiry_while_banknifty_dte_22_does():
    n = _choice("NIFTY", [22])
    assert n.expiry is None and n.cause == "NO_BANDED_EXPIRY"
    assert "22" in n.reason and n.observed[0][1] == 22          # says which expiries existed
    assert _choice("BANKNIFTY", [22]).band == "FALLBACK_2"


def test_an_empty_universe_is_a_data_gap_not_a_policy_exclusion():
    c = _choice("NIFTY", [])
    assert c.expiry is None and c.cause == "NO_INSTRUMENT_MASTER"


def test_dte_zero_is_never_traded():
    c = _choice("NIFTY", [0])
    assert c.expiry is None and c.cause == "NO_BANDED_EXPIRY"
    assert _choice("NIFTY", [0, 7]).dte == 7


@pytest.mark.parametrize("now, expected", [
    (dtime(9, 30), 1), (dtime(11, 59), 1), (dtime(12, 0), None), (dtime(14, 0), None)])
def test_dte_one_only_before_noon(now, expected):
    c = _choice("NIFTY", [1], now=now)
    assert c.dte == expected
    if expected:
        assert c.band == "DTE1_MORNING"


def test_dte_one_is_used_only_when_there_is_no_banded_expiry():
    assert _choice("NIFTY", [1, 7], now=dtime(9, 30)).dte == 7
    assert _choice("NIFTY", [1, 22], now=dtime(9, 30)).dte == 1


# ── end to end: a sub-1:2 signal never opens a position ──────────────────────

def test_a_sub_one_to_two_signal_never_opens_a_position(runtime_ctx, sqlite_db, fake_redis,
                                                        monkeypatch):
    run(seed_config(sqlite_db))
    pin_ist_time(monkeypatch, dtime(10, 0))
    allow_regime(monkeypatch)
    rows = _chain(runtime_ctx, strikes=(22600,))
    _tick(fake_redis, rows[0].token, 100.0)
    with using(with_instruments(runtime_ctx, rows)):
        assert run(paper_engine.handle_signal(make_signal(sl_pct=0.10, target_pct=0.15))) is False
        ok = run(paper_engine.handle_signal(make_signal(sl_pct=0.15, target_pct=0.30)))

    async def _rows():
        async with sqlite_db() as db:
            return ((await db.execute(select(PaperPosition))).scalars().all(),
                    (await db.execute(select(NoTradeDecision))).scalars().all())
    positions, decisions = run(_rows())
    assert ok is True and len(positions) == 1                    # only the canonical signal
    rr = [d for d in decisions if d.code == "RR_INSUFFICIENT"]
    assert len(rr) == 1 and rr[0].stage == "SIGNAL_GATE"          # ONE row, not one per strike
    pos = positions[0]
    assert pos.dte_at_entry == 7 and pos.expiry_band == "PREFERRED"
    assert pos.target_stretched is False and pos.entry_fill_source == "ASK"


# ── the floor has exactly one source ─────────────────────────────────────────

def test_no_min_rr_literal_survives_under_app():
    assert startup_checks.scan_min_rr_literals() == []
    assert startup_checks.check_min_rr_single_source() == []
    import app.services.trading.option_selector as sel
    assert not hasattr(sel, "MIN_RR") and not hasattr(sel, "HF_MIN_RR")


def test_the_literal_scan_catches_a_planted_constant(tmp_path):
    pkg = tmp_path / "app"
    pkg.mkdir()
    (pkg / "x.py").write_text("HF_MIN_RR = 1.5\nMIN_RR   = 2.0\nok = 1\nMIN_RR = signal.min_rr\n")
    hits = startup_checks.scan_min_rr_literals(pkg)
    assert hits == ["app/x.py:1", "app/x.py:2"]


def test_no_strategy_declares_a_min_rr_attribute():
    for s in build_strategies("NIFTY") + build_strategies("BANKNIFTY"):
        assert not hasattr(type(s), "min_rr"), s.name


def test_a_signal_records_the_floor_it_was_built_under(runtime_ctx):
    strat = build_strategies("NIFTY")[0]
    assert strat._make_signal(Direction.BUY, 22600.0, {}).min_rr == 2.0
    with using(with_params(runtime_ctx, min_rr=2.5)):
        # a record of params().min_rr at build time, never a class attribute
        assert strat._make_signal(Direction.BUY, 22600.0, {}).min_rr == 2.5
