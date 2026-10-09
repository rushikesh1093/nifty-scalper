"""Fix 8 (arity) and bug 10 (design §10.5): select() returns a SelectionResult from
EVERY path, _build_candidates always returns the same 3-tuple, and the rejection
tally is a subset of ReasonCode."""
import inspect
from datetime import date, timedelta

import pytest

from app.core.trading_params import TradingParams
from app.runtime.context import using
from app.services.trading.no_trade import ReasonCode
from app.services.trading.option_selector import (
    ExpiryChoice, SelectionResult, option_selector,
)
from app.strategies.base import Direction
from tests.helpers import option_row, run, with_instruments

SPOT = 22600.0
KW = dict(sl_pct=0.15, target_pct=0.30, params=TradingParams(), base_amount=20_000.0,
          regime_multiplier=1.0)
CODES = {c.value for c in ReasonCode}


def _in(ctx, days):
    return ctx.clock.trading_day() + timedelta(days=days)


def test_no_instruments_at_all_is_a_selection_result_not_a_valueerror(runtime_ctx):
    with using(with_instruments(runtime_ctx, [])):
        res = run(option_selector.select(None, Direction.BUY, "NIFTY", SPOT, **KW))
        top = run(option_selector.select_top_n(None, Direction.BUY, "NIFTY", SPOT, n=3, **KW))
    for r in (res, top):
        assert isinstance(r, SelectionResult) and r.candidate is None and r.ranked == []
        assert r.rejections == {"NO_INSTRUMENTS": 1}
        assert r.expiry.cause == "NO_INSTRUMENT_MASTER" and r.evaluated == 0


def test_an_expiry_with_no_strikes_in_the_window_returns_without_error(runtime_ctx):
    far = [option_row(30000, _in(runtime_ctx, 7), kind="PE")]   # nowhere near spot ± 5 strikes (SELL → PE)
    with using(with_instruments(runtime_ctx, far)):
        res = run(option_selector.select(None, Direction.SELL, "NIFTY", SPOT, **KW))
        top = run(option_selector.select_top_n(None, Direction.SELL, "NIFTY", SPOT, n=3, **KW))
    for r in (res, top):
        assert r.candidate is None and r.rejections == {"NO_INSTRUMENTS": 1}
        assert r.expiry.expiry == _in(runtime_ctx, 7)         # the expiry WAS chosen


def test_no_banded_expiry_is_no_suitable_expiry(runtime_ctx):
    rows = [option_row(22600, _in(runtime_ctx, 22))]          # DTE 22: outside NIFTY's bands
    with using(with_instruments(runtime_ctx, rows)):
        res = run(option_selector.select(None, Direction.BUY, "NIFTY", SPOT, **KW))
    assert res.rejections == {"NO_SUITABLE_EXPIRY": 1}
    assert res.expiry.cause == "NO_BANDED_EXPIRY" and "22" in res.expiry.reason


@pytest.mark.parametrize("direction", [Direction.BUY, Direction.SELL])
def test_build_candidates_returns_the_same_three_tuple_on_every_path(runtime_ctx, direction):
    kw = dict(sl_pct=0.15, target_pct=0.30, params=TradingParams(), base_amount=20_000.0,
              regime_multiplier=1.0)
    scenarios = [
        [],                                                    # empty universe
        [option_row(22600, _in(runtime_ctx, 22))],             # nothing in a band
        [option_row(30000, _in(runtime_ctx, 7))],              # no strikes in the window
        [option_row(22600, _in(runtime_ctx, 7), kind="CE"),    # a candidate, but no tick
         option_row(22600, _in(runtime_ctx, 7), kind="PE")],
    ]
    for rows in scenarios:
        with using(with_instruments(runtime_ctx, rows)):
            out = run(option_selector._build_candidates(None, direction, "NIFTY", SPOT, **kw))
        assert isinstance(out, tuple) and len(out) == 3
        cands, rejections, expiry = out
        assert isinstance(cands, list) and isinstance(expiry, ExpiryChoice)
        assert set(rejections) <= CODES


def test_a_contract_with_no_tick_is_tick_unavailable_never_a_sentinel(runtime_ctx):
    rows = [option_row(22600, _in(runtime_ctx, 7))]
    with using(with_instruments(runtime_ctx, rows)):
        res = run(option_selector.select(None, Direction.BUY, "NIFTY", SPOT, **KW))
    assert res.candidate is None and res.rejections == {"TICK_UNAVAILABLE": 1}
    assert res.evaluated == 1


def test_select_signatures_are_keyword_only_with_no_stale_defaults():
    for fn in (option_selector.select, option_selector.select_top_n):
        sig = inspect.signature(fn)
        for name in ("sl_pct", "target_pct", "params", "base_amount", "regime_multiplier"):
            p = sig.parameters[name]
            assert p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is inspect.Parameter.empty
        assert "min_ltp" not in sig.parameters and "max_ltp" not in sig.parameters
