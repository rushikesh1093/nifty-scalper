import { cn } from "@/lib/utils";

export type StatIntent = "neutral" | "positive" | "negative" | "warning" | "info";

const VALUE_COLOUR: Record<StatIntent, string> = {
  neutral: "text-text-1", positive: "text-up", negative: "text-down", warning: "text-yellow", info: "text-blue",
};

export function StatTile({
  label, value, delta, hint, intent = "neutral", className,
}: {
  label:   string;
  value:   React.ReactNode;
  /** Usually a <DeltaText>. */
  delta?:  React.ReactNode;
  hint?:   React.ReactNode;
  intent?: StatIntent;
  className?: string;
}) {
  return (
    <div className={cn("rounded-lg border border-border bg-surface-2 px-3 py-2.5", className)}>
      <div className="text-micro font-semibold uppercase text-text-3">{label}</div>
      <div className={cn("mt-0.5 font-mono text-hero font-bold tabular-nums", VALUE_COLOUR[intent])}>{value}</div>
      {delta && <div className="text-table">{delta}</div>}
      {hint && <div className="mt-0.5 text-micro normal-case tracking-normal text-text-3">{hint}</div>}
    </div>
  );
}
