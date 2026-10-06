"use client";
import { useEffect, useState } from "react";
import type { UseQueryResult } from "@tanstack/react-query";

/**
 * React Query v5 resets a query that has never succeeded back to `status: "pending"` (error: null,
 * failureReason: null) at the start of every refetch / refetchInterval tick. With the backend down
 * that makes every error state flicker back to a skeleton / neutral chip for the duration of the
 * retry cycle (seconds per poll). This hook keeps the last error visible until a fetch succeeds:
 * `errorUpdatedAt` is not reset by a refetch, so "failed since the last success" survives the reset.
 */
export function useStickyQueryError(query: UseQueryResult<unknown, unknown>): { error: unknown; isError: boolean } {
  // failureReason (set between retries) only matters while there is no data: a query with data is
  // never reset to pending, so its own isError/error are already stable.
  const current = query.error ?? (query.data === undefined ? query.failureReason : null) ?? null;
  const [last, setLast] = useState<unknown>(null);

  useEffect(() => {
    if (current) setLast(current);
    else if (query.isSuccess) setLast(null);
  }, [current, query.isSuccess]);

  const failedSinceSuccess = query.errorUpdatedAt > 0 && query.errorUpdatedAt >= query.dataUpdatedAt;
  const error = current ?? (failedSinceSuccess && !query.isSuccess ? last : null);
  return { error, isError: error != null };
}
