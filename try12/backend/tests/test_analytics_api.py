"""Analytics API, run_id scoping, config validation and halt auth (design §17, §21, D12)."""
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from app.db.models.paper_trade import DailyPnLSummary, PaperPosition, StrategySignal, TradingMode
from app.db.models.regime import RegimePerformanceModel, RegimeStateModel
from app.db.models.risk import NoTradeDecision, RiskHalt
from tests.helpers import add_closed, get_config_row, run, seed_config
from tests.test_capital_isolation import scan_unscoped

BACKEND = Path(__file__).resolve().parents[1]
RPL = "RPL-01TESTRUN0000000000000000"[:30]

ANALYTICS_GETS = [
    "/api/v1/analytics/trades", "/api/v1/analytics/metrics",
    "/api/v1/analytics/metrics/by/strategy", "/api/v1/analytics/metrics/by/day",
    "/api/v1/analytics/no-trade-decisions", "/api/v1/analytics/no-trade-decisions/summary",
    "/api/v1/analytics/runs",
]


def _client():
    from app.main import app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _get(path, **params):
    async def _go():
        async with _client() as c:
            return await c.get(path, params=params)
    return run(_go())


def _call(method, path, **kw):
    async def _go():
        async with _client() as c:
            return await c.request(method, path, **kw)
    return run(_go())


@pytest.fixture
def api(runtime_ctx, sqlite_db, fake_redis):
    run(seed_config(sqlite_db, capital=200_000.0, max_lots_per_trade=3, max_open_positions=3,
                    max_daily_loss=6_000.0, max_daily_loss_pct=3.0, max_trades_per_day=6))
    return sqlite_db


def _seed_two_runs(Session, rc):
    """One PAPER and one RPL- row in every run-shared table the endpoints read."""
    at = rc.clock.now_utc() - timedelta(minutes=10)
    for run_id, net in (("PAPER", 500.0), (RPL, -700.0)):
        run(add_closed(Session, net=net, at=at, run_id=run_id, strategy="ORB_VWAP",
                       initial_risk=15.0, r_source="ENTRY", r_multiple=1.0 if net > 0 else -1.0,
                       entry_regime="BULL_TREND_NORMAL_VOL"))

    async def _rest():
        async with Session() as db:
            for run_id in ("PAPER", RPL):
                db.add(StrategySignal(strategy_name="ORB_VWAP", underlying="NIFTY",
                                      direction="BUY", spot_price=22600.0, signal_time=at,
                                      run_id=run_id, skip_reason=run_id))
                # /trading/pnl* read the wall-clock date (app/api is not ctx-clocked)
                db.add(DailyPnLSummary(date=date.today(), strategy_name="ORB_VWAP",
                                       mode=TradingMode.PAPER, total_trades=1, winning_trades=0,
                                       losing_trades=0, gross_pnl=0.0, brokerage=0.0,
                                       net_pnl=1.0 if run_id == "PAPER" else -1.0, run_id=run_id))
                db.add(RegimePerformanceModel(regime="BULL_TREND_NORMAL_VOL", underlying="NIFTY",
                                              strategy_name="ORB_VWAP", total_trades=1,
                                              winning_trades=1, losing_trades=0, total_pnl=1,
                                              run_id=run_id))
                db.add(RegimeStateModel(underlying="NIFTY", regime="BULL_TREND_NORMAL_VOL"
                                        if run_id == "PAPER" else "RANGE_NORMAL_VOL",
                                        # /regime/history windows on the wall clock
                                        timestamp=datetime.now(timezone.utc) - timedelta(minutes=1),
                                        run_id=run_id))
                db.add(NoTradeDecision(run_id=run_id, trading_day=rc.clock.trading_day(),
                                       decided_at=at, stage="CYCLE", code="ATR_BELOW_FLOOR",
                                       underlying="NIFTY", reason=run_id, context={}))
            await db.commit()
    run(_rest())


# ── empty states ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", ANALYTICS_GETS)
def test_every_analytics_route_returns_a_well_formed_empty_payload(api, path):
    r = _get(path)
    assert r.status_code == 200, r.text
    body = r.json()
    if "items" in body:
        assert body["items"] == [] and body["total"] == 0
        assert body["empty_reason"] == "no_data_for_filter"
    elif "metrics" in body:
        m = body["metrics"]
        assert m["trades"] == 0 and m["low_sample"] is True
        assert m["win_rate"] is None and m["expectancy_r"] is None     # null, never 0.0
    else:
        assert body["groups"] == {} and body["empty_reason"] == "no_data_for_filter"


def test_risk_state_payload(api, runtime_ctx):
    r = _get("/api/v1/analytics/risk-state")
    assert r.status_code == 200, r.text
    b = r.json()
    assert (b["run_id"], b["mode"]) == ("PAPER", "PAPER")
    assert b["capital"] == runtime_ctx.cfg.capital and b["used_capital"] == 0.0
    assert b["trade_count"] == 0 and b["daily_loss"]["breached"] is False
    assert set(b["streak"]) >= {"consecutive_losses", "halt_n", "halted_strategies",
                                "cooldown_until"}
    assert b["config_valid"] is True and b["entry_cutoff_ist"] == "15:05"


def test_unknown_run_and_bad_params_are_400_or_404(api):
    assert _get("/api/v1/analytics/trades", run_id="LIVE").status_code == 400
    assert _get("/api/v1/analytics/trades", run_id="RPL-has space").status_code == 400
    assert _get("/api/v1/analytics/trades", strategy="PCR_SIGNAL").status_code == 400
    assert _get("/api/v1/analytics/trades", date_from="2026-09-10",
                date_to="2026-09-01").status_code == 400
    assert _get("/api/v1/analytics/metrics/by/colour").status_code == 400
    assert _get("/api/v1/analytics/runs/RPL-NOPE").status_code == 404
    assert _get("/api/v1/trading/positions", run_id="x").status_code == 400


# ── mixed-run isolation (D12) ────────────────────────────────────────────────

def test_no_endpoint_returns_rows_from_another_run(api, runtime_ctx):
    _seed_two_runs(api, runtime_ctx)
    for run_id, net in (("PAPER", 500.0), (RPL, -700.0)):
        trades = _get("/api/v1/analytics/trades", run_id=run_id).json()
        assert trades["total"] == 1 and {t["run_id"] for t in trades["items"]} == {run_id}
        assert trades["items"][0]["net_pnl"] == net
        m = _get("/api/v1/analytics/metrics", run_id=run_id).json()["metrics"]
        assert m["trades"] == 1 and m["expectancy_inr"] == net
        by = _get("/api/v1/analytics/metrics/by/strategy", run_id=run_id).json()
        assert by["groups"]["ORB_VWAP"]["trades"] == 1
        ntd = _get("/api/v1/analytics/no-trade-decisions", run_id=run_id).json()
        assert [x["reason"] for x in ntd["items"]] == [run_id]
        summ = _get("/api/v1/analytics/no-trade-decisions/summary", run_id=run_id).json()
        assert summ["items"] == [{"stage": "CYCLE", "code": "ATR_BELOW_FLOOR", "count": 1}]

        pos = _get("/api/v1/trading/positions", run_id=run_id).json()
        assert [p["net_pnl"] for p in pos] == [net]
        sig = _get("/api/v1/trading/signals", run_id=run_id, today_only="false").json()
        assert [s["skip_reason"] for s in sig] == [run_id]
        pnl = _get("/api/v1/trading/pnl", run_id=run_id).json()
        assert pnl["total_pnl"] == (1.0 if run_id == "PAPER" else -1.0)
        hist = _get("/api/v1/trading/pnl/history", run_id=run_id).json()
        assert [h["net_pnl"] for h in hist] == [1.0 if run_id == "PAPER" else -1.0]
        perf = _get("/api/v1/regime/performance", run_id=run_id).json()
        assert len(perf) == 1
        hist = _get("/api/v1/regime/history", run_id=run_id, collapse="false").json()
        assert [h["regime"] for h in hist] == \
            ["BULL_TREND_NORMAL_VOL" if run_id == "PAPER" else "RANGE_NORMAL_VOL"]
    # the default is the live run
    assert _get("/api/v1/analytics/trades").json()["items"][0]["run_id"] == "PAPER"


# ── static run_id-predicate scan ─────────────────────────────────────────────

def _scan(root: Path, **kw) -> dict:
    out = {}
    for p in sorted(root.rglob("*.py")):
        hits = scan_unscoped(p.read_text(encoding="utf-8"), **kw)
        if hits:
            out[p.relative_to(BACKEND).as_posix()] = hits
    return out


def test_api_trading_and_script_writes_are_run_scoped():
    assert _scan(BACKEND / "app" / "api") == {}
    assert _scan(BACKEND / "app" / "services" / "trading") == {}
    assert _scan(BACKEND / "scripts", writes_only=True) == {}


def test_the_scan_fails_on_a_synthetic_unscoped_script_delete():
    assert scan_unscoped("from x import delete\nq = delete(PaperPosition)\n",
                         writes_only=True) == [2]


# ── config writes ────────────────────────────────────────────────────────────

def test_put_config_out_of_range_is_400_and_persists_nothing(api):
    before = run(get_config_row(api)).max_daily_loss_pct
    r = _call("PUT", "/api/v1/trading/config", json={"max_daily_loss_pct": 150.0})
    assert r.status_code == 400, r.text
    assert any("max_daily_loss_pct" in v for v in r.json()["detail"]["violations"])
    assert run(get_config_row(api)).max_daily_loss_pct == before


def test_put_config_reports_effective_from_per_key(api):
    r = _call("PUT", "/api/v1/trading/config", json={"max_trades_per_day": 5, "slippage_pct": 0.1})
    assert r.status_code == 200, r.text
    eff = r.json()["effective_from"]
    assert eff == {"max_trades_per_day": "next_process_start", "slippage_pct": "within_30s"}
    assert r.json()["pending_restart"]["max_trades_per_day"]["stored"] == 5


def test_trade_amount_converts_to_fixed_fraction_and_keeps_its_keys(api):
    r = _call("PUT", "/api/v1/trading/capital/trade-amount",
              json={"trade_amount": 20000, "capital": 200000})
    assert r.status_code == 200, r.text
    b = r.json()
    assert {"status", "trade_amount", "capital", "available_capital", "preview"} <= set(b)
    assert isinstance(b["preview"], list) and b["trade_amount"] == 20000.0
    assert b["fixed_fraction_pct"] == pytest.approx(10.0)
    assert run(get_config_row(api)).fixed_fraction_pct == pytest.approx(10.0)
    full = _call("PUT", "/api/v1/trading/capital/trade-amount",
                 json={"trade_amount": 200000, "capital": 200000})   # all capital: allowed
    assert full.status_code == 200, full.text
    assert full.json()["fixed_fraction_pct"] == pytest.approx(100.0)
    bad = _call("PUT", "/api/v1/trading/capital/trade-amount",
                json={"trade_amount": 210000, "capital": 200000})    # 105% > the 100% ceiling
    assert bad.status_code == 400
    assert any("fixed_fraction_pct" in v and "(0, 100]" in v
               for v in bad.json()["detail"]["violations"])


def test_capital_set_is_pending_until_restart(api, runtime_ctx):
    r = _call("PUT", "/api/v1/trading/capital/set", params={"capital": 150000})
    assert r.status_code == 200, r.text
    assert r.json()["effective_from"] == "next_process_start"
    cfg = _get("/api/v1/trading/config").json()
    assert cfg["pending_restart"]["capital"] == {"stored": 150000.0,
                                                 "running": runtime_ctx.cfg.capital}
    assert cfg["deprecated"] == {"regime_transition_window": True}
    assert cfg["config_valid"] is True and "run_config" in cfg and "params" in cfg


# ── halt control and the admin key (§17.4) ───────────────────────────────────

@pytest.fixture
def no_key(monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "ADMIN_API_KEY", None)


@pytest.fixture
def with_key(monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "ADMIN_API_KEY", "k-SENTINEL-ADMIN-9f2")
    monkeypatch.setattr(settings, "ADMIN_API_KEY_LABEL", "ops")
    return {"X-API-Key": "k-SENTINEL-ADMIN-9f2"}


async def _halts(Session):
    async with Session() as db:
        return (await db.execute(select(RiskHalt))).scalars().all()


def test_engage_is_allowed_without_a_key_and_release_is_503(api, no_key):
    r = _call("POST", "/api/v1/trading/halt", json={"reason": "operator stop"})
    assert r.status_code == 200, r.text
    assert r.json()["halted"] is True
    (h,) = run(_halts(api))
    assert (h.halt_type, h.reason, h.released_at) == ("MANUAL", "operator stop", None)
    for path in ("/api/v1/trading/halt/release", "/api/v1/trading/kill-switch/release",
                 "/api/v1/trading/capital/reset-daily", "/api/v1/analytics/reconcile"):
        resp = _call("POST", path)
        assert resp.status_code == 503 and resp.json() == {"detail": "admin key not configured"}
    resp = _call("POST", "/api/v1/trading/clear-test-trades", json={"run_id": "PAPER"})
    assert resp.status_code == 503


def test_release_with_the_key_records_the_label_never_the_key(api, with_key):
    assert _call("POST", "/api/v1/trading/halt").status_code == 401        # key configured
    assert _call("POST", "/api/v1/trading/halt", headers=with_key).status_code == 200
    assert _call("POST", "/api/v1/trading/halt/release").status_code == 401
    assert _call("POST", "/api/v1/trading/halt/release",
                 headers={"X-API-Key": "wrong"}).status_code == 401
    r = _call("POST", "/api/v1/trading/halt/release", headers=with_key,
              json={"halt_type": "MANUAL", "note": "checked"})
    assert r.status_code == 200, r.text
    assert r.json()["halted"] is False
    (h,) = run(_halts(api))
    assert (h.released_by, h.release_note) == ("ops", "checked") and h.released_at is not None
    assert "SENTINEL" not in r.text


def _add_order_tables(Session):
    from app.db.base import Base
    from app.db.models.order import Order
    from app.db.models.trade import Trade

    async def _go():
        async with Session() as db:
            conn = await db.connection()
            await conn.run_sync(lambda c: Base.metadata.create_all(
                c, tables=[Order.__table__, Trade.__table__]))
            await db.commit()
    run(_go())


def test_clear_test_trades_requires_a_run_id_and_deletes_only_that_run(api, runtime_ctx, with_key):
    _seed_two_runs(api, runtime_ctx)
    _add_order_tables(api)
    assert _call("POST", "/api/v1/trading/clear-test-trades", headers=with_key).status_code == 400
    assert _call("POST", "/api/v1/trading/clear-test-trades", headers=with_key,
                 json={"run_id": "DROP TABLE"}).status_code == 400
    r = _call("POST", "/api/v1/trading/clear-test-trades", headers=with_key, json={"run_id": RPL})
    assert r.status_code == 200, r.text
    assert r.json()["positions_deleted"] == 1

    async def _runs():
        async with api() as db:
            return sorted({p.run_id for p in (await db.execute(select(PaperPosition))).scalars()})
    assert run(_runs()) == ["PAPER"]


def test_reconcile_with_the_key(api, runtime_ctx, with_key):
    _seed_two_runs(api, runtime_ctx)
    r = _call("POST", "/api/v1/analytics/reconcile", headers=with_key, params={"run_id": RPL})
    assert r.status_code == 200, r.text
    assert r.json()["run_id"] == RPL


# ── credentials never leave the process ──────────────────────────────────────

def test_credential_sentinels_are_absent_from_payloads(api, runtime_ctx, monkeypatch):
    from app.core.config import settings
    sentinels = {"ANGEL_API_KEY": "SENTINEL-API-1", "ANGEL_CLIENT_ID": "SENTINEL-CID-2",
                 "ANGEL_PASSWORD": "SENTINEL-PWD-3", "ANGEL_TOTP_SECRET": "SENTINEL-TOTP-4",
                 "POSTGRES_PASSWORD": "SENTINEL-PG-5", "ADMIN_API_KEY": "SENTINEL-ADM-6"}
    for k, v in sentinels.items():
        monkeypatch.setattr(settings, k, v)
    _seed_two_runs(api, runtime_ctx)
    paths = ANALYTICS_GETS + ["/api/v1/analytics/risk-state", "/api/v1/trading/config",
                              "/api/v1/trading/positions", "/api/v1/trading/signals"]
    for path in paths:
        r = _get(path)
        assert r.status_code == 200, (path, r.text)
        for v in sentinels.values():
            assert v not in r.text, (path, v)
