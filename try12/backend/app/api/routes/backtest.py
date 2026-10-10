"""
Backtest read API (backtest design §4.6), prefix /api/v1/backtest.

  GET /datasets                                   registered replay stores + the artefact tier
  GET /runs                                       runs merged across stores and artefacts
  GET /runs/{run_id}                              the detail envelope
  GET /runs/{run_id}/metrics                      MetricSet + DailyMetricSet + CIs
  GET /runs/{run_id}/metrics/by/{dimension}       grouped MetricSets
  GET /runs/{run_id}/no-trade-decisions/summary   counts per (stage, code)
  GET /runs/{run_id}/trades                       paginated closed trades (store tier only)
  GET /runs/{run_id}/series                       equity / exit-reason / R histogram (store tier only)

The launcher (§4.7):

  GET  /launch-options                            form limits, defaults, launchable datasets
  POST /params/validate                           admin key; 200 {valid, violations}
  POST /jobs                                      admin key; 202 LaunchResponse, 409 JOB_RUNNING
  GET  /jobs                                      key required iff one is configured
  GET  /jobs/{job_id}                             key required iff one is configured

The reads are unauthenticated (like /analytics) and run-scoped. Everything reads
the replay stores through app.db.replay_store and never the application database;
no response carries a URL, path or secret.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query

from app.api.guards import optional_admin_key, require_admin_key
from app.services.backtest import jobs, read

router = APIRouter(prefix="/backtest", tags=["backtest"])


@router.get("/datasets")
async def list_datasets():
    return await read.datasets_view()


@router.get("/runs")
async def list_runs(dataset: Optional[str] = None,
                    limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)):
    return await read.list_runs(dataset, limit, offset)


@router.get("/runs/{run_id}")
async def get_run(run_id: str, dataset: Optional[str] = None):
    return await read.run_detail(run_id, dataset)


@router.get("/runs/{run_id}/metrics")
async def get_run_metrics(run_id: str, dataset: Optional[str] = None):
    return await read.run_metrics(run_id, dataset)


@router.get("/runs/{run_id}/metrics/by/{dimension}")
async def get_run_metrics_by(run_id: str, dimension: str, dataset: Optional[str] = None):
    return await read.run_breakdown(run_id, dimension, dataset)


@router.get("/runs/{run_id}/no-trade-decisions/summary")
async def get_run_no_trade_summary(run_id: str, dataset: Optional[str] = None):
    return await read.no_trade_summary(run_id, dataset)


@router.get("/runs/{run_id}/trades")
async def get_run_trades(run_id: str, dataset: Optional[str] = None,
                         limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
                         strategy: Optional[str] = None, regime: Optional[str] = None,
                         outcome: Optional[str] = None, exit_reason: Optional[str] = None):
    return await read.trades_page(run_id, dataset, limit, offset, strategy, regime,
                                  outcome, exit_reason)


@router.get("/runs/{run_id}/series")
async def get_run_series(run_id: str, dataset: Optional[str] = None, bin_width: float = 0.25):
    return await read.run_series(run_id, dataset, bin_width)


# ── launcher ─────────────────────────────────────────────────────────────────

@router.get("/launch-options")
async def get_launch_options():
    return jobs.launch_options()


@router.post("/params/validate")
async def post_params_validate(body: jobs.ValidateRequest,
                               _: str = Depends(require_admin_key)):
    # HTTP 200 with the full list: a structured 400 detail would be truncated client-side
    violations = jobs.override_violations(body.overrides)
    return {"valid": not violations, "violations": violations}


@router.post("/jobs", status_code=202)
async def launch_job(body: jobs.LaunchRequest, who: str = Depends(require_admin_key)):
    return await jobs.launch(body, requested_by=who)


@router.get("/jobs")
async def list_jobs(_: str = Depends(optional_admin_key)):
    return jobs.list_jobs()


@router.get("/jobs/{job_id}")
async def get_job(job_id: str, _: str = Depends(optional_admin_key)):
    return jobs.get_job(job_id)
