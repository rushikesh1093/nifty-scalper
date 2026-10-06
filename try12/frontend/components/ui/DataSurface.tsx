"use client";
import type { UseQueryResult } from "@tanstack/react-query";
import { cn } from "@/lib/utils";
import { formatIst } from "@/lib/time";
import { Skeleton } from "@/components/ui/Skeleton";
import { ErrorState } from "@/components/ui/ErrorState";
import { EmptyState } from "@/components/ui/EmptyState";
import { Freshness } from "@/components/ui/Freshness";
import { useStickyQueryError } from "@/hooks/useStickyQueryError";
import { useHydrated } from "@/hooks/useHydrated";

// The loading / error / stale / empty state machine (design §6.5). Every data panel renders
// through this so a failed fetch can never render as an empty state (FR-1).

export interface SurfaceMeta {
  isStale:       boolean;
  dataUpdatedAt: number;
  isFetching:    boolean;
}

interface DataSurfaceBase<T> {
  query:       UseQueryResult<T, unknown>;
  children:    (data: T, meta: SurfaceMeta) => React.ReactNode;  // render prop, always
  skeleton?:   React.ReactNode;        // default <Skeleton />
  emptyHint?:  string;
  onRetry?:    () => void;             // default query.refetch
  className?:  string;
  /** Hide the freshness chip (e.g. when the panel header shows its own). */
  hideFreshness?: boolean;
}

// `emptyTitle` is required iff `isEmpty` is supplied (an untitled empty state is "No signals yet" again).
export type DataSurfaceProps<T> = DataSurfaceBase<T> &
  (
    | { isEmpty: (data: T) => boolean; emptyTitle: string }
    | { isEmpty?: undefined; emptyTitle?: undefined }
  );

export function DataSurface<T>(props: DataSurfaceProps<T>) {
  const { query, children, skeleton, emptyHint, onRetry, className, hideFreshness } = props;
  const { data, isPending, isFetching, isStale, dataUpdatedAt } = query;
  // Sticky: survives React Query v5 resetting a never-successful query to "pending" on each refetch,
  // so a down backend shows a steady ErrorState instead of flickering back to the skeleton.
  const { isError, error } = useStickyQueryError(query as UseQueryResult<unknown, unknown>);
  const retry = onRetry ?? (() => { void query.refetch(); });
  const fallback = skeleton ?? <Skeleton />;
  const meta: SurfaceMeta = { isStale, dataUpdatedAt, isFetching };
  const hydrated = useHydrated();

  // 0. SSR / hydration render: the server never has data, so always match its skeleton first.
  if (!hydrated) return <div className={className}>{fallback}</div>;

  // 1. pending, nothing to show yet
  if (isPending && data === undefined && !isError) return <div className={className}>{fallback}</div>;

  // 2. error and no data — the state that used to render as an empty list
  if (isError && data === undefined) {
    return (
      <div className={className}>
        <ErrorState error={error} onRetry={retry} retrying={isFetching} />
      </div>
    );
  }

  // 4. data undefined while neither pending nor errored (enabled:false, reset) → skeleton, never children
  if (data === undefined) return <div className={className}>{fallback}</div>;

  // 3. error with data — keep the good data, flag it as stale
  const staleBanner = isError && (
    <div
      role="alert"
      className="mb-2 flex flex-wrap items-center gap-2 rounded-md border border-yellow/30 bg-yellow/10 px-3 py-1.5 text-table text-yellow"
    >
      <span>Stale — last updated {formatIst(dataUpdatedAt)}, refresh failing</span>
      <button
        type="button"
        onClick={retry}
        className="ml-auto rounded border border-yellow/40 px-2 py-0.5 font-semibold hover:bg-yellow/10"
      >
        Retry
      </button>
    </div>
  );

  // 5. successful response that is genuinely empty
  if (!isError && props.isEmpty && props.isEmpty(data)) {
    return (
      <div className={className}>
        <EmptyState title={props.emptyTitle} hint={emptyHint} />
        {!hideFreshness && (
          <div className="flex justify-end">
            <Freshness dataUpdatedAt={dataUpdatedAt} isFetching={isFetching} />
          </div>
        )}
      </div>
    );
  }

  // 6. children + freshness + fetching pulse
  return (
    <div className={cn("relative", className)}>
      {staleBanner}
      {children(data, meta)}
      {!hideFreshness && (
        <div className="mt-2 flex justify-end">
          <Freshness dataUpdatedAt={dataUpdatedAt} isStale={isError} isFetching={isFetching} />
        </div>
      )}
    </div>
  );
}
