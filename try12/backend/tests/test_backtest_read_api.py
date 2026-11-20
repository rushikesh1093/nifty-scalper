"""The backtest read API against real replay stores (backtest design §4.2, §4.6, §10).

The four_trade session fixture's SQLite file is a real replay store (full
schema, RPL- runs with closed trades, no-trade rows, and live-shaped PAPER
rows). The registry cases build tiny stores with create_sqlite_schema in
tmp_path. Nothing here connects to Postgres or Redis."""
import logging
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from app.core.config import settings
from app.core.trading_params import TradingParams
from app.db import replay_store
from app.services.backtest import artifacts
from tests.replay_fixtures import run

API = "/api/v1/backtest"
SID = "synthetic-5d-s1-2026-09-01"


def _get(path, **params):
    from app.main import app

    async def _go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as c:
            return await c.get(path, params=params)
    return run(_go())


def _url(p: Path) -> str:
    return f"sqlite+aiosqlite:///{p.as_posix()}"


def _settings(monkeypatch, tmp_path, url):
    monkeypatch.setattr(settings, "REPLAY_DATABASE_URL", url)
    monkeypatch.setattr(settings, "REPLAY_DATASET_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "REPLAY_ARTIFACT_DIR", str(tmp_path / "no-artifacts"))
    replay_store.reset_cache()
    artifacts.reset_cache()


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    def _set(url):
        _settings(monkeypatch, tmp_path, url)
    yield _set
    replay_store.reset_cache()
    artifacts.reset_cache()


@pytest.fixture
def store(four_trade, tmp_path, monkeypatch):
    """REPLAY_DATABASE_URL → the four_trade store. Only the registry's own engines are
    dropped on teardown; four_trade's engines are never disposed here."""
    _settings(monkeypatch, tmp_path / "datasets", four_trade.url_a)
    yield four_trade
    replay_store.reset_cache()
    artifacts.reset_cache()


def _make_store(path: Path, *rows: dict) -> Path:
    from app.db.models.replay import ReplayRun
    from app.services.replay.cli import create_sqlite_schema

    path.parent.mkdir(parents=True, exist_ok=True)
    create_sqlite_schema(_url(path))
    eng = create_engine(f"sqlite:///{path.as_posix()}")
    try:
        with eng.begin() as c:
            for r in rows:
                c.execute(ReplayRun.__table__.insert().values(**r))
    finally:
        eng.dispose()
    return path


def _row(run_id: str, label: str = "plain") -> dict:
    return {"run_id": run_id, "created_at": datetime(2026, 10, 1, tzinfo=timezone.utc),
            "status": "COMPLETE", "params": {"params": {}, "cfg": {"capital": 15_000.0},
                                             "label": label},
            "coverage": {"days": [{"day": "2026-09-01", "status": "TRADEABLE"}]},
            "metrics": {"trades": 0}, "seed": 1, "underlyings": ["NIFTY"]}


# ── four_trade integration ───────────────────────────────────────────────────

def test_runs_list_reads_the_store(store):
    r = _get(f"{API}/runs")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["warnings"] == ["artifacts_unavailable"] and body["truncated"] is False
    rows = {i["run_id"]: i for i in body["items"]}
    assert {store.run1.run_id, store.run2.run_id, store.run3.run_id} <= set(rows)
    assert "PAPER" not in rows
    one = rows[store.run1.run_id]
    m = store.run1.metrics
    assert one["dataset"] == "replay-db" and one["label"] == "test" and one["status"] == "COMPLETE"
    assert one["trades"] == m.trades and one["win_rate"] == m.win_rate
    assert one["expectancy_r"] == m.expectancy_r and one["expectancy_inr"] == m.expectancy_inr
    assert one["max_drawdown_pct"] == m.max_equity_drawdown_pct
    assert one["determinism_hash"] == store.run1.determinism_hash
    assert one["recommendation"] is None and one["failing_criteria"] is None and one["caps"] is None
    assert one["validation_source"] == "none" and one["created_at_source"] == "row"
    assert (one["synthetic_state"], one["synthetic_source"]) == ("UNVERIFIED", "none")
    assert one["sessions"] == 1 and one["trades_per_day"] == m.trades
    assert one["per_trade_available"] is True
    assert store.url_a not in r.text and "a.db" not in r.text


def test_detail_metrics_and_breakdowns(store):
    rid = store.run1.run_id
    d = _get(f"{API}/runs/{rid}").json()
    assert d["dataset"] == "replay-db" and d["label"] == "test"
    assert d["validation"] is None and d["validation_source"] == "none" and d["warnings"] == []
    assert d["capital"] == TradingParams().capital and d["sessions"] == 1
    assert d["determinism_hash"] == store.run1.determinism_hash
    assert d["params"]["label"] == "test" and d["oos_split"] is None
    m = _get(f"{API}/runs/{rid}/metrics", dataset="replay-db").json()
    assert m["source"] == "store" and m["metrics"]["trades"] == store.run1.metrics.trades
    assert m["metrics"]["low_sample"] is True and m["cost_sample_inr"] == m["metrics"]["trades"]
    assert m["ci_source"] == "none" and m["expectancy_r_ci_day_block"] is None
    if m["cost_sample_r"] == m["metrics"]["r_sample_size"]:
        assert m["mean_cost_r"] == pytest.approx(
            m["metrics"]["expectancy_r"] - m["metrics"]["expectancy_r_net"], abs=1e-9)
    by = _get(f"{API}/runs/{rid}/metrics/by/strategy").json()
    assert set(by["groups"]) == {"VWAP_EMA_PULLBACK", "SUPERTREND_EMA"} and by["source"] == "store"
    assert sum(g["trades"] for g in by["groups"].values()) == store.run1.metrics.trades
    day = _get(f"{API}/runs/{rid}/metrics/by/day").json()
    assert list(day["groups"]) == ["2026-09-01"]


def test_no_trade_summary_trades_and_series(store):
    rid = store.run1.run_id
    ntd = _get(f"{API}/runs/{rid}/no-trade-decisions/summary").json()
    assert ntd["source"] == "store" and ntd["total"] == len(ntd["items"]) > 0
    counts = [x["count"] for x in ntd["items"]]
    assert counts == sorted(counts, reverse=True)
    n = store.run1.metrics.trades
    t = _get(f"{API}/runs/{rid}/trades").json()
    assert t["total"] == n and {i["run_id"] for i in t["items"]} == {rid}
    assert all("outcome" in i and "r_valid" in i for i in t["items"])
    assert len(_get(f"{API}/runs/{rid}/trades", limit=2).json()["items"]) == 2
    sl = _get(f"{API}/runs/{rid}/trades", exit_reason="sl").json()
    assert sl["total"] == sum(1 for i in t["items"] if i["exit_reason"] == "SL") > 0
    assert _get(f"{API}/runs/{rid}/trades", outcome="maybe").status_code == 400
    s = _get(f"{API}/runs/{rid}/series").json()
    assert s["trades"] == n and len(s["equity"]) == n
    assert s["max_drawdown_inr"] == pytest.approx(store.run1.metrics.max_equity_drawdown_inr)
    assert sum(b["count"] for b in s["exit_reasons"]) == n
    h = s["r_histogram"]
    assert sum(b["count"] for b in h["bins"]) + h["clipped_low"] + h["clipped_high"] == h["r_valid"]
    assert _get(f"{API}/runs/{rid}/series", bin_width=0.3).status_code == 400


def test_paper_and_bad_ids_and_datasets(store):
    # the store genuinely holds live-shaped PAPER rows; the rule refuses them anyway
    for path in ("", "/metrics", "/trades", "/series", "/no-trade-decisions/summary"):
        r = _get(f"{API}/runs/PAPER{path}")
        assert r.status_code == 400 and r.json()["detail"] == "backtest routes serve replay runs only"
    assert _get(f"{API}/runs/RPL-DOESNOTEXIST").status_code == 404
    assert _get(f"{API}/runs", dataset="../x").status_code == 400
    assert _get(f"{API}/runs", dataset="synthetic-1d-s1-2026-09-01").status_code == 404
    assert _get(f"{API}/runs/{store.run1.run_id}", dataset="artifacts").status_code == 404


def test_the_store_session_is_read_only(store):
    ds = replay_store.resolve("replay-db")

    async def _raw_write():
        async with replay_store.engine_for(ds).connect() as conn:
            await conn.execute(text("UPDATE replay_runs SET notes = 'x'"))

    async def _session_write():
        async with replay_store.store_session(ds) as db:
            await db.execute(text("UPDATE replay_runs SET notes = 'x'"))
    with pytest.raises(OperationalError, match="readonly"):
        run(_raw_write())
    with pytest.raises(HTTPException) as e:                # mapped at the session boundary
        run(_session_write())
    assert e.value.status_code == 503


# ── registry identity ────────────────────────────────────────────────────────

def test_replay_db_alias_of_a_synthetic_file(tmp_path, isolated, caplog):
    f = _make_store(tmp_path / f"{SID}.db", _row("RPL-ALIAS1"))
    isolated(_url(f))
    b = _get(f"{API}/datasets").json()
    assert [(i["dataset_id"], i["kind"], i["runs"]) for i in b["items"]] == [(SID, "synthetic", 1)]
    assert b["replay_db_alias_of"] == SID and b["replay_store_error"] is None
    rows = _get(f"{API}/runs").json()["items"]
    assert [(r["run_id"], r["dataset"], r["synthetic_state"], r["synthetic_source"])
            for r in rows] == [("RPL-ALIAS1", SID, "SYNTHETIC", "dataset")]
    d = _get(f"{API}/runs/RPL-ALIAS1", dataset="replay-db").json()
    assert (d["dataset"], d["synthetic_state"], d["synthetic_source"]) == (SID, "SYNTHETIC", "dataset")
    assert d["capital"] == 15_000.0
    assert str(tmp_path) not in _get(f"{API}/datasets").text

    def _alias_lines():
        return [r for r in caplog.records if "same store as dataset" in r.getMessage()]
    replay_store.reset_cache()
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="app.db.replay_store"):
        for _ in range(3):
            assert _get(f"{API}/runs").status_code == 200
        assert len(_alias_lines()) == 1
        replay_store.reset_cache()
        _get(f"{API}/runs")
        assert len(_alias_lines()) == 2


def test_same_name_inside_and_outside_the_dataset_dir(tmp_path, isolated):
    _make_store(tmp_path / f"{SID}.db", _row("RPL-INSIDE1"))
    outside = _make_store(tmp_path / "other" / f"{SID}.db", _row("RPL-OUTSIDE1"))
    isolated(_url(outside))
    b = _get(f"{API}/datasets").json()
    assert [(i["dataset_id"], i["kind"]) for i in b["items"]] == \
        [("replay-db", "replay_db"), (SID, "synthetic")]
    assert b["replay_db_alias_of"] is None
    rows = {r["run_id"]: r for r in _get(f"{API}/runs").json()["items"]}
    assert (rows["RPL-OUTSIDE1"]["dataset"], rows["RPL-OUTSIDE1"]["synthetic_state"]) == \
        ("replay-db", "UNVERIFIED")
    assert (rows["RPL-INSIDE1"]["dataset"], rows["RPL-INSIDE1"]["synthetic_state"]) == \
        (SID, "SYNTHETIC")
    # outside only
    (tmp_path / f"{SID}.db").unlink()
    b = _get(f"{API}/datasets").json()
    assert [(i["dataset_id"], i["kind"]) for i in b["items"]] == [("replay-db", "replay_db")]
    assert b["replay_db_alias_of"] is None
    rows = _get(f"{API}/runs").json()["items"]
    assert [(r["run_id"], r["synthetic_state"]) for r in rows] == [("RPL-OUTSIDE1", "UNVERIFIED")]


def test_replay_db_not_generated_yet_reads_as_empty(tmp_path, isolated):
    final = tmp_path / f"{SID}.db"
    isolated(_url(final))
    merged = _get(f"{API}/runs")
    assert merged.status_code == 200, merged.text
    assert merged.json()["items"] == []
    assert not any(w.startswith("store_unreachable") for w in merged.json()["warnings"])
    only = _get(f"{API}/runs", dataset="replay-db").json()
    assert (only["items"], only["total"], only["dataset"], only["empty_reason"]) == \
        ([], 0, "replay-db", "replay_store_not_initialised")
    items = _get(f"{API}/datasets").json()["items"]
    assert [(i["dataset_id"], i["exists"], i["runs"]) for i in items] == [("replay-db", False, 0)]
    assert _get(f"{API}/runs/RPL-X1", dataset="replay-db").status_code == 404
    assert not final.exists() and list(tmp_path.glob("synthetic-*.db")) == []


def test_an_empty_sqlite_file_is_503_not_500(tmp_path, isolated):
    sid = "synthetic-1d-s1-2026-09-01"
    (tmp_path / f"{sid}.db").write_bytes(b"")
    isolated(None)
    r = _get(f"{API}/runs", dataset=sid)
    assert r.status_code == 503 and r.json() == {"detail": f"replay store '{sid}' unreachable"}
    merged = _get(f"{API}/runs")
    assert merged.status_code == 200 and f"store_unreachable:{sid}" in merged.json()["warnings"]
    d = _get(f"{API}/datasets").json()
    assert d["items"][0]["runs"] is None and d["items"][0]["error"] == "unreachable"
    assert _get(f"{API}/runs/RPL-X1", dataset=sid).status_code == 503


def test_a_refused_replay_database_url_is_never_opened(tmp_path, isolated):
    isolated(settings.DATABASE_URL)
    b = _get(f"{API}/datasets").json()
    assert b["items"] == [] and b["replay_store_error"] == "refused_main_db"
    r = _get(f"{API}/runs", dataset="replay-db")
    assert r.status_code == 503 and r.json()["detail"] == "replay_store_misconfigured"
    assert _get(f"{API}/runs").status_code == 200
