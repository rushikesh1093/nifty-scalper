"""The artefact tier through the backtest API (backtest design §4.3, §4.6, §10).

Three real report artefacts are copied into tmp_path: the 118-trade synthetic
baseline, the 4-trade ₹15,000 run (the R4 case: synthetic but labelled as if
real) and the zero-tradeable-day real-data run."""
import json
import shutil
from pathlib import Path

import httpx
import pytest

from app.core.config import settings
from app.db import replay_store
from app.services.backtest import artifacts
from tests.replay_fixtures import run

BASELINE = "RPL-01M4GCZX80SBAR1TXKC8KSP3WE"
FOUR = "RPL-01M4GM4G8715T96KC6GB2P8FM7"
ZERO = "RPL-01M4G383WHJ8TQACYBD9C05BMM"
API = "/api/v1/backtest"


def _real_runs() -> Path:
    for parent in Path(__file__).resolve().parents:
        d = parent / ".agents" / "tasks" / "paper-trading-hardening" / "runs"
        if d.is_dir():
            return d
    pytest.skip("real replay artefacts not available")


def _get(path, **params):
    from app.main import app

    async def _go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as c:
            return await c.get(path, params=params)
    return run(_go())


def _isolate(monkeypatch, tmp_path, art_dir):
    monkeypatch.setattr(settings, "REPLAY_DATABASE_URL", None)
    monkeypatch.setattr(settings, "REPLAY_DATASET_DIR", str(tmp_path / "datasets"))
    monkeypatch.setattr(settings, "REPLAY_ARTIFACT_DIR", str(art_dir))
    replay_store.reset_cache()
    artifacts.reset_cache()


@pytest.fixture
def art(tmp_path, monkeypatch):
    src = _real_runs()
    d = tmp_path / "runs"
    d.mkdir()
    for rid in (BASELINE, FOUR, ZERO):
        if not (src / f"{rid}.json").is_file():
            pytest.skip(f"{rid}.json not available")
        shutil.copy2(src / f"{rid}.json", d / f"{rid}.json")
    _isolate(monkeypatch, tmp_path, d)
    yield d
    replay_store.reset_cache()
    artifacts.reset_cache()


def _rows():
    r = _get(f"{API}/runs")
    assert r.status_code == 200, r.text
    return r.json(), {i["run_id"]: i for i in r.json()["items"]}


def test_list_rows_are_derived_from_the_artefacts(art):
    body, rows = _rows()
    assert set(rows) == {BASELINE, FOUR, ZERO} and body["total"] == 3
    assert body["warnings"] == [] and body["truncated"] is False and body["empty_reason"] is None
    b = rows[BASELINE]
    assert b["trades"] == 118 and b["win_rate"] == pytest.approx(0.5254, abs=1e-4)
    assert b["failing_criteria"] == [3, 4] and b["sessions"] == 25
    assert b["trades_per_day"] == pytest.approx(4.72)
    assert (b["synthetic_state"], b["synthetic_source"]) == ("SYNTHETIC", "validation")
    assert b["label"] == "synthetic-acceptance-test" and b["status"] == "COMPLETE"
    f = rows[FOUR]
    assert f["trades"] == 4 and (f["synthetic_state"], f["synthetic_source"]) == ("UNVERIFIED", "none")
    z = rows[ZERO]
    assert z["trades"] == 0 and z["win_rate"] is None and z["max_drawdown_pct"] is None
    assert z["recommendation"] == "NOT YET" and z["failing_criteria"] == [1, 2, 3, 4, 5, 6]
    assert z["sessions"] == 0 and z["trades_per_day"] is None and z["status"] == "NO_DATA"
    for row in rows.values():
        assert row["dataset"] == "artifacts" and row["created_at_source"] == "file_mtime"
        assert row["determinism_hash"] is None and row["per_trade_available"] is False
        assert row["metrics_available"] is True and row["breakdowns_available"] is True
        assert row["validation_source"] == "artifact"
    assert str(art) not in _get(f"{API}/runs").text


def test_datasets_list_the_artefact_tier(art):
    r = _get(f"{API}/datasets")
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["items"] == [{"dataset_id": "artifacts", "kind": "artifacts",
                           "label": "Report artefacts (read-only)", "synthetic": False,
                           "exists": True, "runs": 3, "error": None}]
    assert b["replay_store_configured"] is False and b["replay_store_error"] == "not_configured"
    assert b["replay_db_alias_of"] is None


def test_metrics_from_the_artefact(art):
    m = _get(f"{API}/runs/{BASELINE}/metrics").json()
    assert m["source"] == "artifact" and m["dataset"] == "artifacts"
    assert m["mean_cost_inr"] is None and m["cost_sample_r"] == 0 and m["cost_sample_inr"] == 0
    assert m["sessions"] == 25 and m["capital"] == 200_000.0
    assert m["win_rate_wilson_95"] == pytest.approx([0.4360, 0.6133], abs=1e-4)
    assert m["ci_source"] == "validation" and m["expectancy_r_ci_day_block"][0] is not None
    assert m["metrics"]["trades"] == 118 and m["daily"]["trading_days"] == 25
    z = _get(f"{API}/runs/{ZERO}/metrics", dataset="artifacts").json()
    assert z["sessions"] == 0 and z["trades_per_day"] is None
    assert z["win_rate_wilson_95"] == [None, None] and z["empty_reason"] == "no_data_for_filter"
    f = _get(f"{API}/runs/{FOUR}/metrics").json()
    assert f["capital"] == 15_000.0 and f["metrics"]["trades"] == 4


def test_detail_from_the_artefact(art):
    d = _get(f"{API}/runs/{FOUR}").json()
    assert d["validation_source"] == "artifact" and d["dataset"] == "artifacts"
    assert d["synthetic_state"] == "UNVERIFIED" and d["label"] == "capital-15000-A-proposed"
    assert d["capital"] == 15_000.0 and d["params"]["cfg"]["capital"] == 15_000.0
    assert d["per_trade_available"] is False and d["metrics_row"] is None
    assert d["oos_split"] == 0.7 and d["warnings"] == [] and d["validation"]["run_id"] == FOUR


def test_breakdowns_and_no_trade_summary_from_the_artefact(art):
    by = _get(f"{API}/runs/{BASELINE}/metrics/by/regime")
    assert by.status_code == 200 and by.json()["source"] == "artifact"
    assert len(by.json()["groups"]) == 5 and by.json()["total"] == 5
    day = _get(f"{API}/runs/{BASELINE}/metrics/by/day")
    assert day.status_code == 400 and "per-trade rows" in day.json()["detail"]
    assert _get(f"{API}/runs/{BASELINE}/metrics/by/colour").status_code == 400
    z = _get(f"{API}/runs/{ZERO}/metrics/by/strategy").json()
    assert z["groups"] == {} and z["empty_reason"] == "no_data_for_filter"
    ntd = _get(f"{API}/runs/{BASELINE}/no-trade-decisions/summary").json()
    assert ntd["source"] == "artifact" and ntd["total"] == len(ntd["items"]) > 0
    counts = [x["count"] for x in ntd["items"]]
    assert counts == sorted(counts, reverse=True)
    assert sum(s["count"] for s in ntd["by_stage"]) == sum(counts)


def test_per_trade_routes_are_409_on_the_artefact_tier(art):
    for path in ("trades", "series"):
        r = _get(f"{API}/runs/{BASELINE}/{path}")
        assert r.status_code == 409, (path, r.text)


def test_bad_run_ids_are_400(art):
    assert _get(f"{API}/runs/PAPER").status_code == 400
    assert _get(f"{API}/runs/RPL-..x").status_code == 400
    assert _get(f"{API}/runs/RPL-NOPE").status_code == 404
    assert artifacts.load("../RPL-x") is None and artifacts.load("PAPER") is None


def test_missing_artifact_dir_is_a_warning_not_an_error(tmp_path, monkeypatch):
    not_a_dir = tmp_path / "file.txt"
    not_a_dir.write_text("x")
    _isolate(monkeypatch, tmp_path, not_a_dir)
    assert artifacts.artifact_dir() is None
    r = _get(f"{API}/runs")
    assert r.status_code == 200, r.text
    assert r.json()["warnings"] == ["artifacts_unavailable"]
    assert r.json()["items"] == [] and r.json()["empty_reason"] == "no_runs"
    assert _get(f"{API}/datasets").json()["items"] == []


def test_unreadable_artefacts_are_non_fatal(art, monkeypatch):
    (art / "RPL-BADJSON.json").write_text("{not json", encoding="utf-8")
    (art / "RPL-NOSECTIONS.json").write_text(json.dumps({"run_id": "RPL-NOSECTIONS"}),
                                             encoding="utf-8")
    big = art / "RPL-BIG.json"
    big.write_text(json.dumps({"run_id": "RPL-BIG", "sections": {}, "pad": "x" * 300_000}),
                   encoding="utf-8")
    monkeypatch.setattr(artifacts, "MAX_ARTIFACT_BYTES", 200_000)   # the real ones are < 90 KB
    artifacts.reset_cache()
    _, rows = _rows()
    assert set(rows) == {BASELINE, FOUR, ZERO}
    for rid in ("RPL-BADJSON", "RPL-NOSECTIONS", "RPL-BIG"):
        assert artifacts.load(rid) is None
        assert _get(f"{API}/runs/{rid}").status_code == 404


def test_the_scan_is_bounded(art, monkeypatch):
    monkeypatch.setattr(settings, "BACKTEST_MAX_ARTIFACT_FILES", 2)
    body, rows = _rows()
    assert len(rows) == 2 and body["truncated"] is True and body["caps"] == {"artifacts": 2}
