"use client";
import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { selectOption } from "@/lib/tradingApi";
import { useMarketField, useMarketStore, selectTick } from "@/lib/store";
import { cn, formatNumber, formatPrice } from "@/lib/utils";
import { Badge } from "@/components/ui/Badge";
import { ErrorState } from "@/components/ui/ErrorState";
import type { OptionCandidate } from "@/types/trading";

// Research-only preview of the option the selector would pick for a hypothetical signal.
// It never places a trade. Spot comes from the live tick store (field selectors, not the whole store).

const SPOT_TOKEN: Record<string, string> = { NIFTY: "26000", BANKNIFTY: "26009" };
const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
const fixed = (v: unknown, d: number, suffix = "") => { const x = num(v); return x === null ? "—" : `${x.toFixed(d)}${suffix}`; };

function Candidate({ o, primary, rank }: { o: OptionCandidate; primary: boolean; rank: number }) {
  const score = num(o.score) ?? 0;
  const spread = num(o.spread_pct) ?? 0;
  const delta = num(o.delta);
  const checks = [
    { label: "Spread ≤ 2%", ok: spread <= 2 },
    { label: "OI ≥ 1L", ok: (num(o.oi) ?? 0) >= 100_000 },
    { label: "Vol ≥ 5k", ok: (num(o.volume) ?? 0) >= 5_000 },
    { label: "LTP 15–600", ok: (num(o.ltp) ?? 0) >= 15 && (num(o.ltp) ?? 0) <= 600 },
  ];
  return (
    <li className={cn("space-y-2 rounded-lg border p-3 text-table", primary ? "border-yellow/40 bg-yellow/5" : "border-border bg-surface-2")}>
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          {primary ? <Badge intent="warning">auto-select</Badge> : <span className="text-micro text-text-2">Alt {rank}</span>}
          <span className="font-mono font-bold text-text-1">{o.strike} {o.option_type}</span>
        </div>
        <div className="text-right">
          <div className={cn("font-mono text-body font-bold", score >= 80 ? "text-green" : score >= 60 ? "text-yellow" : "text-red")}>
            {score.toFixed(0)}/100
          </div>
          <div className="text-micro text-text-2">score</div>
        </div>
      </div>
      <dl className="grid grid-cols-3 gap-x-3 gap-y-1">
        {[
          { label: "LTP", val: formatPrice(num(o.ltp)), cls: "text-text-1" },
          { label: "Delta", val: fixed(delta, 3), cls: delta !== null && delta >= 0.35 && delta <= 0.5 ? "text-green" : "text-yellow" },
          { label: "IV", val: fixed(o.iv, 1, "%"), cls: "text-blue" },
          { label: "OI", val: formatNumber(num(o.oi)), cls: "text-text-1" },
          { label: "Vol", val: formatNumber(num(o.volume)), cls: "text-text-1" },
          { label: "Spread", val: fixed(o.spread_pct, 1, "%"), cls: spread > 2 ? "text-red" : "text-green" },
        ].map(({ label, val, cls }) => (
          <div key={label} className="flex gap-1">
            <dt className="text-text-2">{label}</dt>
            <dd className={cn("font-mono tabular-nums", cls)}>{val}</dd>
          </div>
        ))}
      </dl>
      {primary && (
        <ul className="flex flex-wrap gap-1 border-t border-border pt-2" aria-label="Liquidity checks">
          {checks.map((c) => (
            <li key={c.label}>
              <Badge intent={c.ok ? "positive" : "negative"} strength="outline" caps={false}>
                <span aria-hidden="true">{c.ok ? "✓" : "✗"}</span>
                <span className="sr-only">{c.ok ? "pass:" : "fail:"}</span> {c.label}
              </Badge>
            </li>
          ))}
        </ul>
      )}
    </li>
  );
}

export function OptionSelectorWidget() {
  const underlying = useMarketField("selectedUnderlying");
  const setUnderlying = useMarketField("setUnderlying");
  const tick = useMarketStore(selectTick(SPOT_TOKEN[underlying]));
  const spotLtp = num(tick?.ltp);
  const [direction, setDirection] = useState<"BUY" | "SELL">("BUY");

  const preview = useMutation({
    mutationFn: () => selectOption({ direction, underlying, spot_price: spotLtp ?? 0 }),
  });
  const optionType = direction === "BUY" ? "CE" : "PE";
  const results = Array.isArray(preview.data) ? preview.data : [];

  return (
    <div className="space-y-3">
      <div className="flex items-start justify-between gap-2">
        <p className="text-table text-text-2">
          Strategies pick the option automatically when a signal fires. This previews that scoring; no trade is placed.
        </p>
        <Badge intent="neutral">research only</Badge>
      </div>

      <div role="group" aria-label="Underlying" className="grid grid-cols-2 gap-2">
        {(["NIFTY", "BANKNIFTY"] as const).map((u) => (
          <button
            key={u}
            type="button"
            aria-pressed={underlying === u}
            onClick={() => { setUnderlying(u); preview.reset(); }}
            className={cn(
              "rounded-md border py-1 text-table font-semibold",
              underlying === u ? "border-accent/60 bg-accent/30 text-text-1" : "border-border bg-surface-2 text-text-2 hover:text-text-1",
            )}
          >
            {u}
          </button>
        ))}
      </div>

      <div role="group" aria-label="Simulated signal direction" className="grid grid-cols-2 gap-2">
        {(["BUY", "SELL"] as const).map((d) => (
          <button
            key={d}
            type="button"
            aria-pressed={direction === d}
            onClick={() => { setDirection(d); preview.reset(); }}
            className={cn(
              "rounded-lg border py-2 text-table font-bold",
              direction === d
                ? d === "BUY" ? "border-green/50 bg-green/10 text-green" : "border-red/50 bg-red/10 text-red"
                : "border-border bg-transparent text-text-2 hover:text-text-1",
            )}
          >
            <span aria-hidden="true">{d === "BUY" ? "▲ " : "▼ "}</span>{underlying} {d === "BUY" ? "up" : "down"}
            <span className="block text-micro font-normal normal-case tracking-normal">best {d === "BUY" ? "CE" : "PE"}</span>
          </button>
        ))}
      </div>

      <div className="flex items-center justify-between text-table">
        <span className="text-text-2">{underlying} spot</span>
        <span className={cn("font-mono font-bold tabular-nums", spotLtp ? "text-text-1" : "text-text-2")}>
          {spotLtp ? formatPrice(spotLtp) : "no live tick"}
        </span>
      </div>

      <button
        type="button"
        onClick={() => preview.mutate()}
        disabled={!spotLtp || preview.isPending}
        className="w-full rounded-lg border border-accent/60 bg-accent/30 py-2 text-table font-semibold text-text-1 hover:bg-accent/40 disabled:cursor-not-allowed disabled:opacity-50"
      >
        {preview.isPending ? "Scoring candidates…" : `Preview best ${optionType} for ${underlying}`}
      </button>
      {!spotLtp && <p className="text-micro normal-case tracking-normal text-text-2">Needs a live spot tick from the feed.</p>}

      {preview.isError && <ErrorState error={preview.error} onRetry={() => preview.mutate()} title="Preview failed" />}

      {preview.isSuccess && results.length === 0 && (
        <p className="text-table text-text-2">The selector returned no candidates.</p>
      )}
      {results.length > 0 && (
        <ul className="space-y-2" aria-label={`${underlying} ${direction === "BUY" ? "up" : "down"} signal would select`}>
          {results.map((o, i) => <Candidate key={o.token} o={o} primary={i === 0} rank={i + 1} />)}
        </ul>
      )}
    </div>
  );
}
