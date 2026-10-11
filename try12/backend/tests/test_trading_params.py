"""
TradingParams.validate() — design §6.3 check 6, §19.1 defaults, R18.2.

One test per violation family. Each asserts the message names the offending
key, so an operator reading the CONFIG_INVALID halt or the HTTP 400 knows what
to fix.
"""
import dataclasses

import pytest

from app.core.trading_params import (
    FORBIDDEN_KEYS, TradingParams, TradingParamsError, validate_overrides,
)


def _violations(**overrides) -> list[str]:
    return dataclasses.replace(TradingParams(), **overrides).validate()


def _assert_one_naming(violations: list[str], key: str) -> None:
    assert violations, f"expected a violation naming {key}"
    assert any(key in v for v in violations), violations


# ── defaults ─────────────────────────────────────────────────────────────────

def test_defaults_are_valid():
    assert TradingParams().validate() == []


def test_section_19_1_defaults():
    p = TradingParams()
    assert p.capital == 200_000.0
    assert p.fixed_fraction_pct == 10.0
    assert p.trade_amount is None
    assert p.open_exposure_cap_pct == 40.0
    assert (p.max_open_positions, p.max_lots_per_trade, p.max_trades_per_day) == (3, 3, 6)
    assert (p.max_daily_loss, p.max_daily_loss_pct) == (6000.0, 3.0)
    assert (p.losing_streak_halt_n, p.strategy_streak_halt_n, p.post_loss_cooldown_minutes) == (3, 2, 15)
    assert (p.kelly_multiplier, p.kelly_fraction_cap_pct, p.kelly_min_trades) == (0.25, 15.0, 30)
    assert (p.cooldown_mirror_ttl_s, p.params_cache_ttl_s) == (120, 30)
    assert p.transition_cooldown_candles == 3
    assert p.min_rr == 2.0
    assert p.stale_tick_seconds == 60
    assert p.entry_cutoff_buffer_min == 10
    assert [(t.trigger_reward_frac, t.lock_reward_frac) for t in p.trail_tiers] == [
        (0.90, 0.60), (0.75, 0.40), (0.50, 0.00)]
    assert (p.partial_exit_reward_frac, p.partial_exit_fraction) == (0.75, 0.50)
    assert p.dte_bands == {"NIFTY": ((2, 7), (8, 14)), "BANKNIFTY": ((2, 7), (8, 14), (15, 30))}
    assert p.dte1_cutoff_ist == "12:00"
    assert (p.max_spread_pct, p.min_oi, p.min_volume, p.max_slippage_pct, p.min_score) == (
        3.0, 50_000, 1_000, 2.0, 40.0)
    assert p.premium_band == {"NIFTY": (10.0, 700.0), "BANKNIFTY": (10.0, 1500.0)}
    assert (p.slippage_pct, p.quote_slippage_pct) == (0.2, 0.05)
    assert p.max_hold_minutes == {"VWAP_REVERSAL": 25, "RSI_VWAP": 25, "VWAP_PULLBACK": 25, "DEFAULT": 45}
    assert p.timeout_exit_min_gain_pct == 5.0
    assert p.r_flat_epsilon == 0.01
    assert p.brokerage_per_lot_per_leg == 20.0
    assert p.max_cost_fraction_of_edge == 0.33
    assert p.flat_epsilon == 1.0
    assert p.min_display_sample == 20
    assert p.allow_ltp_only_fill is False


# ── dropped keys (§3 deviations, R2 finding 8) ───────────────────────────────

def test_no_floor_or_buffer_field_exists():
    names = {f.name for f in dataclasses.fields(TradingParams)}
    assert "fraction_floor" not in names
    assert "breakeven_buffer_reward" not in names
    assert {"fraction_floor", "breakeven_buffer_reward"} <= set(FORBIDDEN_KEYS)


@pytest.mark.parametrize("key", ["fraction_floor", "breakeven_buffer_reward"])
def test_forbidden_key_is_rejected(key):
    v = validate_overrides({key: 0.0})
    _assert_one_naming(v, key)
    with pytest.raises(TradingParamsError) as ei:
        TradingParams.from_mapping({key: 0.0})
    assert key in str(ei.value)


def test_unknown_key_is_rejected():
    _assert_one_naming(validate_overrides({"not_a_param": 1}), "not_a_param")


def test_from_mapping_overlays_defaults():
    p = TradingParams.from_mapping({"min_rr": 2.5, "max_trades_per_day": 4})
    assert (p.min_rr, p.max_trades_per_day, p.capital) == (2.5, 4, 200_000.0)


# ── check-6 rules ────────────────────────────────────────────────────────────

def test_min_rr_below_two():
    _assert_one_naming(_violations(min_rr=1.9), "min_rr")


def test_loss_cap_above_capital():
    _assert_one_naming(_violations(max_daily_loss=250_000.0), "max_daily_loss")


def test_kelly_cap_below_fraction_is_valid_and_bounds_kelly():
    # a Kelly cap below the fixed fraction no longer fails: Kelly is capped at
    # min(kelly_fraction_cap_pct, fixed_fraction_pct), so it can only size down
    assert _violations(fixed_fraction_pct=20.0, kelly_fraction_cap_pct=15.0) == []
    assert dataclasses.replace(TradingParams(), fixed_fraction_pct=20.0,
                               kelly_fraction_cap_pct=15.0).kelly_cap_effective_pct == 15.0
    assert TradingParams().kelly_cap_effective_pct == 10.0      # defaults 10 / 15


@pytest.mark.parametrize("value", [0.0, 40.01, 100.0])
def test_fixed_fraction_bounds(value):
    v = _violations(fixed_fraction_pct=value)
    _assert_one_naming(v, "fixed_fraction_pct")
    # the message must state the 40% bound, not merely mention the field
    assert any("fixed_fraction_pct" in x and "(0, 40]" in x for x in v), v


@pytest.mark.parametrize("value", [0.0, 0.6])
def test_kelly_multiplier_bounds(value):
    _assert_one_naming(_violations(kelly_multiplier=value), "kelly_multiplier")


def test_kelly_multiplier_half_is_allowed():
    assert _violations(kelly_multiplier=0.5) == []


@pytest.mark.parametrize("value", [0.0, 30.0])
def test_kelly_fraction_cap_bounds(value):
    _assert_one_naming(_violations(kelly_fraction_cap_pct=value), "kelly_fraction_cap_pct")


def test_dte_bands_overlap():
    _assert_one_naming(_violations(dte_bands={"NIFTY": ((2, 7), (7, 14))}), "dte_bands")


def test_dte_bands_unordered():
    _assert_one_naming(_violations(dte_bands={"NIFTY": ((8, 14), (2, 7))}), "dte_bands")


def test_dte_band_inverted():
    _assert_one_naming(_violations(dte_bands={"NIFTY": ((7, 2),)}), "dte_bands")


@pytest.mark.parametrize("key", [
    "brokerage_per_lot_per_leg", "stt_sell_pct", "exchange_txn_pct",
    "sebi_per_crore", "stamp_buy_pct", "gst_pct", "slippage_pct",
])
def test_negative_cost_rate(key):
    _assert_one_naming(_violations(**{key: -0.01}), key)


def test_partial_exit_needs_two_lots():
    _assert_one_naming(_violations(max_lots_per_trade=1, partial_exit_enabled=True),
                       "max_lots_per_trade")
    assert _violations(max_lots_per_trade=1, partial_exit_enabled=False) == []


@pytest.mark.parametrize("value", [0, 9, 12])
def test_transition_cooldown_bounds(value):
    _assert_one_naming(_violations(transition_cooldown_candles=value),
                       "transition_cooldown_candles")


def test_transition_cooldown_seven_is_allowed():
    assert _violations(transition_cooldown_candles=7) == []


def test_max_hold_missing_default():
    _assert_one_naming(_violations(max_hold_minutes={"RSI_VWAP": 25}), "max_hold_minutes")


def test_max_hold_unknown_strategy():
    _assert_one_naming(_violations(max_hold_minutes={"DEFAULT": 45, "PCR_" + "SIGNAL": 25}),
                       "max_hold_minutes")


@pytest.mark.parametrize("value", [0, 375])
def test_max_hold_value_range(value):
    _assert_one_naming(_violations(max_hold_minutes={"DEFAULT": value}), "max_hold_minutes")


def test_timeout_gain_at_target():
    _assert_one_naming(_violations(timeout_exit_min_gain_pct=30.0), "timeout_exit_min_gain_pct")
    _assert_one_naming(_violations(timeout_exit_min_gain_pct=-1.0), "timeout_exit_min_gain_pct")


def test_quote_slippage_above_fallback_slippage():
    _assert_one_naming(_violations(quote_slippage_pct=0.3, slippage_pct=0.2), "quote_slippage_pct")
    _assert_one_naming(_violations(quote_slippage_pct=-0.01), "quote_slippage_pct")


def test_trade_amount_cache_must_match_derivation():
    # 10% x 200,000 = 20,000; tolerance is ₹500
    assert _violations(trade_amount=20_400.0) == []
    _assert_one_naming(_violations(trade_amount=60_000.0), "trade_amount")
    # R2 finding 2: a stored 20,000 against a 10,000 derivation is rejected
    _assert_one_naming(_violations(capital=100_000.0, max_daily_loss=3000.0, trade_amount=20_000.0),
                       "trade_amount")


# ── ladder (delegated to trailing.validate_ladder) ───────────────────────────

def _tiers(*pairs):
    from app.services.trading.trailing import TrailTier
    return tuple(TrailTier(t, l, f"T{i}") for i, (t, l) in enumerate(pairs))


def test_ladder_unreachable_trigger():
    _assert_one_naming(_violations(trail_tiers=_tiers((1.0, 0.6), (0.5, 0.0))), "trail_tiers")


def test_ladder_triggers_not_decreasing():
    _assert_one_naming(_violations(trail_tiers=_tiers((0.5, 0.0), (0.9, 0.6))), "trail_tiers")


def test_ladder_lock_not_decreasing():
    _assert_one_naming(_violations(trail_tiers=_tiers((0.9, 0.3), (0.75, 0.4))), "trail_tiers")


def test_ladder_lock_exceeds_trigger():
    _assert_one_naming(_violations(trail_tiers=_tiers((0.5, 0.5),)), "trail_tiers")


def test_ladder_lock_at_or_below_initial_stop():
    # sl 0.15 / target 0.30 -> bound is lock > -0.5
    _assert_one_naming(_violations(trail_tiers=_tiers((0.5, -0.5),)), "trail_tiers")
    assert _violations(trail_tiers=_tiers((0.5, -0.1),)) == []


def test_partial_frac_bounds():
    _assert_one_naming(_violations(partial_exit_reward_frac=1.0), "partial_exit_reward_frac")
    _assert_one_naming(_violations(partial_exit_fraction=1.0), "partial_exit_fraction")
    _assert_one_naming(_violations(partial_exit_fraction=0.0), "partial_exit_fraction")


def test_every_violation_is_collected():
    v = _violations(min_rr=1.0, kelly_multiplier=0.9, quote_slippage_pct=5.0)
    assert len(v) >= 3
