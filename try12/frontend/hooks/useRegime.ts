"use client";
import { useQuery } from "@tanstack/react-query";
import { getCurrentRegime, getRegimeHistory, getRegimePerformance, getRegimeV2 } from "@/lib/api";
import { queryKeys } from "@/lib/queryKeys";

export type Underlying = "NIFTY" | "BANKNIFTY";
export const REGIME_HISTORY_HOURS = 24;

/** GET /regime/current — shares its key with the StatusBar chip (NIFTY), so one request feeds both. */
export function useCurrentRegime(underlying: Underlying) {
  return useQuery({
    queryKey: queryKeys.regimeCurrent(underlying),
    queryFn: () => getCurrentRegime(underlying),
    refetchInterval: 10_000,
  });
}

/** Collapsed ascending spans over the last 24 h (design §5.5). */
export function useRegimeHistory(underlying: Underlying) {
  return useQuery({
    queryKey: queryKeys.regimeHistory(underlying, REGIME_HISTORY_HOURS),
    queryFn: () => getRegimeHistory(underlying, REGIME_HISTORY_HOURS, 500, true),
    refetchInterval: 60_000,
  });
}

export function useRegimePerformance(underlying?: Underlying) {
  return useQuery({
    queryKey: queryKeys.regimePerformance(underlying),
    queryFn: () => getRegimePerformance(underlying),
    refetchInterval: 60_000,
  });
}

/** GET /regime/v2 — the validated volatility bucket and opening-range side (shadow; gates nothing). */
export function useRegimeV2(underlying: Underlying) {
  return useQuery({
    queryKey: queryKeys.regimeV2(underlying),
    queryFn: () => getRegimeV2(underlying),
    refetchInterval: 30_000,
  });
}
