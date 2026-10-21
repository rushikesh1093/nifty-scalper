"""Option scoring: the delta and moneyness factors agree on the near-the-money strike."""
from app.services.trading import option_selector as o


def test_delta_score_is_monotonic_below_the_band():
    scores = [o._score_delta(23000, k, "CE", 5, 0.13)[0] for k in range(23050, 23400, 50)]
    deltas = [o._score_delta(23000, k, "CE", 5, 0.13)[1] for k in range(23050, 23400, 50)]
    assert deltas == sorted(deltas, reverse=True)            # further OTM, lower delta
    assert scores == sorted(scores, reverse=True)            # … and never a higher score
    assert o._score_delta(23000, 23250, "CE", 5, 0.13)[0] == 0.0


def test_nearest_strike_is_atm_for_both_sides():
    assert o._score_moneyness(23000, 23020, 50, "CE") == 100.0     # 0.4 strike ITM: still ATM
    assert o._score_moneyness(23000, 23020, 50, "PE") == 100.0
    assert o._score_moneyness(23050, 23020, 50, "CE") == 85.0      # 1 OTM
    assert o._score_moneyness(22950, 23020, 50, "CE") == 75.0      # 1 ITM
    assert o._score_moneyness(23150, 23020, 50, "CE") < o._score_moneyness(23100, 23020, 50, "CE")


def test_delta_plus_moneyness_prefers_the_atm_contract():
    def best(typ):
        return max(range(22850, 23250, 50),
                   key=lambda k: o.W_DELTA * o._score_delta(23020, k, typ, 5, 0.13)[0]
                   + o.W_MONEYNESS * o._score_moneyness(k, 23020, 50, typ))
    assert best("CE") == 23000 and best("PE") in (23000, 23050)
