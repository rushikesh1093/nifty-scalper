import { cn } from "@/lib/utils";
import { formatIst } from "@/lib/time";

// "updated HH:mm:ss" + stale / fetching indicators. dataUpdatedAt is a client-only value (React
// Query never has data during SSR here), so formatting it in render cannot cause a hydration mismatch.
export function Freshness({
  dataUpdatedAt, isStale, isFetching, className,
}: { dataUpdatedAt: number; isStale?: boolean; isFetching?: boolean; className?: string }) {
  if (!dataUpdatedAt) return null;
  return (
    <span className={cn("inline-flex items-center gap-1.5 font-mono text-micro text-text-3", className)}>
      {isFetching && <span aria-hidden="true" className="h-1.5 w-1.5 rounded-full bg-accent animate-pulse-soft" />}
      <span>updated {formatIst(dataUpdatedAt)}</span>
      {isStale && <span className="text-yellow">· stale</span>}
      {isFetching && <span className="sr-only">refreshing</span>}
    </span>
  );
}
