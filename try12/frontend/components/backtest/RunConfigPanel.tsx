"use client";
import toast from "react-hot-toast";
import { Panel } from "@/components/ui/Panel";
import { HashCell, copyText } from "./HashCell";
import { SourceMarker } from "./ChartWithTable";
import { fmtInr } from "./format";
import type { RunDetail } from "@/types/backtest";

// The run's frozen configuration (design §5.4, §6.2), read from its own record — never from the
// live runtime context: capital, sizing, caps, hold limits, seed, OOS split, label, notes,
// code_version, intra_candle_order, the hashes, and the full params JSON behind a <details>.

type Rec = Record<string, unknown>;
const asRec = (v: unknown): Rec => (v && typeof v === "object" && !Array.isArray(v) ? (v as Rec) : {});

function show(v: unknown): string {
  if (v === null || v === undefined || v === "") return "—";
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (typeof v === "number") return Number.isFinite(v) ? v.toLocaleString("en-IN") : "—";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

const withUnit = (v: unknown, unit: string) => (show(v) === "—" ? "—" : `${show(v)} ${unit}`);

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-3 border-b border-border/50 py-1 last:border-0">
      <dt className="text-table text-text-3">{label}</dt>
      <dd className="text-right font-mono text-table text-text-1">{children}</dd>
    </div>
  );
}

export function RunConfigPanel({ run }: { run: RunDetail }) {
  const rec = asRec(run.params);
  const cfg = asRec(rec.cfg);
  const params = asRec(rec.params);
  const s1 = asRec(run.validation?.sections?.["1"]);
  /** A knob as the run used it: the run controls (cfg, authoritative since R1) first, then the
   *  frozen TradingParams. */
  const knob = (k: string) => (cfg[k] !== undefined ? cfg[k] : params[k]);
  const hold = asRec(knob("max_hold_minutes"));
  const json = run.params ? JSON.stringify(run.params, null, 2) : null;

  const onCopy = async () => {
    if (!json) return;
    if (await copyText(json)) toast.success("Params JSON copied");
    else toast.error("Copy failed — select the text manually");
  };

  return (
    <Panel title="Run configuration" meta={<SourceMarker source={run.dataset === "artifacts" ? "artifact" : null} />}>
      <div className="grid grid-cols-1 gap-x-6 md:grid-cols-2 xl:grid-cols-3">
        <dl>
          <Row label="Capital">{run.capital ? fmtInr(run.capital) : "unknown"}</Row>
          <Row label="Fixed fraction">{withUnit(knob("fixed_fraction_pct"), "%")}</Row>
          <Row label="Kelly cap">{withUnit(knob("kelly_fraction_cap_pct"), "%")}</Row>
          <Row label="Max lots / trade">{show(knob("max_lots_per_trade"))}</Row>
          <Row label="Partial exit">{show(knob("partial_exit_enabled"))}</Row>
          <Row label="Open exposure cap">{withUnit(knob("open_exposure_cap_pct"), "%")}</Row>
        </dl>
        <dl>
          <Row label="Max trades / day">{show(knob("max_trades_per_day"))}</Row>
          <Row label="Max open positions">{show(knob("max_open_positions"))}</Row>
          <Row label="Max daily loss">{typeof knob("max_daily_loss") === "number" ? fmtInr(knob("max_daily_loss") as number) : show(knob("max_daily_loss"))}</Row>
          <Row label="Max daily loss %">{withUnit(knob("max_daily_loss_pct"), "%")}</Row>
          <Row label="Transition cooldown">{withUnit(knob("transition_cooldown_candles"), "candles")}</Row>
          <Row label="Hold limits">
            {Object.keys(hold).length === 0 ? "—" : Object.entries(hold).map(([k, v]) => `${k} ${show(v)}m`).join(" · ")}
          </Row>
        </dl>
        <dl>
          <Row label="Seed">{show(run.seed)}</Row>
          <Row label="OOS split">{typeof run.oos_split === "number" ? run.oos_split.toFixed(2) : "—"}</Row>
          <Row label="Bootstrap iterations">{show(s1.bootstrap_iterations)}</Row>
          <Row label="Intra-candle order">{show(rec.intra_candle_order ?? s1.intra_candle_order)}</Row>
          <Row label="Code version">{show(run.code_version)}</Row>
          <Row label="Params hash"><HashCell value={run.params_hash} label="params hash" /></Row>
          <Row label="Determinism hash"><HashCell value={run.determinism_hash} label="determinism hash" /></Row>
        </dl>
      </div>
      <dl className="mt-3 space-y-1">
        <Row label="Label">{run.label || "—"}</Row>
        <div className="py-1">
          <dt className="text-table text-text-3">Notes</dt>
          <dd className="mt-0.5 whitespace-pre-wrap text-table text-text-1">{run.notes || "—"}</dd>
        </div>
      </dl>
      {run.warnings.includes("params_defaults_used") && (
        <p role="note" className="mt-2 text-micro text-yellow">
          This record was written by an older code version; some knobs could not be rebuilt and the
          derived figures use defaults.
        </p>
      )}
      {json && (
        <details className="mt-3">
          <summary className="cursor-pointer text-table font-semibold text-text-2 hover:text-text-1">Full frozen params (JSON)</summary>
          <div className="mt-2 space-y-2">
            <button type="button" onClick={() => { void onCopy(); }}
              className="rounded border border-border-2 px-2 py-0.5 text-micro text-text-2 hover:bg-surface-3 hover:text-text-1">
              Copy JSON
            </button>
            <pre className="max-h-[360px] overflow-auto rounded-md border border-border bg-surface-2 p-3 font-mono text-micro text-text-2">{json}</pre>
          </div>
        </details>
      )}
    </Panel>
  );
}
