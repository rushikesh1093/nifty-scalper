"use client";
import { useQuery } from "@tanstack/react-query";
import { getTrendDecisions } from "@/lib/tradingApi";
import { queryKeys } from "@/lib/queryKeys";
import type { TrendDecision, TrendDaySummary } from "@/types/trading";
import { Panel } from "@/components/ui/Panel";
import { DataSurface } from "@/components/ui/DataSurface";
import { Badge, type BadgeIntent } from "@/components/ui/Badge";

// TREND_ENGINE: one BUY CE / BUY PE / NO TRADE decision per underlying per completed
// candle, with the reason. Every level shown here is in INDEX points; the option
// contract, premium stop/target and live trailing stop of an opened trade are in the
// Positions table (premium points).

// Plain-language reason for each NO TRADE code (backend trend_engine reason codes).
const REASON_TEXT: Record<string, string> = {
  SIDEWAYS_MARKET:       "sideways market",
  CONFLICTING_SIGNALS:   "signals disagree",
  WEAK_TREND:            "trend too weak",
  NO_ENTRY_CONFIRMATION: "waiting for a pullback / breakout entry",
  EXTENDED_MOVE:         "move already extended (no chasing)",
  LOW_VOLATILITY:        "too quiet to reach target",
  NO_ROOM_TO_TARGET:     "level in the way of the target",
  STOP_TOO_WIDE:         "stop would be too wide",
  STALE_DATA:            "data stale",
  INSUFFICIENT_DATA:     "not enough candles yet",
  MALFORMED_DATA:        "bad candle data",
  OUTSIDE_ENTRY_WINDOW:  "outside entry hours",
};
const reasonText = (code: string) => REASON_TEXT[code] ?? code.replace(/_/g, " ").toLowerCase();

function DaySummary({ u, s }: { u: string; s: TrendDaySummary | undefined }) {
  if (!s || s.evaluated === 0) {
    return <div className="text-table text-text-2"><span className="font-semibold text-text-1">{u}</span> · no decisions yet today</div>;
  }
  return (
    <div className="space-y-1 text-table">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className="font-semibold text-text-1">{u}</span>
        <span className="text-text-2">{s.evaluated} candles checked</span>
        <span className="text-green">{s.buy_ce} BUY CE</span>
        <span className="text-red">{s.buy_pe} BUY PE</span>
        <span className="text-text-2">{s.no_trade} no trade</span>
      </div>
      {s.top_reasons.length > 0 && (
        <div className="text-text-2">
          Mostly no trade because:{" "}
          {s.top_reasons.slice(0, 3).map(([code, n], i) => (
            <span key={code}>
              {i > 0 && " · "}
              <span className="text-text-1">{reasonText(code)}</span> ({Math.round((100 * n) / Math.max(1, s.no_trade))}%)
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

const trendIntent = (t: string): BadgeIntent =>
  t === "UPTREND" ? "positive" : t === "DOWNTREND" ? "negative" : "neutral";
const decisionIntent = (d: string): BadgeIntent =>
  d === "BUY CE" ? "positive" : d === "BUY PE" ? "negative" : "warning";
const n2 = (v: number | null | undefined) => (typeof v === "number" ? v.toFixed(2) : "—");
const hhmm = (ts: string) => (ts && ts.length >= 16 ? ts.slice(11, 16) : ts);

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex gap-2 text-table">
      <span className="w-36 shrink-0 text-text-2">{label}</span>
      <span className="min-w-0 text-text-1">{children}</span>
    </div>
  );
}

function LatestCard({ u, d }: { u: string; d: TrendDecision | null }) {
  if (!d) {
    return (
      <div className="rounded-md border border-border p-3 text-table text-text-2">
        <div className="font-semibold text-text-1">{u}</div>
        No decision yet (needs ≥ 30 completed candles and an enabled regime).
      </div>
    );
  }
  const trade = d.decision !== "NO TRADE";
  const sgn = d.decision === "BUY CE" ? 1 : -1;
  return (
    <div className="space-y-1.5 rounded-md border border-border p-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-semibold text-text-1">{u}</span>
        <Badge intent={trendIntent(d.trend)}>{d.trend}</Badge>
        <Badge intent="neutral" strength="outline">{d.strength}</Badge>
        <Badge intent={decisionIntent(d.decision)} strength="solid">{d.decision}</Badge>
        <span className="ml-auto font-mono text-table text-text-2">candle {hhmm(d.candle_time)}</span>
      </div>
      <Row label="Spot (index)">{n2(d.spot)}</Row>
      {trade ? (
        <>
          <Row label="Entry trigger">{d.entry_trigger}</Row>
          <Row label="Entry zone (index)">{d.entry_zone ? `${n2(d.entry_zone[0])} – ${n2(d.entry_zone[1])}` : "—"}</Row>
          <Row label="Stop-loss (index)">{n2(d.stop_level)} ({n2(-sgn * (d.stop_points ?? 0))} pts)</Row>
          <Row label="Target 1 (index)">{n2(d.target1_level)} ({n2(sgn * (d.target1_points ?? 0))} pts)</Row>
          <Row label="Target 2 (index)">
            {d.target2_level != null ? `${n2(d.target2_level)} (${n2(sgn * (d.target2_points ?? 0))} pts) — reference only` : "— not justified"}
          </Row>
          <Row label="Risk : reward">{d.risk_reward != null ? `1 : ${d.risk_reward.toFixed(2)}` : "—"}</Row>
          <Row label="Trailing SL">{d.trailing_rule}</Row>
          <Row label="Invalidation">{d.invalidation}</Row>
          <Row label="Option / premium">selected at entry — see Positions (premium points)</Row>
        </>
      ) : (
        <Row label="Reason">
          {d.reason_code && <span className="text-text-1">{reasonText(d.reason_code)}</span>} <span className="text-text-2">— {d.reason}</span>
        </Row>
      )}
      {d.evidence?.length > 0 && (
        <Row label="Evidence">
          <ul className="list-disc pl-4">
            {d.evidence.map((e) => <li key={e}>{e}</li>)}
          </ul>
        </Row>
      )}
    </div>
  );
}

export function TrendDecisionPanel() {
  const query = useQuery({ queryKey: queryKeys.trendDecisions, queryFn: () => getTrendDecisions(40), refetchInterval: 10_000 });
  return (
    <Panel title="Trend engine decisions" meta={<span className="text-table text-text-2">index points · paper only</span>}>
      <DataSurface query={query} hideFreshness>
        {(r) => (
          <div className="space-y-3">
            {r.decision_engine !== "UNIFIED" && (
              <div role="alert" className="rounded-md border border-border px-3 py-2 text-table text-text-2">
                decision_engine = {r.decision_engine}: the 12 legacy strategies are trading; the trend engine is idle.
              </div>
            )}
            <div className="space-y-1.5 rounded-md border border-border bg-surface-2 px-3 py-2" aria-label="Today so far">
              {["NIFTY", "BANKNIFTY"].map((u) => <DaySummary key={u} u={u} s={r.summary?.[u]} />)}
            </div>
            <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
              {["NIFTY", "BANKNIFTY"].map((u) => <LatestCard key={u} u={u} d={r.latest[u] ?? null} />)}
            </div>
            <div className="max-h-64 overflow-auto rounded-md border border-border">
              <table className="w-full text-table">
                <thead className="sticky top-0 bg-surface-2 text-text-2">
                  <tr>
                    <th className="px-2 py-1 text-left">Candle</th>
                    <th className="px-2 py-1 text-left">Index</th>
                    <th className="px-2 py-1 text-left">Trend</th>
                    <th className="px-2 py-1 text-left">Decision</th>
                    <th className="px-2 py-1 text-left">Reason</th>
                  </tr>
                </thead>
                <tbody>
                  {[...r.items].reverse().map((d) => (
                    <tr key={`${d.underlying}-${d.evaluated_at}`} className="border-t border-border">
                      <td className="px-2 py-1 font-mono">{hhmm(d.candle_time)}</td>
                      <td className="px-2 py-1">{d.underlying}</td>
                      <td className="px-2 py-1">{d.trend} · {d.strength}</td>
                      <td className="px-2 py-1"><Badge intent={decisionIntent(d.decision)}>{d.decision}</Badge></td>
                      <td className="px-2 py-1 text-text-2">{d.reason_code ? reasonText(d.reason_code) : d.entry_trigger}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </DataSurface>
    </Panel>
  );
}
