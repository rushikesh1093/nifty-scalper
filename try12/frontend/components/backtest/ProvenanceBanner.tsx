import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/Badge";
import type { SyntheticSource, SyntheticState } from "@/types/backtest";

// Run provenance (design §4.9, C6). There are exactly two states and neither claims the data is
// genuine market evidence: SYNTHETIC (generated data) or UNVERIFIED (provenance not recorded).

const SOURCE_TEXT: Record<SyntheticSource, string> = {
  validation: "the run's validation report marks it synthetic",
  dataset:    "it was read from a generated synthetic dataset",
  label:      "its label says synthetic",
  none:       "no provenance was recorded for it",
};

export function provenanceTitle(state: SyntheticState, source: SyntheticSource): string {
  return state === "SYNTHETIC"
    ? `Synthetic data (${SOURCE_TEXT[source]}) — not evidence of edge`
    : "Data provenance not recorded — treat as unverified";
}

/** Compact chip for tables: SYNTHETIC (negative) / UNVERIFIED (warning). */
export function ProvenanceChip({ state, source }: { state: SyntheticState; source: SyntheticSource }) {
  return (
    <Badge
      intent={state === "SYNTHETIC" ? "negative" : "warning"}
      status={state}
      title={provenanceTitle(state, source)}
    >
      {state}
    </Badge>
  );
}

/** Full-width banner shown above everything on a run view. */
export function ProvenanceBanner({
  state, source, className,
}: { state: SyntheticState; source: SyntheticSource; className?: string }) {
  const synthetic = state === "SYNTHETIC";
  return (
    <div
      role="note"
      data-status={state}
      className={cn(
        "rounded-lg border px-4 py-3",
        synthetic ? "border-red/40 bg-red/10" : "border-yellow/40 bg-yellow/10",
        className,
      )}
    >
      <div className={cn("text-body font-semibold", synthetic ? "text-red" : "text-yellow")}>
        {synthetic ? "SYNTHETIC — not evidence of edge" : "Data provenance not recorded — treat as unverified"}
      </div>
      <div className="mt-0.5 text-table text-text-2">
        {synthetic
          ? `This run is labelled synthetic because ${SOURCE_TEXT[source]}. Its results describe generated data and say nothing about live market performance.`
          : "Nothing recorded with this run says where its market data came from, so its results cannot be relied on as evidence of edge."}
      </div>
    </div>
  );
}
