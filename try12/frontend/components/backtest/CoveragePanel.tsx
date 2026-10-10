import { Badge } from "@/components/ui/Badge";
import { EmptyState } from "@/components/ui/EmptyState";
import { Panel } from "@/components/ui/Panel";
import { SourceMarker } from "./ChartWithTable";
import { fmtInt } from "./format";
import type { CoverageDay, RunDetail } from "@/types/backtest";

// Data coverage (design §5.4, §6.2). The summary is per underlying (29 session days on A4 is a
// per-underlying number, not the run's), with the distinct-day totals above it, then one row per
// (day, underlying) with its skip cause.

const cell = (v: unknown): string => {
  if (v === null || v === undefined || v === "") return "—";
  if (typeof v === "number") return Number.isFinite(v) ? v.toLocaleString("en-IN") : "—";
  return String(v);
};

/** Skip reasons summed over underlyings — used by the zero-tradeable-day banner. */
export function skipReasonTotals(run: RunDetail): [string, number][] {
  const out = new Map<string, number>();
  for (const u of Object.values(run.coverage?.summary ?? {})) {
    for (const [code, n] of Object.entries(u?.skipped ?? {})) out.set(code, (out.get(code) ?? 0) + (n || 0));
  }
  return Array.from(out).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
}

function DayRow({ d }: { d: CoverageDay }) {
  const tradeable = d.status === "TRADEABLE";
  return (
    <tr>
      <td className="whitespace-nowrap font-mono">{d.day}</td>
      <td>{d.underlying}</td>
      <td>
        <Badge intent={tradeable ? "positive" : "warning"} status={d.status}>{d.status}</Badge>
      </td>
      <td className="font-mono text-text-2">{cell(d.cause)}</td>
      <td className="num font-mono">{cell(d.spot_candles)}</td>
      <td className="num font-mono">{cell(d.option_tokens_with_ticks)}</td>
      <td className="num font-mono">{cell(d.option_tick_minutes)}</td>
      <td className="num font-mono">{d.snapshot_cadence_s == null ? "—" : `${cell(d.snapshot_cadence_s)} s`}</td>
      <td className="num font-mono">{cell(d.nearest_dte)}</td>
      <td className="whitespace-nowrap font-mono">{cell(d.chosen_expiry)}</td>
      <td className="num font-mono">{cell(d.unattributed_tokens)}</td>
    </tr>
  );
}

export function CoveragePanel({ run }: { run: RunDetail }) {
  const cov = run.coverage;
  const summary = Object.entries(cov?.summary ?? {});
  const days = cov?.days ?? [];
  const distinctDays = new Set(days.map((d) => d.day)).size;
  return (
    <Panel title="Data coverage" meta={<SourceMarker source={run.dataset === "artifacts" ? "artifact" : null} />}>
      {!cov || (summary.length === 0 && days.length === 0) ? (
        <EmptyState title="No coverage report stored for this run" />
      ) : (
        <div className="space-y-3">
          <p className="text-table text-text-2">
            <span className="font-mono text-text-1">{fmtInt(distinctDays)}</span> distinct session days ·{" "}
            <span className="font-mono text-text-1">{fmtInt(run.sessions)}</span> distinct TRADEABLE days ·{" "}
            <span className="font-mono text-text-1">{fmtInt(days.length)}</span> day rows across{" "}
            {summary.length} underlying{summary.length === 1 ? "" : "s"}
          </p>
          {summary.length > 0 && (
            <div className="overflow-x-auto">
              <table className="data-table">
                <caption className="sr-only">Coverage per underlying</caption>
                <thead>
                  <tr>
                    <th scope="col">Underlying</th>
                    <th scope="col" className="num">Session days</th>
                    <th scope="col" className="num">TRADEABLE</th>
                    <th scope="col">Skip reasons</th>
                    <th scope="col" className="num">Mean option ticks / tradeable day</th>
                  </tr>
                </thead>
                <tbody>
                  {summary.map(([u, s]) => (
                    <tr key={u}>
                      <td className="font-mono text-text-1">{u}</td>
                      <td className="num font-mono">{fmtInt(s.session_days)}</td>
                      <td className="num font-mono">{fmtInt(s.tradeable)}</td>
                      <td>
                        {Object.keys(s.skipped ?? {}).length === 0 ? <span className="text-text-3">none</span> : (
                          <span className="flex flex-wrap gap-1">
                            {Object.entries(s.skipped).sort((a, b) => b[1] - a[1]).map(([code, n]) => (
                              <Badge key={code} intent="warning" strength="outline" caps={false} status={code}>
                                {code} {n}
                              </Badge>
                            ))}
                          </span>
                        )}
                      </td>
                      <td className="num font-mono">
                        {typeof s.mean_option_ticks_per_tradeable_day === "number"
                          ? s.mean_option_ticks_per_tradeable_day.toFixed(0) : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {days.length > 0 && (
            <details open>
              <summary className="cursor-pointer text-table font-semibold text-text-2 hover:text-text-1">
                Per-day coverage ({days.length} rows)
              </summary>
              <div className="mt-2 max-h-[420px] overflow-auto">
                <table className="data-table">
                  <caption className="sr-only">Coverage per day and underlying</caption>
                  <thead>
                    <tr>
                      <th scope="col">Day</th>
                      <th scope="col">Underlying</th>
                      <th scope="col">Status</th>
                      <th scope="col">Cause</th>
                      <th scope="col" className="num">Candles</th>
                      <th scope="col" className="num">Option tokens</th>
                      <th scope="col" className="num">Tick minutes</th>
                      <th scope="col" className="num">Snapshot cadence</th>
                      <th scope="col" className="num">Nearest DTE</th>
                      <th scope="col">Expiry</th>
                      <th scope="col" className="num">Unattributed tokens</th>
                    </tr>
                  </thead>
                  <tbody>
                    {days.map((d) => <DayRow key={`${d.day}:${d.underlying}`} d={d} />)}
                  </tbody>
                </table>
              </div>
            </details>
          )}
        </div>
      )}
    </Panel>
  );
}
