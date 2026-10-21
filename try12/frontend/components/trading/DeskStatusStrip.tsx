"use client";
import { useQuery } from "@tanstack/react-query";
import { useDiagnostics } from "@/hooks/useDiagnostics";
import { useHydrated } from "@/hooks/useHydrated";
import { getRiskState, getTrendDecisions } from "@/lib/tradingApi";
import { queryKeys } from "@/lib/queryKeys";
import { cn } from "@/lib/utils";
import { regimeLabel, regimeTone } from "@/components/regime/regimeMeta";
import type { SignalDiagnostics } from "@/types/diagnostics";
import type { RiskState } from "@/types/trading";

// One line answering "is the desk trading right now, and if not, why?" — market, who
// decides, the regime per index and whether any halt is engaged. Read-only: every value
// comes from an existing endpoint, and an unknown value shows as "—", never as "OK".

type Tone = "ok" | "warn" | "bad" | "muted";
const DOT: Record<Tone, string> = { ok: "bg-green", warn: "bg-yellow", bad: "bg-red", muted: "bg-text-3" };
const TEXT: Record<Tone, string> = { ok: "text-text-1", warn: "text-yellow", bad: "text-red", muted: "text-text-2" };

function Item({ label, value, tone, title }: { label: string; value: React.ReactNode; tone: Tone; title?: string }) {
  return (
    <div className="flex min-w-0 items-center gap-2 px-3 py-2" title={title}>
      <span aria-hidden="true" className={cn("h-2 w-2 shrink-0 rounded-full", DOT[tone])} />
      <div className="min-w-0">
        <div className="text-micro font-semibold uppercase text-text-3">{label}</div>
        <div className={cn("truncate text-table font-semibold", TEXT[tone])}>{value}</div>
      </div>
    </div>
  );
}

function market(d: SignalDiagnostics | undefined): { value: string; tone: Tone; title?: string } {
  if (!d) return { value: "—", tone: "muted" };
  if (d.session_window_open) return { value: `Open · ${d.session_window}`, tone: "ok" };
  const closed = (d.blockers ?? []).find((b) => b.code === "MARKET_CLOSED");
  const detail = closed?.detail ?? "";
  const holiday = /NSE holiday \(([^)]+)\)/.exec(detail);
  if (holiday) return { value: `Closed · ${holiday[1]}`, tone: "muted", title: detail };
  if (/weekend/.test(detail)) return { value: "Closed · weekend", tone: "muted", title: detail };
  return { value: "Closed", tone: "muted", title: detail || undefined };
}

function trading(r: RiskState | undefined): { value: string; tone: Tone; title?: string } {
  if (!r) return { value: "—", tone: "muted" };
  if (r.kill_switch?.halted) return { value: "Kill switch on", tone: "bad", title: r.kill_switch.reason ?? undefined };
  if (r.daily_loss?.breached) return { value: "Halted · daily loss cap", tone: "bad" };
  if (r.streak?.global_halt) return { value: `Halted · ${r.streak.consecutive_losses} losses in a row`, tone: "bad" };
  if (r.trade_count >= r.max_trades_per_day) return { value: "Halted · trade cap reached", tone: "warn" };
  if (r.streak?.cooldown_minutes_remaining) {
    return { value: `Cooldown · ${Math.ceil(r.streak.cooldown_minutes_remaining)} min`, tone: "warn" };
  }
  return { value: "Active", tone: "ok" };
}

export function DeskStatusStrip() {
  const diag = useDiagnostics();
  const risk = useQuery({ queryKey: queryKeys.riskState, queryFn: getRiskState, refetchInterval: 10_000 });
  const engine = useQuery({ queryKey: queryKeys.trendDecisions, queryFn: () => getTrendDecisions(40), refetchInterval: 10_000 });

  // The diagnostics query is shared with the layout's StatusBar, so it can already hold data
  // at hydration; render data only after mount so the markup matches the server HTML.
  const hydrated = useHydrated();
  const d = hydrated ? diag.data : undefined;
  const riskData = hydrated ? risk.data : undefined;
  const engineData = hydrated ? engine.data : undefined;
  const m = market(d);
  const t = trading(riskData);
  const unified = engineData?.decision_engine === "UNIFIED";
  const regimes = (d?.underlyings ?? []).map((u) => ({
    name: u.underlying,
    regime: u.regime,
    allows: u.regime_allows_trading,
  }));

  return (
    <section
      aria-label="Desk status"
      className="panel grid grid-cols-2 divide-border sm:grid-cols-3 lg:flex lg:flex-wrap lg:divide-x"
    >
      <Item label="Market" value={m.value} tone={m.tone} title={m.title} />
      <Item
        label="Decides trades"
        value={engineData ? (unified ? "Trend engine" : "12 legacy strategies") : "—"}
        tone={engineData ? "ok" : "muted"}
        title={unified ? "One BUY CE / BUY PE / NO TRADE decision per index per candle" : undefined}
      />
      <Item label="Mode" value={d?.mode ?? "—"} tone={d?.mode === "PAPER" ? "ok" : d?.mode ? "bad" : "muted"} />
      {regimes.map((r) => (
        <Item
          key={r.name}
          label={`${r.name} regime`}
          value={<span className={r.regime ? regimeTone(r.regime) : undefined}>{regimeLabel(r.regime)}</span>}
          tone={r.regime == null ? "muted" : r.allows === false ? "warn" : "ok"}
          title={r.allows === false ? "This regime enables no strategy" : undefined}
        />
      ))}
      <Item label="Trading" value={t.value} tone={t.tone} title={t.title} />
    </section>
  );
}
