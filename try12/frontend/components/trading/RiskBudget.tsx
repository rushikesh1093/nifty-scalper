"use client";
import { useQuery } from "@tanstack/react-query";
import { getRiskState } from "@/lib/tradingApi";
import { useHydrated } from "@/hooks/useHydrated";
import { queryKeys } from "@/lib/queryKeys";
import { cn } from "@/lib/utils";
import type { RiskState } from "@/types/trading";

// Today's risk budget at a glance: how much of the daily loss cap is used (realised +
// open MTM), how many of the day's trades are used, and the losing streak against its
// halt. The cap shown is the binding one: min(₹ cap, % cap × capital).

const inr = (v: number) => `${v < 0 ? "−" : ""}₹${Math.abs(Math.round(v)).toLocaleString("en-IN")}`;

function Meter({ label, used, total, display, tone, hint }: {
  label: string; used: number; total: number; display: string; tone: "ok" | "warn" | "bad"; hint?: string;
}) {
  const pct = total > 0 ? Math.min(100, Math.max(0, (used / total) * 100)) : 0;
  const bar = tone === "bad" ? "bg-red" : tone === "warn" ? "bg-yellow" : "bg-green";
  return (
    <div className="min-w-0 rounded-lg border border-border bg-surface-2 px-3 py-2">
      <div className="flex items-baseline justify-between gap-2">
        <span className="text-micro font-semibold uppercase text-text-3">{label}</span>
        <span className="font-mono text-table tabular-nums text-text-1">{display}</span>
      </div>
      <div
        role="meter"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={total}
        aria-valuenow={Math.min(used, total)}
        aria-valuetext={display}
        className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-surface-3"
      >
        <div className={cn("h-full rounded-full transition-[width]", bar)} style={{ width: `${pct}%` }} />
      </div>
      {hint && <div className="mt-1 text-micro text-text-3">{hint}</div>}
    </div>
  );
}

export function RiskBudgetMeters({ r }: { r: RiskState }) {
  const dl = r.daily_loss;
  const cap = Math.min(dl.threshold_abs, (dl.threshold_pct / 100) * r.capital);
  const loss = Math.max(0, -dl.combined);              // only losses consume the budget
  const lossTone = dl.breached || loss >= cap ? "bad" : loss >= 0.6 * cap ? "warn" : "ok";
  const tradesTone = r.trade_count >= r.max_trades_per_day ? "bad"
    : r.trade_count >= r.max_trades_per_day - 1 ? "warn" : "ok";
  const streak = r.streak;
  const streakTone = streak.global_halt || streak.consecutive_losses >= streak.halt_n ? "bad"
    : streak.consecutive_losses >= streak.halt_n - 1 ? "warn" : "ok";
  return (
    <div className="grid grid-cols-1 gap-2 sm:grid-cols-3" aria-label="Today's risk budget">
      <Meter
        label="Daily loss used"
        used={loss}
        total={cap}
        display={`${inr(loss)} / ${inr(cap)}`}
        tone={lossTone}
        hint={`realised ${inr(dl.realised)} · open ${inr(dl.unrealised)} · ${inr(Math.max(0, cap - loss))} left`}
      />
      <Meter
        label="Trades today"
        used={r.trade_count}
        total={r.max_trades_per_day}
        display={`${r.trade_count} / ${r.max_trades_per_day}`}
        tone={tradesTone}
        hint={`new entries until ${r.entry_cutoff_ist} · square-off ${r.square_off_ist}`}
      />
      <Meter
        label="Losing streak"
        used={streak.consecutive_losses}
        total={streak.halt_n}
        display={`${streak.consecutive_losses} / ${streak.halt_n}`}
        tone={streakTone}
        hint={streak.cooldown_minutes_remaining
          ? `cooldown ${Math.ceil(streak.cooldown_minutes_remaining)} min after a loss`
          : `trading halts at ${streak.halt_n} losses in a row`}
      />
    </div>
  );
}

export function RiskBudget() {
  const q = useQuery({ queryKey: queryKeys.riskState, queryFn: getRiskState, refetchInterval: 10_000 });
  const hydrated = useHydrated();          // the risk-state query is shared with the status strip
  if (!hydrated || !q.data) return null;          // the stat tiles above already show loading / errors
  return <RiskBudgetMeters r={q.data} />;
}
