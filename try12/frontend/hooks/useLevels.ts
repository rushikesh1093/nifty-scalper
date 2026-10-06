"use client";
import { useQuery } from "@tanstack/react-query";
import { getLevels } from "@/lib/levelsApi";
import { queryKeys } from "@/lib/queryKeys";
import type { CandleInterval } from "@/types/market";

export const LEVELS_MAX = 12;

/**
 * Backend S/R levels for an underlying at a timeframe, polled every 60 s (design §6.4). The chart
 * overlay and LevelsPanel share this key, so both render the same response from one request.
 */
export function useLevels(underlying: "NIFTY" | "BANKNIFTY", interval: CandleInterval, maxLevels = LEVELS_MAX) {
  return useQuery({
    queryKey: queryKeys.levels(underlying, interval, maxLevels),
    queryFn: () => getLevels(underlying, interval, maxLevels),
    refetchInterval: 60_000,
    staleTime: 30_000,
  });
}
