"use client";
import { useQuery } from "@tanstack/react-query";
import {
  getDatasets, getLaunchOptions, getRun, getRunMetrics, getRunMetricsBy, getRunNoTrade,
  getRunSeries, getRunTrades, getRuns,
} from "@/lib/backtestApi";
import { queryKeys } from "@/lib/queryKeys";
import type { BinWidth, BreakdownDimension, RunsQuery, TradesQuery } from "@/types/backtest";

// Backtest read hooks (design §5.3). A finished run is immutable, so the run-scoped reads do not
// poll; only a RUNNING run detail, the runs list, the datasets and the launch options do.
const FIVE_MIN = 5 * 60_000;

/** Runs list: 30 s, or 5 s while any listed run is RUNNING or a launcher job is active. */
export function useRuns(p: RunsQuery = {}, opts: { jobActive?: boolean } = {}) {
  return useQuery({
    queryKey: queryKeys.backtestRuns(p),
    queryFn: () => getRuns(p),
    refetchInterval: (q) =>
      opts.jobActive || q.state.data?.items.some((r) => r.status === "RUNNING") ? 5_000 : 30_000,
  });
}

export function useDatasets() {
  return useQuery({
    queryKey: queryKeys.backtestDatasets,
    queryFn: getDatasets,
    refetchInterval: 30_000,
  });
}

export function useLaunchOptions() {
  return useQuery({
    queryKey: queryKeys.backtestDefaults,
    queryFn: getLaunchOptions,
    refetchInterval: 60_000,
  });
}

/** Run detail: no polling, except a RUNNING run polls at 10 s. */
export function useRun(runId: string | null, dataset?: string) {
  return useQuery({
    queryKey: queryKeys.backtestRun(dataset, runId ?? ""),
    queryFn: () => getRun(runId as string, dataset),
    enabled: !!runId,
    staleTime: FIVE_MIN,
    refetchInterval: (q) => (q.state.data?.status === "RUNNING" ? 10_000 : false),
  });
}

export function useRunMetrics(runId: string | null, dataset?: string, enabled = true) {
  return useQuery({
    queryKey: queryKeys.backtestRunMetrics(dataset, runId ?? ""),
    queryFn: () => getRunMetrics(runId as string, dataset),
    enabled: !!runId && enabled,
    staleTime: FIVE_MIN,
  });
}

export function useRunBreakdown(
  runId: string | null, dimension: BreakdownDimension, dataset?: string, enabled = true,
) {
  return useQuery({
    queryKey: queryKeys.backtestRunMetricsBy(dataset, runId ?? "", dimension),
    queryFn: () => getRunMetricsBy(runId as string, dimension, dataset),
    enabled: !!runId && enabled,
    staleTime: FIVE_MIN,
  });
}

export function useRunNoTrade(runId: string | null, dataset?: string, enabled = true) {
  return useQuery({
    queryKey: queryKeys.backtestRunNoTrade(dataset, runId ?? ""),
    queryFn: () => getRunNoTrade(runId as string, dataset),
    enabled: !!runId && enabled,
    staleTime: FIVE_MIN,
  });
}

/** Per-trade rows (tier 1 only): pass `enabled = per_trade_available` so a 409 is never requested. */
export function useRunTrades(runId: string | null, dataset?: string, p: TradesQuery = {}, enabled = true) {
  return useQuery({
    queryKey: queryKeys.backtestRunTrades(dataset, runId ?? "", p),
    queryFn: () => getRunTrades(runId as string, { ...p, dataset }),
    enabled: !!runId && enabled,
    staleTime: FIVE_MIN,
  });
}

/** Series (tier 1 only): pass `enabled = per_trade_available`. */
export function useRunSeries(runId: string | null, dataset?: string, binWidth: BinWidth = 0.25, enabled = true) {
  return useQuery({
    queryKey: queryKeys.backtestRunSeries(dataset, runId ?? "", binWidth),
    queryFn: () => getRunSeries(runId as string, dataset, binWidth),
    enabled: !!runId && enabled,
    staleTime: FIVE_MIN,
  });
}
