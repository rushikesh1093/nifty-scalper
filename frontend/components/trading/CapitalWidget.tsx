"use client";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { getCapital, setTradeAmount, setCapital } from "@/lib/tradingApi";
import { useState } from "react";
import { cn } from "@/lib/utils";
import toast from "react-hot-toast";
import type { CapitalSummary } from "@/types/trading";

const PRESETS = [10000, 15000, 20000, 25000, 50000, 75000];
const fmt = (n: number) => "₹" + n.toLocaleString("en-IN", { maximumFractionDigits: 0 });

function ProgressBar({ pct, color }: { pct: number; color: string }) {
  return (
    <div className="w-full h-1 rounded-full overflow-hidden" style={{ background: "var(--surface-3)" }}>
      <div className="h-full rounded-full transition-all duration-700" style={{ width: `${Math.min(Math.max(pct,0),100)}%`, background: color }} />
    </div>
  );
}

export function CapitalWidget() {
  const qc = useQueryClient();
  const [editing, setEditing]   = useState<null | "trade" | "capital">(null);
  const [inputVal, setInputVal] = useState("");

  const { data: cap } = useQuery<CapitalSummary>({
    queryKey: ["capital"], queryFn: getCapital, refetchInterval: 8000,
  });

  const tradeMut = useMutation({
    mutationFn: (ta: number) => setTradeAmount(ta),
    onSuccess: (d: any) => {
      qc.invalidateQueries({ queryKey: ["capital"] });
      qc.invalidateQueries({ queryKey: ["config"] });
      toast.success(`Per-trade set to Rs.${d.trade_amount?.toLocaleString("en-IN")}`);
      setEditing(null); setInputVal("");
    },
    onError: (e: any) => toast.error(e.response?.data?.detail || "Failed"),
  });

  const capMut = useMutation({
    mutationFn: (c: number) => setCapital(c),
    onSuccess: (d: any) => {
      qc.invalidateQueries({ queryKey: ["capital"] });
      qc.invalidateQueries({ queryKey: ["config"] });
      toast.success(`Capital set to Rs.${d.capital?.toLocaleString("en-IN")}`);
      setEditing(null); setInputVal("");
    },
    onError: (e: any) => toast.error(e.response?.data?.detail || "Failed"),
  });

  if (!cap) return <div className="rounded-xl h-40 shimmer" />;

  const usedPct = cap.utilisation_pct ?? 0;
  const lossPct = cap.capital > 0 ? Math.abs(cap.realised_pnl) / cap.capital * 100 : 0;
  const pnlPositive = cap.realised_pnl >= 0;

  return (
    <div className="space-y-4">
      {/* Circuit breaker banner */}
      {cap.circuit_breaker && (
        <div className="flex items-center gap-2 px-3 py-2 rounded-lg text-[11px] font-semibold"
          style={{ background: "var(--red-dim)", border: "1px solid #f43f5e30", color: "var(--red)" }}>
          <svg className="w-3.5 h-3.5 shrink-0" fill="currentColor" viewBox="0 0 20 20">
            <path fillRule="evenodd" d="M8.257 3.099c.765-1.36 2.722-1.36 3.486 0l5.58 9.92c.75 1.334-.213 2.98-1.742 2.98H4.42c-1.53 0-2.493-1.646-1.743-2.98l5.58-9.92zM11 13a1 1 0 11-2 0 1 1 0 012 0zm-1-8a1 1 0 00-1 1v3a1 1 0 002 0V6a1 1 0 00-1-1z" clipRule="evenodd" />
          </svg>
          {cap.circuit_reason}
        </div>
      )}

      {/* Capital header */}
      <div className="flex items-center justify-between">
        <span className="text-[11px] font-semibold uppercase tracking-wider" style={{ color: "var(--text-3)" }}>Capital</span>
        <span className="font-mono text-[11px] font-semibold" style={{ color: pnlPositive ? "var(--green)" : "var(--red)" }}>
          {pnlPositive ? "+" : ""}₹{cap.realised_pnl.toLocaleString("en-IN", { maximumFractionDigits: 0 })} today
        </span>
      </div>

      {/* Three capital cards */}
      <div className="grid grid-cols-3 gap-2">
        {[
          { label: "Total",     val: fmt(cap.capital),            color: "var(--text-1)",  action: () => setEditing("capital") },
          { label: "In Use",    val: fmt(cap.used_capital),       color: "var(--yellow)",  action: null },
          { label: "Available", val: fmt(cap.available_capital),  color: cap.available_capital < cap.trade_amount ? "var(--red)" : "var(--green)", action: null },
        ].map(({ label, val, color, action }) => (
          <div
            key={label}
            onClick={() => action?.()}
            className="rounded-lg px-2 py-2.5 text-center"
            style={{
              background: "var(--surface-2)",
              border: "1px solid var(--border)",
              cursor: action ? "pointer" : "default",
            }}
          >
            <div className="text-[9px] font-semibold uppercase tracking-wider mb-1" style={{ color: "var(--text-3)" }}>{label}</div>
            <div className="font-mono font-bold" style={{ fontSize: 13, color }}>{val}</div>
            {action && <div className="text-[9px] mt-0.5" style={{ color: "var(--text-4)" }}>tap to edit</div>}
          </div>
        ))}
      </div>

      {/* Capital edit */}
      {editing === "capital" && (
        <div className="flex gap-2">
          <input type="number" value={inputVal} onChange={e => setInputVal(e.target.value)}
            placeholder={String(cap.capital)} autoFocus
            className="flex-1 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none"
            style={{ background: "var(--surface-2)", border: "1px solid var(--accent)", color: "var(--text-1)" }}
          />
          <button onClick={() => { const v = parseFloat(inputVal); if (v > 0) capMut.mutate(v); }}
            disabled={capMut.isPending}
            className="px-3 py-2 rounded-lg text-xs font-semibold"
            style={{ background: "var(--accent)", color: "#fff" }}>Save</button>
          <button onClick={() => { setEditing(null); setInputVal(""); }}
            className="px-3 py-2 rounded-lg text-xs"
            style={{ background: "var(--surface-3)", color: "var(--text-2)" }}>Cancel</button>
        </div>
      )}

      {/* Utilisation */}
      <div className="space-y-1.5">
        <div className="flex justify-between" style={{ fontSize: 10, color: "var(--text-3)" }}>
          <span>Capital utilisation</span><span>{usedPct.toFixed(1)}%</span>
        </div>
        <ProgressBar pct={usedPct} color={usedPct > 80 ? "var(--red)" : usedPct > 50 ? "var(--yellow)" : "var(--accent)"} />
      </div>

      {/* Daily loss */}
      <div className="space-y-1.5">
        <div className="flex justify-between" style={{ fontSize: 10, color: "var(--text-3)" }}>
          <span>Daily loss limit ({cap.max_daily_loss_pct}%)</span><span>{lossPct.toFixed(1)}% used</span>
        </div>
        <ProgressBar pct={cap.max_daily_loss_pct > 0 ? lossPct / cap.max_daily_loss_pct * 100 : 0}
          color={lossPct >= cap.max_daily_loss_pct ? "var(--red)" : lossPct > cap.max_daily_loss_pct * 0.7 ? "var(--yellow)" : "var(--green)"} />
      </div>

      {/* Per-trade amount */}
      <div className="pt-3" style={{ borderTop: "1px solid var(--border)" }}>
        <div className="flex items-center justify-between mb-2">
          <span className="text-[10px] font-semibold uppercase tracking-wider" style={{ color: "var(--text-3)" }}>Per-trade amount</span>
          <button onClick={() => setEditing(editing === "trade" ? null : "trade")}
            className="text-[10px] font-medium transition-colors"
            style={{ color: "var(--accent)" }}>
            {editing === "trade" ? "cancel" : "change"}
          </button>
        </div>

        {editing !== "trade" ? (
          <div className="space-y-2">
            <div className="grid grid-cols-3 gap-1.5">
              {PRESETS.map(amt => (
                <button key={amt} onClick={() => tradeMut.mutate(amt)} disabled={tradeMut.isPending}
                  className="py-1.5 rounded-lg text-[10px] font-semibold transition-all"
                  style={{
                    background: cap.trade_amount === amt ? "var(--accent-dim)" : "var(--surface-2)",
                    border: `1px solid ${cap.trade_amount === amt ? "var(--accent-glow)" : "var(--border)"}`,
                    color: cap.trade_amount === amt ? "var(--accent)" : "var(--text-3)",
                  }}>
                  ₹{(amt / 1000).toFixed(0)}k
                </button>
              ))}
            </div>
            <div className="text-right font-mono font-bold" style={{ fontSize: 13, color: "var(--accent)" }}>
              {fmt(cap.trade_amount)} <span className="text-[9px] font-normal" style={{ color: "var(--text-3)" }}>current</span>
            </div>
          </div>
        ) : (
          <div className="flex gap-2">
            <input type="number" value={inputVal} onChange={e => setInputVal(e.target.value)}
              placeholder="custom amount" autoFocus
              className="flex-1 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none"
              style={{ background: "var(--surface-2)", border: "1px solid var(--accent)", color: "var(--text-1)" }}
            />
            <button onClick={() => { const v = parseFloat(inputVal); if (v > 0) tradeMut.mutate(v); }}
              disabled={tradeMut.isPending}
              className="px-3 py-2 rounded-lg text-xs font-semibold"
              style={{ background: "var(--accent)", color: "#fff" }}>Set</button>
          </div>
        )}
      </div>

      {/* Footer stats */}
      <div className="flex items-center justify-between pt-2" style={{ borderTop: "1px solid var(--border)", fontSize: 10, color: "var(--text-3)" }}>
        <span>Trades <span className="font-mono" style={{ color: "var(--text-1)" }}>{cap.daily_trades}/{cap.max_trades_per_day}</span></span>
        <span>Open <span className="font-mono" style={{ color: "var(--text-1)" }}>{cap.open_positions}/{cap.max_open_positions}</span></span>
        <span className="font-semibold" style={{ color: cap.circuit_breaker ? "var(--red)" : "var(--green)" }}>
          {cap.circuit_breaker ? "HALTED" : "ACTIVE"}
        </span>
      </div>
    </div>
  );
}
