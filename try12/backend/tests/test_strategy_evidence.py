"""
Forward paper evidence per strategy (app.services.metrics.strategy_evidence):
only trades since the rule's last change count, trades without an R are
excluded and counted, and the research gates decide the status.
"""
import random
from datetime import date, timedelta

from app.services.metrics import strategy_evidence as se
from tests.test_analytics_api import _get, api  # noqa: F401  (fixture)


def _rows(strategy, n, mean, start=date(2026, 11, 2), sd=1.0, seed=1):
    rng = random.Random(seed)
    out, d = [], start
    for i in range(n):
        if i % 2 == 0:
            d += timedelta(days=1)
        out.append({"strategy": strategy, "trading_day": d.isoformat(), "entry_time": "10:00",
                    "r_net": round(rng.gauss(mean, sd), 4)})
    return out


def test_only_trades_since_the_rule_change_count():
    old = _rows("FIRST_CANDLE_BREAKOUT", 10, 0.5, start=date(2026, 10, 1))
    new = _rows("FIRST_CANDLE_BREAKOUT", 6, -0.2)
    res = se.evaluate(old + new)["FIRST_CANDLE_BREAKOUT"]
    assert res["counted"] == 6 and res["excluded"]["before_rule_change"] == 10
    assert res["rule_since"] == "2026-11-02"            # puts-only from 2 Nov


def test_trades_without_r_are_excluded_not_zero():
    rows = _rows("ORB_BREAKOUT", 4, 0.1) + [{"strategy": "ORB_BREAKOUT", "trading_day": "2026-11-20",
                                              "entry_time": "10:00", "r_net": None}]
    res = se.evaluate(rows)["ORB_BREAKOUT"]
    assert res["counted"] == 4 and res["excluded"]["no_r"] == 1


def test_statuses_follow_the_research_gates():
    few = se.evaluate(_rows("PIVOT_BOUNCE", 20, 0.4))["PIVOT_BOUNCE"]
    assert few["status"] == "INSUFFICIENT_EVIDENCE"
    bad = se.evaluate(_rows("SUPERTREND_EMA", 300, -0.3, sd=0.6))["SUPERTREND_EMA"]
    assert bad["status"] == "DISABLED_FOR_RESEARCH"
    good = se.evaluate(_rows("ORB_BREAKOUT", 400, 0.35, sd=0.8, seed=3))["ORB_BREAKOUT"]
    assert good["status"] == "PASSES_GATES", good["reasons"]


def test_endpoint_empty_payload(api):  # noqa: F811
    r = _get("/api/v1/analytics/strategy-evidence")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["strategies"] == {} and body["empty_reason"] == "no_data_for_filter"
    assert body["criteria"]["min_trades"] == 100
    assert body["policy"]["evidence_blocked_strategies"] == ["SUPERTREND_EMA"]
    assert body["policy"]["pe_only_strategies"] == ["ORB_BREAKOUT", "PIVOT_BOUNCE", "FIRST_CANDLE_BREAKOUT",
                                                    "CPR_VWAP"]
    assert body["policy"]["max_option_buys_per_day"] == 2 and body["policy"]["regime_gating"] is False
