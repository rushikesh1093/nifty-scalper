"use client";
import { useId, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import toast from "react-hot-toast";
import { getCapital, setTradeAmount, setCapital } from "@/lib/tradingApi";
import { ApiError, errorMessage } from "@/lib/http";
import { queryKeys } from "@/lib/queryKeys";
import { cn } from "@/lib/utils";
import { DataSurface } from "@/components/ui/DataSurface";
import { Skeleton } from "@/components/ui/Skeleton";
import { DeltaText } from "@/components/ui/PriceText";
import type { CapitalSummary, TradePreviewRow } from "@/types/trading";

// Capital + per-trade sizing. Absorbs the retired TradeAmountSetter (presets, custom amount,
// capital update, lot preview) with validated, labelled inputs (design §8.2).

const PRESETS = [5000, 10000, 15000, 20000, 25000, 50000, 75000];
const MAX_CAPITAL = 10_000_000;

const rupees = (n: number | null | undefined) =>
  n == null || !Number.isFinite(n) ? "—" : "₹" + n.toLocaleString("en-IN", { maximumFractionDigits: 0 });
const failText = (e: unknown) => (e instanceof ApiError && e.detail ? e.detail : errorMessage(e));

function validateCapital(raw: string): string | null {
  if (raw.trim() === "") return "Enter the total capital";
  const v = Number(raw);
  if (!Number.isFinite(v)) return "Must be a number";
  if (v <= 0) return "Must be greater than 0";
  if (v > MAX_CAPITAL) return "Cannot exceed ₹1,00,00,000";
  if (!Number.isInteger(v)) return "Whole rupees only";
  return null;
}

function validateTradeAmount(raw: string, capital: number): string | null {
  if (raw.trim() === "") return "Enter a per-trade amount";
  const v = Number(raw);
  if (!Number.isFinite(v)) return "Must be a number";
  if (v <= 0) return "Must be greater than 0";
  if (v > capital) return "Cannot exceed total capital";
  return null;
}

function Meter({ label, pct, valueText, tone }: { label: string; pct: number; valueText: string; tone: "ok" | "warn" | "bad" | "accent" }) {
  const clamped = Math.min(Math.max(Number.isFinite(pct) ? pct : 0, 0), 100);
  const id = useId();
  return (
    <div className="space-y-1.5">
      <div className="flex justify-between text-table text-text-2">
        <span id={id}>{label}</span>
        <span className="font-mono tabular-nums">{valueText}</span>
      </div>
      <div
        role="progressbar"
        aria-labelledby={id}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(clamped)}
        aria-valuetext={valueText}
        className="h-1.5 w-full overflow-hidden rounded-full bg-surface-3"
      >
        <div
          className={cn(
            "h-full rounded-full transition-[width] duration-700",
            tone === "ok" && "bg-green", tone === "warn" && "bg-yellow", tone === "bad" && "bg-red", tone === "accent" && "bg-accent",
          )}
          style={{ width: `${clamped}%` }}
        />
      </div>
    </div>
  );
}

function AmountForm({
  label, placeholder, submitLabel, hint, validate, pending, onSubmit, onCancel, autoFocus,
}: {
  label: string; placeholder: string; submitLabel: string; hint?: string;
  validate: (raw: string) => string | null; pending: boolean;
  onSubmit: (v: number) => void; onCancel?: () => void; autoFocus?: boolean;
}) {
  const [raw, setRaw] = useState("");
  const [touched, setTouched] = useState(false);
  const id = useId();
  const error = validate(raw);
  const showError = touched && error !== null;
  const describedBy = [hint ? `${id}-hint` : null, showError ? `${id}-err` : null].filter(Boolean).join(" ") || undefined;
  return (
    <form
      noValidate
      onSubmit={(e) => {
        e.preventDefault();
        setTouched(true);
        if (error === null) onSubmit(Number(raw));
      }}
      className="space-y-1"
    >
      <label htmlFor={id} className="block text-table text-text-2">{label}</label>
      <div className="flex gap-2">
        <input
          id={id}
          type="number"
          inputMode="numeric"
          min={1}
          step={1}
          value={raw}
          placeholder={placeholder}
          autoFocus={autoFocus}
          onChange={(e) => { setRaw(e.target.value); setTouched(true); }}
          aria-invalid={showError}
          aria-describedby={describedBy}
          className={cn(
            "min-w-0 flex-1 rounded-md border bg-surface-2 px-3 py-1.5 font-mono text-body text-text-1 placeholder:text-text-3",
            showError ? "border-red" : "border-border-2 focus:border-accent",
          )}
        />
        <button
          type="submit"
          disabled={pending || error !== null}
          className="rounded-md border border-accent/60 bg-accent/30 px-3 py-1.5 text-table font-semibold text-text-1 hover:bg-accent/40 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {pending ? "Saving…" : submitLabel}
        </button>
        {onCancel && (
          <button
            type="button"
            onClick={onCancel}
            className="rounded-md border border-border-2 bg-surface-3 px-3 py-1.5 text-table text-text-1 hover:bg-surface-2"
          >
            Cancel
          </button>
        )}
      </div>
      {showError && <p id={`${id}-err`} className="text-table text-red">{error}</p>}
      {hint && <p id={`${id}-hint`} className="text-micro normal-case tracking-normal text-text-2">{hint}</p>}
    </form>
  );
}

function PreviewTable({ rows, onHide }: { rows: TradePreviewRow[]; onHide: () => void }) {
  return (
    <div className="space-y-1.5 border-t border-border pt-3">
      <div className="flex items-center justify-between">
        <span className="text-micro font-semibold uppercase text-text-2">Lot preview at the new per-trade amount</span>
        <button type="button" onClick={onHide} className="text-micro text-text-2 underline-offset-2 hover:text-text-1 hover:underline">
          hide
        </button>
      </div>
      <div className="max-h-48 overflow-auto rounded-md border border-border">
        <table className="data-table">
          <caption className="sr-only">Lots bought per option price and lot size</caption>
          <thead>
            <tr>
              <th scope="col" className="num">Option LTP</th>
              <th scope="col" className="num">Lot size</th>
              <th scope="col" className="num">Lots</th>
              <th scope="col" className="num">Cost / lot</th>
              <th scope="col" className="num">Deployed</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={`${r.ltp}-${r.lot_size}`}>
                <td className="num font-mono text-text-1">₹{r.ltp}</td>
                <td className="num font-mono">{r.lot_size}</td>
                <td className="num font-mono font-semibold text-text-1">{r.lots}</td>
                <td className="num font-mono">{rupees(r.capital_needed)}</td>
                <td className="num font-mono text-yellow">{rupees(r.capital_used)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="text-micro normal-case tracking-normal text-text-2">
        Lots = floor(per-trade amount ÷ (LTP × lot size)), minimum 1, capped at the max lots per trade.
      </p>
    </div>
  );
}

function CapitalBody({ cap }: { cap: CapitalSummary }) {
  const qc = useQueryClient();
  const [editingCapital, setEditingCapital] = useState(false);
  const [customTrade, setCustomTrade] = useState(false);
  const [preview, setPreview] = useState<TradePreviewRow[] | null>(null);

  const invalidate = () => {
    for (const key of [queryKeys.capital, queryKeys.config, queryKeys.stats, queryKeys.diagnostics]) {
      void qc.invalidateQueries({ queryKey: key });
    }
  };

  const tradeMut = useMutation({
    mutationFn: (ta: number) => setTradeAmount(ta),
    onSuccess: (d) => {
      invalidate();
      setPreview(Array.isArray(d.preview) ? d.preview : null);
      setCustomTrade(false);
      toast.success(`Per-trade amount set to ${rupees(d.trade_amount)}`);
    },
    onError: (e: unknown) => { console.error("[capital] trade amount failed", e); toast.error(`Update failed: ${failText(e)}`); },
  });

  const capMut = useMutation({
    mutationFn: (c: number) => setCapital(c),
    onSuccess: (d: { capital?: number }) => {
      invalidate();
      setEditingCapital(false);
      toast.success(`Capital set to ${rupees(d.capital)}`);
    },
    onError: (e: unknown) => { console.error("[capital] capital failed", e); toast.error(`Update failed: ${failText(e)}`); },
  });

  const usedPct = Number.isFinite(cap.utilisation_pct) ? cap.utilisation_pct : 0;
  const lossPct = cap.capital > 0 ? (Math.abs(Math.min(cap.realised_pnl, 0)) / cap.capital) * 100 : 0;
  const lossOfLimit = cap.max_daily_loss_pct > 0 ? (lossPct / cap.max_daily_loss_pct) * 100 : 0;
  const lowFunds = cap.available_capital < cap.trade_amount;

  return (
    <div className="space-y-4">
      {cap.circuit_breaker && (
        <div role="alert" className="rounded-md border border-red/40 bg-red/10 px-3 py-2 text-table font-semibold text-red">
          Circuit breaker tripped{cap.circuit_reason ? ` — ${cap.circuit_reason}` : ""}
        </div>
      )}

      <div className="flex items-center justify-between text-table text-text-2">
        <span>Realised today ({cap.mode})</span>
        <span aria-live="polite"><DeltaText value={cap.realised_pnl} prefix="₹" decimals={0} /></span>
      </div>

      <div className="grid grid-cols-3 gap-2">
        <div className="rounded-lg border border-border bg-surface-2 px-2 py-2 text-center">
          <div className="text-micro font-semibold uppercase text-text-2">Total</div>
          <div className="font-mono text-body font-bold text-text-1">{rupees(cap.capital)}</div>
          <button
            type="button"
            onClick={() => setEditingCapital((v) => !v)}
            aria-expanded={editingCapital}
            className="mt-0.5 text-micro font-semibold text-blue hover:underline"
          >
            {editingCapital ? "cancel" : "edit"}
          </button>
        </div>
        <div className="rounded-lg border border-border bg-surface-2 px-2 py-2 text-center">
          <div className="text-micro font-semibold uppercase text-text-2">In use</div>
          <div className="font-mono text-body font-bold text-yellow">{rupees(cap.used_capital)}</div>
        </div>
        <div className="rounded-lg border border-border bg-surface-2 px-2 py-2 text-center">
          <div className="text-micro font-semibold uppercase text-text-2">Available</div>
          <div className={cn("font-mono text-body font-bold", lowFunds ? "text-red" : "text-green")}>{rupees(cap.available_capital)}</div>
          {lowFunds && <div className="text-micro normal-case tracking-normal text-red">below per-trade</div>}
        </div>
      </div>

      {editingCapital && (
        <AmountForm
          label="Total capital (₹)"
          placeholder={String(cap.capital)}
          submitLabel="Save"
          hint="Whole rupees, up to ₹1 crore. The API refuses changes while positions are open."
          validate={validateCapital}
          pending={capMut.isPending}
          onSubmit={(v) => capMut.mutate(v)}
          onCancel={() => setEditingCapital(false)}
          autoFocus
        />
      )}

      <Meter
        label="Capital utilisation"
        pct={usedPct}
        valueText={`${usedPct.toFixed(1)}%`}
        tone={usedPct > 80 ? "bad" : usedPct > 50 ? "warn" : "accent"}
      />
      <Meter
        label={`Daily loss limit (${cap.max_daily_loss_pct}%)`}
        pct={lossOfLimit}
        valueText={`${lossPct.toFixed(1)}% used`}
        tone={lossPct >= cap.max_daily_loss_pct ? "bad" : lossPct > cap.max_daily_loss_pct * 0.7 ? "warn" : "ok"}
      />

      <div className="space-y-2 border-t border-border pt-3">
        <div className="flex items-baseline justify-between">
          <span className="text-micro font-semibold uppercase text-text-2">Per-trade amount</span>
          <span className="font-mono text-body font-bold text-text-1">{rupees(cap.trade_amount)}</span>
        </div>
        <div role="group" aria-label="Per-trade presets" className="grid grid-cols-4 gap-1.5">
          {PRESETS.map((amt) => {
            const current = cap.trade_amount === amt;
            const tooBig = amt > cap.capital;
            return (
              <button
                key={amt}
                type="button"
                aria-pressed={current}
                disabled={tradeMut.isPending || tooBig}
                title={tooBig ? "Exceeds total capital" : undefined}
                onClick={() => tradeMut.mutate(amt)}
                className={cn(
                  "rounded-md border py-1 text-table font-semibold disabled:cursor-not-allowed disabled:opacity-50",
                  current ? "border-accent/60 bg-accent/30 text-text-1" : "border-border bg-surface-2 text-text-2 hover:text-text-1",
                )}
              >
                ₹{amt / 1000}k
              </button>
            );
          })}
          <button
            type="button"
            aria-expanded={customTrade}
            onClick={() => setCustomTrade((v) => !v)}
            className="rounded-md border border-border bg-surface-2 py-1 text-table font-semibold text-text-2 hover:text-text-1"
          >
            {customTrade ? "Cancel" : "Custom"}
          </button>
        </div>
        {customTrade && (
          <AmountForm
            label="Custom per-trade amount (₹)"
            placeholder="e.g. 12500"
            submitLabel="Set"
            validate={(raw) => validateTradeAmount(raw, cap.capital)}
            pending={tradeMut.isPending}
            onSubmit={(v) => tradeMut.mutate(v)}
            autoFocus
          />
        )}
      </div>

      {preview && preview.length > 0 && <PreviewTable rows={preview} onHide={() => setPreview(null)} />}

      <div className="flex items-center justify-between border-t border-border pt-2 text-table text-text-2">
        <span>Trades <span className="font-mono text-text-1">{cap.daily_trades}/{cap.max_trades_per_day}</span></span>
        <span>Open <span className="font-mono text-text-1">{cap.open_positions}/{cap.max_open_positions}</span></span>
        <span className={cn("font-semibold", cap.circuit_breaker ? "text-red" : "text-green")}>
          {cap.circuit_breaker ? "HALTED" : "ACTIVE"}
        </span>
      </div>
    </div>
  );
}

export function CapitalWidget() {
  const query = useQuery({ queryKey: queryKeys.capital, queryFn: getCapital, refetchInterval: 8_000 });
  return (
    <DataSurface query={query} skeleton={<Skeleton label="Loading capital" className="py-4" />}>
      {(cap) => <CapitalBody cap={cap} />}
    </DataSurface>
  );
}
