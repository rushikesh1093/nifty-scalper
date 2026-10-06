"use client";
import { useQuery } from "@tanstack/react-query";
import { getDiagnostics } from "@/lib/tradingApi";
import { queryKeys } from "@/lib/queryKeys";

/** Signal-pipeline diagnostics, polled every 15 s (design §6.4). Shared cache across consumers. */
export function useDiagnostics() {
  return useQuery({
    queryKey: queryKeys.diagnostics,
    queryFn: getDiagnostics,
    refetchInterval: 15_000,
  });
}
