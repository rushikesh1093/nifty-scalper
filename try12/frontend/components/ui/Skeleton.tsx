import { cn } from "@/lib/utils";

// Loading placeholders. Each top-level skeleton is a polite status region with an sr-only label,
// and keeps the final layout's footprint so data arrival causes no layout shift.

export function Skeleton({ className, label = "Loading" }: { className?: string; label?: string }) {
  return (
    <div role="status" aria-live="polite" className={cn("space-y-2", className)}>
      <span className="sr-only">{label}…</span>
      <div aria-hidden="true" className="shimmer h-3 w-2/3" />
      <div aria-hidden="true" className="shimmer h-3 w-1/2" />
      <div aria-hidden="true" className="shimmer h-3 w-3/4" />
    </div>
  );
}

export function TableSkeleton({ rows = 5, cols = 6, className }: { rows?: number; cols?: number; className?: string }) {
  return (
    <div role="status" aria-live="polite" className={cn("w-full", className)}>
      <span className="sr-only">Loading table…</span>
      <div aria-hidden="true" className="space-y-2">
        <div className="grid gap-3" style={{ gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))` }}>
          {Array.from({ length: cols }, (_, c) => <div key={c} className="shimmer h-2.5 opacity-60" />)}
        </div>
        {Array.from({ length: rows }, (_, r) => (
          <div key={r} className="grid gap-3" style={{ gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))` }}>
            {Array.from({ length: cols }, (_, c) => <div key={c} className="shimmer h-4" />)}
          </div>
        ))}
      </div>
    </div>
  );
}

export function ChartSkeleton({ height = 340, className }: { height?: number; className?: string }) {
  return (
    <div role="status" aria-live="polite" className={cn("w-full", className)} style={{ height }}>
      <span className="sr-only">Loading chart…</span>
      <div aria-hidden="true" className="shimmer h-full w-full rounded-lg opacity-60" />
    </div>
  );
}
