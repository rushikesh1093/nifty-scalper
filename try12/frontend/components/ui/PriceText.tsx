import { cn, formatPrice } from "@/lib/utils";

// Every price / quantity / P&L / percentage renders through these (design §6.3): font-mono
// tabular-nums so columns do not jitter, and direction is carried by a ▲/▼ glyph *and* colour.

export function PriceText({
  value, decimals = 2, prefix, className,
}: { value: number | null | undefined; decimals?: number; prefix?: string; className?: string }) {
  return (
    <span className={cn("font-mono tabular-nums", className)}>
      {value == null ? "—" : `${prefix ?? ""}${formatPrice(value, decimals)}`}
    </span>
  );
}

export interface DeltaTextProps {
  value:     number | null | undefined;
  decimals?: number;
  /** Appended after the number, e.g. "%". */
  suffix?:   string;
  /** Prepended to the magnitude, e.g. "₹". */
  prefix?:   string;
  /** Hide the ▲/▼ glyph (only when an adjacent element already carries direction). */
  noGlyph?:  boolean;
  className?: string;
}

export function DeltaText({ value, decimals = 2, suffix = "", prefix = "", noGlyph, className }: DeltaTextProps) {
  if (value == null || Number.isNaN(value)) {
    return <span className={cn("font-mono tabular-nums text-text-3", className)}>—</span>;
  }
  const dir = value > 0 ? "up" : value < 0 ? "down" : "flat";
  const magnitude = `${prefix}${formatPrice(Math.abs(value), decimals)}${suffix}`;
  const sign = dir === "up" ? "+" : dir === "down" ? "−" : "";
  const glyph = dir === "up" ? "▲" : dir === "down" ? "▼" : "";
  const label = dir === "flat" ? `unchanged ${magnitude}` : `${dir} ${magnitude}`;
  return (
    <span
      aria-label={label}
      className={cn(
        "inline-flex items-center gap-0.5 font-mono tabular-nums",
        dir === "up" && "text-up",
        dir === "down" && "text-down",
        dir === "flat" && "text-text-2",
        className,
      )}
    >
      {!noGlyph && glyph && <span aria-hidden="true" className="text-[0.8em]">{glyph}</span>}
      <span aria-hidden="true">{sign}{magnitude}</span>
    </span>
  );
}
