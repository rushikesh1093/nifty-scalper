import { cn } from "@/lib/utils";

// The single status-chip primitive (DP-3 / D11). Colour is never the only carrier of meaning:
// the chip always carries text, and `data-status` exposes the raw status for tests/contrast checks.
export type BadgeIntent = "neutral" | "positive" | "negative" | "warning" | "info" | "accent";
export type BadgeStrength = "subtle" | "solid" | "outline";

const INTENT: Record<BadgeIntent, { fg: string; bg: string; border: string; solidFg: string }> = {
  neutral:  { fg: "text-text-2", bg: "bg-surface-3",   border: "border-border-2",  solidFg: "text-bg" },
  positive: { fg: "text-green",  bg: "bg-green/10",    border: "border-green/40",  solidFg: "text-bg" },
  negative: { fg: "text-red",    bg: "bg-red/10",      border: "border-red/40",    solidFg: "text-bg" },
  warning:  { fg: "text-yellow", bg: "bg-yellow/10",   border: "border-yellow/40", solidFg: "text-bg" },
  info:     { fg: "text-blue",   bg: "bg-blue/10",     border: "border-blue/40",   solidFg: "text-bg" },
  accent:   { fg: "text-accent", bg: "bg-accent/15",   border: "border-accent/40", solidFg: "text-text-1" },
};

const SOLID_BG: Record<BadgeIntent, string> = {
  neutral: "bg-text-2", positive: "bg-green", negative: "bg-red",
  warning: "bg-yellow", info: "bg-blue", accent: "bg-accent",
};

const DOT_BG: Record<BadgeIntent, string> = {
  neutral: "bg-text-3", positive: "bg-green", negative: "bg-red",
  warning: "bg-yellow", info: "bg-blue", accent: "bg-accent",
};

export interface BadgeProps {
  children:  React.ReactNode;
  intent?:   BadgeIntent;
  strength?: BadgeStrength;
  /** Raw status value, rendered as data-status (e.g. "SKIPPED"). */
  status?:   string;
  /** Leading status dot. `pulse` animates it (live states). */
  dot?:      boolean | "pulse";
  title?:    string;
  className?: string;
}

export function Badge({
  children, intent = "neutral", strength = "subtle", status, dot, title, className,
}: BadgeProps) {
  const c = INTENT[intent];
  return (
    <span
      data-status={status}
      title={title}
      className={cn(
        "inline-flex items-center gap-1 whitespace-nowrap rounded px-1.5 py-0.5 text-micro font-semibold uppercase",
        strength === "subtle"  && [c.fg, c.bg],
        strength === "outline" && [c.fg, "border bg-transparent", c.border],
        strength === "solid"   && [c.solidFg, SOLID_BG[intent]],
        className,
      )}
    >
      {dot && (
        <span
          aria-hidden="true"
          className={cn(
            "h-1.5 w-1.5 shrink-0 rounded-full",
            strength === "solid" ? "bg-current" : DOT_BG[intent],
            dot === "pulse" && "animate-pulse-soft",
          )}
        />
      )}
      {children}
    </span>
  );
}
