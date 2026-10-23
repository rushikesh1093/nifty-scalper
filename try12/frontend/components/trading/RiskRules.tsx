"use client";
import { useQuery } from "@tanstack/react-query";
import { getConfig } from "@/lib/tradingApi";
import { queryKeys } from "@/lib/queryKeys";
import type { TradingConfig } from "@/types/trading";

// The entry rules that now decide a trade's size and timing, read from the resolved
// TradingParams (GET /trading/config → params.resolved) so the panel shows what the
// backend actually enforces, not a copy of the defaults.

const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);

function windows(v: unknown): string {
  if (!Array.isArray(v) || !v.length) return "none";
  return v
    .filter((w): w is [string, string] => Array.isArray(w) && w.length === 2)
    .map(([a, b]) => `${a}–${b}`)
    .join(", ");
}

function Row({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <li className="flex items-baseline justify-between gap-3">
      <span className="text-text-2">{label}</span>
      <span className="text-right font-mono text-text-1" title={hint}>{value}</span>
    </li>
  );
}

function Rules({ cfg }: { cfg: TradingConfig }) {
  const p = cfg.params?.resolved ?? {};
  const riskPct = num(p.max_risk_per_trade_pct);
  const riskRs = riskPct !== null ? (riskPct / 100) * cfg.capital : null;
  const otm = num(p.max_otm_strikes);
  const streak = num(cfg.losing_streak_halt_n ?? p.losing_streak_halt_n);
  const kelly = num(cfg.kelly_min_trades ?? p.kelly_min_trades);
  return (
    <section aria-labelledby="risk-rules-title" className="space-y-2 border-t border-border pt-3 text-table">
      <h3 id="risk-rules-title" className="text-micro uppercase tracking-wide text-text-3">Entry rules</h3>
      <ul className="space-y-1.5">
        <Row
          label="Max loss per trade"
          value={riskRs !== null ? `₹${Math.round(riskRs).toLocaleString("en-IN")} (${riskPct}%)` : "—"}
          hint="Lots are cut so a premium stop-out cannot lose more than this"
        />
        <Row
          label="Strikes"
          value={otm === null ? "—" : otm === 0 ? "ATM only" : `ATM or ${otm} OTM`}
        />
        <Row label="No new entries" value={windows(p.entry_blackout_windows)} hint="Exits still run" />
        <Row label="Stop after" value={streak !== null ? `${streak} losses in a row` : "—"} />
        <Row label="Square-off" value={cfg.square_off_time || "—"} />
        <Row
          label="Kelly sizing"
          value={kelly !== null ? `after ${kelly} trades` : "—"}
          hint="Until then each strategy uses the fixed fraction"
        />
      </ul>
    </section>
  );
}

export function RiskRules() {
  const query = useQuery({ queryKey: queryKeys.config, queryFn: getConfig, refetchInterval: 15_000 });
  if (!query.data) return null;
  return <Rules cfg={query.data} />;
}
