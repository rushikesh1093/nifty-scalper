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

All reads, all unauthenticated (like /analytics), all run-scoped. They read the
replay stores through app.db.replay_store and never the application database;
no response carries a URL, path or secret.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query

from app.services.backtest import read

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
