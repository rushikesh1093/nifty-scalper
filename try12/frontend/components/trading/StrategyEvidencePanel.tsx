"use client";
import { useQuery } from "@tanstack/react-query";
import { getStrategyEvidence } from "@/lib/tradingApi";
import { queryKeys } from "@/lib/queryKeys";
import { Panel } from "@/components/ui/Panel";
import { DataSurface } from "@/components/ui/DataSurface";
import { Skeleton } from "@/components/ui/Skeleton";
import { Badge, type BadgeIntent } from "@/components/ui/Badge";
import { STRATEGY_META } from "@/components/trading/strategyMeta";
import type { EvidenceStatus, StrategyEvidence } from "@/types/trading";

const STATUS: Record<EvidenceStatus, { label: string; intent: BadgeIntent }> = {
  PASSES_GATES:          { label: "Passes gates",          intent: "positive" },
  INSUFFICIENT_EVIDENCE: { label: "Collecting evidence",   intent: "neutral" },
  DISABLED_FOR_RESEARCH: { label: "Auto-blocked",          intent: "negative" },
};

// The five strategies the engine runs (MOMENTUM_BURST / VWAP_EMA_PULLBACK are flag-disabled).
const SHOWN = ["FIRST_CANDLE_BREAKOUT", "ORB_BREAKOUT", "EMA_RSI_PULLBACK_15M", "PIVOT_BOUNCE", "SUPERTREND_EMA"];

function label(key: string) {
  return STRATEGY_META.find((m) => m.key === key)?.label ?? key;
}

function fmtR(x: number | null | undefined) {
  return x == null ? "—" : `${x >= 0 ? "+" : ""}${x.toFixed(2)}R`;
}

/**
 * What the risk gate's evidence rules do to each strategy now (policy from TradingParams)
 * and each strategy's forward paper record since its rules last changed. A strategy needs
 * PASSES_GATES (≥ 100 trades, CI above 0, …) before it is anything more than a hypothesis.
 */
export function StrategyEvidencePanel() {
  const query = useQuery({
    queryKey: queryKeys.strategyEvidence,
    queryFn: getStrategyEvidence,
    refetchInterval: 60_000,
  });
  return (
    <Panel title="Strategy evidence">
      <DataSurface query={query} skeleton={<Skeleton label="Loading strategy evidence" />}>
        {(e: StrategyEvidence) => {
          const p = e.policy;
          return (
            <div className="space-y-2" data-testid="strategy-evidence">
              <p className="text-micro text-text-3">
                Paper trades counted since each strategy&apos;s last rule change.
                At most {p.max_option_buys_per_day > 0 ? p.max_option_buys_per_day : "∞"} option buys a day ·
                regime {p.regime_gating ? "gating" : "in shadow"}.
              </p>
              <ul className="divide-y divide-border">
                {SHOWN.map((key) => {
                  const row = e.strategies[key];
                  const blocked = p.evidence_blocked_strategies.includes(key);
                  const st = row ? STATUS[row.status] : null;
                  return (
                    <li key={key} className="flex flex-wrap items-center gap-x-2 gap-y-1 py-2" data-strategy={key}>
                      <span className="min-w-0 flex-1 text-table font-medium text-text-1">{label(key)}</span>
                      {blocked && <Badge intent="negative" title="evidence_blocked_strategies">Blocked</Badge>}
                      {p.pe_only_strategies.includes(key) && <Badge intent="info" title="pe_only_strategies">Puts only</Badge>}
                      {p.low_vol_skip_strategies.includes(key) && (
                        <Badge intent="warning" title="low_vol_skip_strategies">Skips low vol</Badge>
                      )}
                      {!blocked && st && <Badge intent={st.intent}>{st.label}</Badge>}
                      <span className="w-full font-mono text-micro text-text-3">
                        {blocked
                          ? "Not traded: negative in every variant, exit and year of research"
                          : row
                            ? `${row.counted} trades${e.rule_since[key] ? ` since ${e.rule_since[key]}` : ""} · ` +
                              `${fmtR(row.stats.expectancy_r)} · months + ${row.months_positive}`
                            : `No trades yet${e.rule_since[key] ? ` since ${e.rule_since[key]}` : ""}`}
                      </span>
                      {e.shadow?.[key] && (
                        <span className="w-full font-mono text-micro text-text-3" title="What the refused signals would have made (spot-based, net of costs)">
                          refused: {Object.entries(e.shadow[key])
                            .map(([code, s]) => `${code.toLowerCase().replace(/_/g, " ")} ${s.n} × ${fmtR(s.mean_r_net)}`)
                            .join(" · ")}
                        </span>
                      )}
                    </li>
                  );
                })}
              </ul>
            </div>
          );
        }}
      </DataSurface>
    </Panel>
  );
}
