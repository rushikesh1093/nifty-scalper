"""Bug 4 (design §8.1): the pure trailing ladder — validate_ladder and next_stop."""
import pytest

from app.services.trading.trailing import (
    DEFAULT_TRAIL_TIERS, PARTIAL_EXIT_FRACTION, PARTIAL_EXIT_REWARD_FRAC, TrailTier,
    next_stop, validate_ladder,
)

ENTRY, TARGET, STOP0 = 100.0, 130.0, 85.0       # sl 15%, target 30%: target = +2R


def _walk(entry, target, stop, tiers=DEFAULT_TRAIL_TIERS, step=0.25):
    """Rising path entry → target; returns [(ltp, new_stop, label)] for each advance."""
    sl, fired = stop, []
    ltp = entry
    while ltp < target:
        res = next_stop(entry, target, ltp, sl, tiers)
        if res is not None:
            new_sl, label = res
            assert new_sl > sl                      # monotonic, enforced inside next_stop
            assert new_sl < ltp                     # never locks more than has been earned
            sl = new_sl
            fired.append((ltp, new_sl, label))
        ltp = round(ltp + step, 2)
    return fired


def test_rising_path_fires_each_tier_once_in_order():
    fired = _walk(ENTRY, TARGET, STOP0)
    assert [(label, s) for _, s, label in fired] == [
        ("BREAKEVEN", 100.0), ("LOCK_40", 112.0), ("LOCK_60", 118.0)]
    assert [ltp for ltp, _, _ in fired] == [115.0, 122.5, 127.0]   # +15%, +22.5%, +27%
    assert all(ltp < TARGET for ltp, _, _ in fired)                # every tier before target


def test_breakeven_is_exactly_entry():
    assert next_stop(ENTRY, TARGET, 115.0, STOP0) == (100.0, "BREAKEVEN")
    assert next_stop(ENTRY, TARGET, 114.99, STOP0) is None


def test_next_stop_never_lowers_the_stop():
    assert next_stop(ENTRY, TARGET, 116.0, 105.0) is None          # BREAKEVEN 100 < 105
    assert next_stop(ENTRY, TARGET, 123.0, 112.0) is None          # same tier again


def test_highest_trigger_first():
    # a jump straight to +27% places LOCK_60, not BREAKEVEN
    assert next_stop(ENTRY, TARGET, 127.5, STOP0) == (118.0, "LOCK_60")


@pytest.mark.parametrize("target", [None, 100.0, 90.0])
def test_next_stop_requires_a_target_above_entry(target):
    with pytest.raises(ValueError):
        next_stop(ENTRY, target, 120.0, STOP0)


def test_default_ladder_validates():
    assert validate_ladder(DEFAULT_TRAIL_TIERS, PARTIAL_EXIT_REWARD_FRAC,
                           PARTIAL_EXIT_FRACTION, 0.15, 0.30) == []


def _tiers(*pairs):
    return tuple(TrailTier(t, lk, f"T{i}") for i, (t, lk) in enumerate(pairs))


@pytest.mark.parametrize("tiers, needle", [
    (_tiers((1.0, 0.6), (0.5, 0.0)), "unreachable"),                 # at the target
    (_tiers((0.5, 0.0), (0.9, 0.6)), "strictly decreasing"),         # triggers out of order
    (_tiers((0.9, 0.3), (0.75, 0.4)), "locks must be strictly"),     # lower trigger locks more
    (_tiers((0.9, 0.95), (0.5, 0.0)), "must be < trigger"),          # locks more than earned
    (_tiers((0.9, 0.6), (0.5, -0.5)), "initial_stop_loss"),          # at the initial stop
])
def test_validation_rejects_bad_ladders(tiers, needle):
    v = validate_ladder(tiers, 0.75, 0.5, 0.15, 0.30)
    assert any(needle in m for m in v), v


def test_negative_lock_is_below_entry_but_above_the_initial_stop():
    tiers = _tiers((0.9, 0.6), (0.5, -0.1))
    assert validate_ladder(tiers, 0.75, 0.5, 0.15, 0.30) == []
    new_sl, label = next_stop(ENTRY, TARGET, 116.0, STOP0, tiers)
    assert label == "T1" and STOP0 < new_sl < ENTRY and new_sl == 97.0


def test_partial_and_lock_40_share_the_trigger():
    assert PARTIAL_EXIT_REWARD_FRAC == 0.75
    assert any(t.trigger_reward_frac == 0.75 and t.label == "LOCK_40" for t in DEFAULT_TRAIL_TIERS)
