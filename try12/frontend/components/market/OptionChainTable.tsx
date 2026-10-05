"use client";
import { useMemo } from "react";
import { useMarketStore } from "@/lib/store";
import { useOptionChain } from "@/hooks/useOptionChain";
import { useOptionChainFeed } from "@/hooks/useTickFeed";
import { formatPrice, formatNumber, cn } from "@/lib/utils";
import type { OptionStrike } from "@/types/market";

interface StrikeRowProps {
  strike:   number;
  ce?:      OptionStrike;
  pe?:      OptionStrike;
  atm:      number;
  onSelect: (token: string) => void;
}

function StrikeRow({ strike, ce, pe, atm, onSelect }: StrikeRowProps) {
  const ceToken  = ce?.token ?? null;
  const peToken  = pe?.token ?? null;
  // Single selector — avoids two separate subscriptions (and two re-renders) per row
  const { ceTick, peTick } = useMarketStore((s) => ({
    ceTick: ceToken ? s.ticks[ceToken] : null,
    peTick: peToken ? s.ticks[peToken] : null,
  }));
  const isATM    = Math.abs(strike - atm) < 50;
  const isITM_CE = strike < atm;
  const isITM_PE = strike > atm;

  const ceLtp = ceTick?.ltp ?? ce?.ltp;
  const peLtp = peTick?.ltp ?? pe?.ltp;
  const ceOI  = ceTick?.oi  ?? ce?.oi;
  const peOI  = peTick?.oi  ?? pe?.oi;

  return (
    <tr style={{ borderBottom: "1px solid var(--border)", background: isATM ? "var(--accent-dim)" : "transparent" }}>
      <td className="px-3 py-2 text-right font-mono text-[11px]" style={{ color: isITM_CE ? "var(--text-3)" : "var(--green)" }}>
        {ceLtp ? formatPrice(ceLtp) : "—"}
      </td>
      <td className="px-3 py-2 text-right font-mono text-[11px]" style={{ color: "var(--text-3)" }}>
        {ce?.iv ? `${ce.iv.toFixed(1)}%` : "—"}
      </td>
      <td className="px-3 py-2 text-right font-mono text-[11px]" style={{ color: "var(--text-3)" }}>
        {formatNumber(ceOI)}
      </td>
      <td className="px-3 py-2 text-right font-mono text-[11px] cursor-pointer" style={{ color: "var(--text-3)" }}
        onClick={() => ce && onSelect(ce.token)}>
        {ceTick?.volume ? formatNumber(ceTick.volume) : "—"}
      </td>

      <td className="px-4 py-2 text-center font-bold font-mono text-[12px]"
        style={{ color: isATM ? "var(--yellow)" : "var(--text-1)" }}>
        {strike.toLocaleString()}
        {isATM && <span className="ml-1 text-[9px]" style={{ color: "var(--yellow)" }}>ATM</span>}
      </td>

      <td className="px-3 py-2 text-left font-mono text-[11px] cursor-pointer" style={{ color: "var(--text-3)" }}
        onClick={() => pe && onSelect(pe.token)}>
        {peTick?.volume ? formatNumber(peTick.volume) : "—"}
      </td>
      <td className="px-3 py-2 text-left font-mono text-[11px]" style={{ color: "var(--text-3)" }}>
        {formatNumber(peOI)}
      </td>
      <td className="px-3 py-2 text-left font-mono text-[11px]" style={{ color: "var(--text-3)" }}>
        {pe?.iv ? `${pe.iv.toFixed(1)}%` : "—"}
      </td>
      <td className="px-3 py-2 text-left font-mono text-[11px]" style={{ color: isITM_PE ? "var(--text-3)" : "var(--red)" }}>
        {peLtp ? formatPrice(peLtp) : "—"}
      </td>
    </tr>
  );
}

export function OptionChainTable() {
  const { selectedUnderlying, setUnderlying, setSelectedToken } = useMarketStore();
  const { chain, isLoading } = useOptionChain();
  useOptionChainFeed(selectedUnderlying);

  const atmStrike = chain?.underlying_ltp
    ? Math.round(chain.underlying_ltp / (selectedUnderlying === "NIFTY" ? 50 : 100)) *
      (selectedUnderlying === "NIFTY" ? 50 : 100)
    : 0;

  const strikeMap = useMemo(() => {
    const map: Record<number, { ce?: OptionStrike; pe?: OptionStrike }> = {};
    for (const s of chain?.strikes ?? []) {
      if (!map[s.strike]) map[s.strike] = {};
      if (s.option_type === "CE") map[s.strike].ce = s;
      else                        map[s.strike].pe = s;
    }
    return map;
  }, [chain?.strikes]);

  const sortedStrikes = useMemo(
    () => Object.keys(strikeMap).map(Number).sort((a, b) => a - b),
    [strikeMap]
  );

  const TH = "px-3 py-2.5 font-semibold text-[10px] uppercase tracking-wider whitespace-nowrap";

  return (
    <div className="rounded-xl overflow-hidden" style={{ background: "var(--surface)", border: "1px solid var(--border)" }}>
      {/* Controls */}
      <div className="flex items-center gap-4 px-4 py-3" style={{ borderBottom: "1px solid var(--border)" }}>
        <div className="flex gap-1 p-0.5 rounded-lg" style={{ background: "var(--surface-2)", border: "1px solid var(--border)" }}>
          {(["NIFTY", "BANKNIFTY"] as const).map((u) => (
            <button key={u} onClick={() => setUnderlying(u)}
              className="px-3 py-1.5 rounded-md text-[11px] font-semibold transition-all"
              style={{
                background: selectedUnderlying === u ? "var(--accent)" : "transparent",
                color:      selectedUnderlying === u ? "#fff" : "var(--text-3)",
              }}>
              {u}
            </button>
          ))}
        </div>

        {chain && (
          <div className="text-[11px] font-mono" style={{ color: "var(--text-3)" }}>
            Spot <span className="font-bold" style={{ color: "var(--text-1)" }}>{formatPrice(chain.underlying_ltp)}</span>
            {chain.pcr != null && (
              <span className="ml-3">
                PCR <span className="font-bold" style={{ color: chain.pcr > 1 ? "var(--green)" : "var(--red)" }}>
                  {chain.pcr.toFixed(3)}
                </span>
              </span>
            )}
          </div>
        )}

        <div className="ml-auto font-mono text-[10px]" style={{ color: "var(--text-4)" }}>
          {chain?.expiry ?? "—"}
        </div>
      </div>

      {isLoading ? (
        <div className="flex items-center justify-center h-40 text-[11px]" style={{ color: "var(--text-3)" }}>
          Loading option chain…
        </div>
      ) : (
        <div className="overflow-auto" style={{ maxHeight: "60vh" }}>
          <table className="w-full">
            <thead className="sticky top-0 z-10" style={{ background: "var(--surface-2)", borderBottom: "1px solid var(--border)" }}>
              <tr style={{ color: "var(--text-3)" }}>
                <th className={cn(TH, "text-right w-24")}>LTP</th>
                <th className={cn(TH, "text-right w-16")}>IV</th>
                <th className={cn(TH, "text-right w-20")}>OI</th>
                <th className={cn(TH, "text-right w-20")} style={{ color: "var(--green)" }}>CE Vol</th>
                <th className={cn(TH, "text-center w-28")} style={{ color: "var(--text-1)" }}>Strike</th>
                <th className={cn(TH, "text-left w-20")} style={{ color: "var(--red)" }}>PE Vol</th>
                <th className={cn(TH, "text-left w-20")}>OI</th>
                <th className={cn(TH, "text-left w-16")}>IV</th>
                <th className={cn(TH, "text-left w-24")}>LTP</th>
              </tr>
            </thead>
            <tbody>
              {sortedStrikes.map((strike) => (
                <StrikeRow key={strike} strike={strike}
                  ce={strikeMap[strike].ce} pe={strikeMap[strike].pe}
                  atm={atmStrike} onSelect={setSelectedToken} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
