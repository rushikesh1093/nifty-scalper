"use client";
import { useId, useState } from "react";
import { useDiagnostics } from "@/hooks/useDiagnostics";
import { useNow } from "@/hooks/useNow";
import { cn, formatPrice } from "@/lib/utils";
import { formatIst, relativeAge } from "@/lib/time";
import { Panel } from "@/components/ui/Panel";
import { DataSurface } from "@/components/ui/DataSurface";
import { Skeleton } from "@/components/ui/Skeleton";
import { Badge } from "@/components/ui/Badge";
import type { Blocker, SignalDiagnostics } from "@/types/diagnostics";

// Read-only explanation of why signals are (not) firing / converting (design §6.10). It never
// changes a setting: remedies only switch the sidebar tab where the user can act.

export type DeskSidebarTab = "capital" | "strategies" | "selector";

const REMEDY: Record<string, { tab: DeskSidebarTab; label: string }> = {
  CAPITAL_BELOW_LOT_COST:  { tab: "capital",    label: "Open Capital & sizing" },
  ALL_STRATEGIES_DISABLED: { tab: "strategies", label: "Open Strategies" },
};

/** Trading-policy gates: shown as information, not as faults to fix. */
const POLICY_CODES = new Set([
  "MARKET_CLOSED", "STUDY_PHASE", "VIX_OUT_OF_RANGE", "REGIME_BLOCKS_ALL", "REGIME_TRANSITION_COOLDOWN",
  "DAILY_TRADE_CAP_REACHED", "MAX_OPEN_POSITIONS_REACHED", "CIRCUIT_BREAKER", "ATR_BELOW_MIN",
]);

type Severity = "ok" | "warning" | "blocking";

const DOT: Record<Severity, string> = { ok: "bg-green", warning: "bg-yellow", blocking: "bg-red" };
const SEVERITY_TEXT: Record<Severity, string> = { ok: "text-green", warning: "text-yellow", blocking: "text-red" };

const yesNo = (v: boolean | null | undefined, yes = "yes", no = "no") => (v == null ? "unknown" : v ? yes : no);
const n = (v: number | null | undefined, decimals = 0) => (v == null ? "—" : formatPrice(v, decimals));

function verdict(d: SignalDiagnostics): { severity: Severity; text: string } {
  const blockers = Array.isArray(d.blockers) ? d.blockers : [];
  const blocking = blockers.filter((b) => b.severity === "blocking").length;
  const warnings = blockers.length - blocking;
  const today = d.signals?.today_count;
  const converted = d.positions?.today_total;
  const firing =
    today == null
      ? "Signal count unknown"
      : today > 0
        ? `Signals firing (${today} today), ${converted ?? "unknown"} converting to trades`
        : "No signals today";
  if (blockers.length === 0) return { severity: "ok", text: `${firing} — no blockers` };
  const parts = [
    blocking ? `${blocking} blocking` : null,
    warnings ? `${warnings} warning${warnings === 1 ? "" : "s"}` : null,
  ].filter(Boolean);
  return { severity: blocking ? "blocking" : "warning", text: `${firing} — ${parts.join(", ")}` };
}

function BlockerItem({ b, onNavigate }: { b: Blocker; onNavigate?: (tab: DeskSidebarTab) => void }) {
  const remedy = REMEDY[b.code];
  const policy = POLICY_CODES.has(b.code);
  return (
    <li className="rounded-md border border-border bg-surface-2 px-3 py-2" data-blocker-code={b.code}>
      <div className="flex flex-wrap items-center gap-2">
        <Badge intent={b.severity === "blocking" ? "negative" : "warning"} status={b.severity}>
          {b.severity}
        </Badge>
        <span className="font-mono text-table font-semibold text-text-1">{b.code}</span>
        {b.underlying && <Badge intent="neutral">{b.underlying}</Badge>}
        {policy && <span className="text-micro text-text-2">trading policy — informational</span>}
        {remedy && onNavigate && (
          <button
            type="button"
            onClick={() => onNavigate(remedy.tab)}
            className="ml-auto rounded border border-accent/40 bg-accent/15 px-2 py-px text-micro font-semibold text-text-1 hover:bg-accent/25"
          >
            {remedy.label}
          </button>
        )}
      </div>
      <p className="mt-1 text-table text-text-1">{b.message}</p>
      {b.detail && <p className="mt-0.5 break-words font-mono text-table text-text-2">{b.detail}</p>}
    </li>
  );
}

function Row({ label, value, state }: { label: string; value: React.ReactNode; state?: Severity }) {
  return (
    <div className="flex items-baseline justify-between gap-3 border-b border-border/60 py-1">
      <dt className="text-text-2">{label}</dt>
      <dd className={cn("text-right font-mono tabular-nums", state ? SEVERITY_TEXT[state] : "text-text-1")}>{value}</dd>
    </div>
  );
}

function Checklist({ d, now }: { d: SignalDiagnostics; now: Date | null }) {
  const cap = d.capital ?? ({} as SignalDiagnostics["capital"]);
  const underlyings = Array.isArray(d.underlyings) ? d.underlyings : [];
  return (
    <div className="grid gap-x-6 gap-y-3 text-table md:grid-cols-2">
      <dl>
        <Row
          label={`Signal window ${d.session_window}`}
          value={d.session_window_open ? "open" : "closed"}
          state={d.session_window_open ? "ok" : "warning"}
        />
        <Row label="Strategy runner" value={yesNo(d.runner_running, "running", "stopped")} state={d.runner_running === false ? "blocking" : d.runner_running ? "ok" : undefined} />
        <Row label="Mode" value={d.mode ?? "unknown"} />
        <Row label="Study phase" value={yesNo(d.study_phase, "on", "off")} />
        <Row
          label="Feed"
          value={`${yesNo(d.feed?.connected, "connected", "down")} · ${d.feed?.subscriptions ?? "—"} subs · session ${yesNo(d.feed?.session_valid, "valid", "invalid")}`}
        />
        <Row
          label="VIX gate"
          value={`${n(d.vix?.ltp, 2)} (${n(d.vix?.min, 1)}–${n(d.vix?.max, 1)}) ${d.vix?.gate ?? ""}`}
          state={d.vix?.gate === "BLOCKED" ? "blocking" : d.vix?.gate === "PASS" ? "ok" : undefined}
        />
        <Row
          label="Signals today"
          value={`${d.signals?.today_count ?? "—"}${d.signals?.last_signal_at && now ? ` · last ${relativeAge(d.signals.last_signal_at, now)}` : ""}`}
        />
        <Row label="Positions open / today" value={`${d.positions?.open ?? "—"} / ${d.positions?.today_total ?? "—"}`} />
      </dl>
      <dl>
        <Row label="Capital available / per trade" value={`₹${n(cap.available)} / ₹${n(cap.trade_amount)}`} />
        <Row label="Trades today" value={`${cap.daily_trades ?? "—"} / ${cap.max_trades_per_day ?? "—"}`} />
        <Row label="Open positions" value={`${cap.open_positions ?? "—"} / ${cap.max_open_positions ?? "—"}`} />
        <Row
          label="Circuit breaker"
          value={cap.circuit_breaker == null ? "unknown" : cap.circuit_breaker ? `tripped${cap.circuit_reason ? ` — ${cap.circuit_reason}` : ""}` : "clear"}
          state={cap.circuit_breaker ? "blocking" : cap.circuit_breaker === false ? "ok" : undefined}
        />
        <Row
          label="Strategies enabled"
          value={`${d.strategies?.enabled ?? "—"} / ${d.strategies?.total ?? "—"}`}
          state={d.strategies?.enabled === 0 ? "blocking" : undefined}
        />
        {underlyings.map((u) => (
          <Row
            key={u.underlying}
            label={`${u.underlying} · candles ${u.candles_today ?? "—"}/${u.candles_required} · ATR ${n(u.atr, 1)}/${n(u.atr_min, 1)}`}
            value={u.regime == null ? "regime not detected yet"
              : `${u.regime} · ${u.regime_gating === false && u.regime_allows_trading
                  ? "shadow (does not gate trades)"
                  : yesNo(u.regime_allows_trading, "allows trading", "blocks trading")}`}
            state={u.regime_allows_trading === false ? "warning" : u.regime_allows_trading ? "ok" : undefined}
          />
        ))}
      </dl>
      {d.strategies?.disabled?.length ? (
        <p className="text-text-2 md:col-span-2">Disabled: <span className="font-mono">{d.strategies.disabled.join(", ")}</span></p>
      ) : null}
      <p className="text-micro normal-case tracking-normal text-text-2 md:col-span-2">
        {d.session_window_note} · as of {formatIst(d.as_of)} IST
      </p>
    </div>
  );
}

export function SignalDiagnosticsPanel({ onNavigate }: { onNavigate?: (tab: DeskSidebarTab) => void }) {
  const query = useDiagnostics();
  const now = useNow(15_000);
  const bodyId = useId();
  // null → follow the data (expanded while anything is blocking); a click pins the user's choice.
  const [expandedPref, setExpandedPref] = useState<boolean | null>(null);

  return (
    <Panel title="Signal pipeline diagnostics" bodyClassName="space-y-3">
      <DataSurface query={query} skeleton={<Skeleton label="Loading diagnostics" />}>
        {(d) => {
          const v = verdict(d);
          const blockers = Array.isArray(d.blockers) ? d.blockers : [];
          const expanded = expandedPref ?? v.severity === "blocking";
          return (
            <div className="space-y-3">
              <div className="flex items-start gap-2">
                <span aria-hidden="true" className={cn("mt-1.5 h-2 w-2 shrink-0 rounded-full", DOT[v.severity])} />
                <p className="flex-1 text-body text-text-1" data-role="diagnostics-verdict">
                  <span className={cn("font-semibold", SEVERITY_TEXT[v.severity])}>
                    {v.severity === "ok" ? "Healthy" : v.severity === "blocking" ? "Blocked" : "Degraded"}:
                  </span>{" "}
                  {v.text}
                </p>
                <button
                  type="button"
                  onClick={() => setExpandedPref(!expanded)}
                  aria-expanded={expanded}
                  aria-controls={bodyId}
                  className="shrink-0 rounded border border-border-2 px-2 py-0.5 text-micro font-semibold text-text-2 hover:bg-surface-3 hover:text-text-1"
                >
                  {expanded ? "Hide details" : "Show details"}
                </button>
              </div>

              <div id={bodyId} hidden={!expanded} className="space-y-3">
                {blockers.length === 0 ? (
                  <p className="rounded-md border border-green/30 bg-green/10 px-3 py-2 text-table text-green">
                    No blockers — every gate the pipeline checks is currently passing.
                  </p>
                ) : (
                  <ul className="space-y-2" aria-label="Blockers">
                    {blockers.map((b) => <BlockerItem key={`${b.code}|${b.underlying ?? ""}|${b.message}`} b={b} onNavigate={onNavigate} />)}
                  </ul>
                )}
                <Checklist d={d} now={now} />
              </div>
            </div>
          );
        }}
      </DataSurface>
    </Panel>
  );
}
