"use client";
import { useMarketStore } from "@/lib/store";
import { SpotTicker }       from "@/components/market/SpotTicker";
import { OptionChainTable } from "@/components/market/OptionChainTable";
import { CandleChart }      from "@/components/charts/CandleChart";
import { OIBarChart }       from "@/components/market/OIBarChart";
import { Card }             from "@/components/ui/Card";
import { formatPrice, formatNumber } from "@/lib/utils";

// Simple Black-Scholes N(d1) approximation for delta display only
function estimateDelta(spot: number, strike: number, optionType: "CE" | "PE"): number {
  if (!spot || !strike) return 0;
  const moneyness = (spot - strike) / spot;
  // Sigmoid approximation: maps moneyness → delta range
  const raw = 1 / (1 + Math.exp(-10 * moneyness));
  return optionType === "CE" ? raw : raw - 1;
}

function InfoPanel() {
  const { selectedToken, ticks, optionChain } = useMarketStore();
  const tick       = selectedToken ? ticks[selectedToken] : null;
  const allStrikes = optionChain?.strikes ?? [];
  const inst       = allStrikes.find((s) => s.token === selectedToken);

  if (!tick && !inst) {
    return (
      <Card>
        <div className="flex items-center justify-center h-28 text-[11px]" style={{ color: "var(--text-3)" }}>
          Select a strike from the option chain
        </div>
      </Card>
    );
  }

  const spread = (tick?.best_ask ?? 0) - (tick?.best_bid ?? 0);
  // Use live tick LTP if available, fall back to chain snapshot LTP
  const ltp    = tick?.ltp ?? inst?.ltp;
  // Delta: use chain value if present, otherwise estimate from spot/strike
  const spot   = optionChain?.underlying_ltp ?? 0;
  const delta  = inst?.delta != null
    ? inst.delta
    : (inst && spot ? estimateDelta(spot, inst.strike, inst.option_type as "CE" | "PE") : null);

  const rows: [string, string][] = [
    ["LTP",       formatPrice(ltp)],
    ["Bid / Ask", tick ? `${formatPrice(tick.best_bid)} / ${formatPrice(tick.best_ask)}` : "—"],
    ["Spread",    spread > 0 ? formatPrice(spread) : "—"],
    ["OI",        formatNumber(tick?.oi ?? inst?.oi)],
    ["Volume",    formatNumber(tick?.volume ?? inst?.volume)],
    ["Strike",    inst?.strike?.toLocaleString() ?? "—"],
    ["Type",      inst?.option_type ?? "—"],
    ["IV",        inst?.iv ? `${inst.iv.toFixed(2)}%` : "—"],
    ["Delta",     delta != null ? delta.toFixed(3) : "—"],
  ];

  return (
    <Card title={inst?.symbol ?? selectedToken ?? ""}>
      <div className="space-y-0.5">
        {rows.map(([label, val]) => (
          <div key={label} className="flex justify-between items-center py-1.5"
            style={{ borderBottom: "1px solid var(--border)" }}>
            <span className="text-[10px] font-medium" style={{ color: "var(--text-3)" }}>{label}</span>
            <span className="font-mono text-[12px]" style={{ color: "var(--text-1)" }}>{val}</span>
          </div>
        ))}
      </div>
    </Card>
  );
}

export default function DashboardPage() {
  const { selectedToken, optionChain, selectedUnderlying } = useMarketStore();
  // chain is managed inside OptionChainTable via useOptionChain — read from store here
  const chain = optionChain;

  const atm = chain?.underlying_ltp
    ? Math.round(chain.underlying_ltp / (selectedUnderlying === "NIFTY" ? 50 : 100)) *
      (selectedUnderlying === "NIFTY" ? 50 : 100)
    : 0;

  return (
    <div className="p-4 space-y-4 max-w-[1800px] mx-auto">
      <SpotTicker />

      <div className="grid grid-cols-1 xl:grid-cols-[1fr_360px] gap-4">
        {/* Left */}
        <div className="space-y-4">
          <OptionChainTable />
          {chain && chain.strikes.length > 0 && <OIBarChart strikes={chain.strikes} atm={atm} />}
        </div>

        {/* Right */}
        <div className="space-y-4">
          <CandleChart token={selectedToken} />
          <InfoPanel />

          {chain && (
            <Card title="Chain Stats">
              <div className="space-y-0.5">
                {([
                  ["Underlying",    chain.underlying],
                  ["Expiry",        chain.expiry],
                  ["Spot LTP",      formatPrice(chain.underlying_ltp)],
                  ["PCR (OI)",      chain.pcr != null
                    ? <span className="font-bold" style={{ color: chain.pcr > 1 ? "var(--green)" : "var(--red)" }}>
                        {chain.pcr.toFixed(3)}
                      </span>
                    : "—"
                  ],
                  ["Total Strikes", chain.strikes.length],
                ] as [string, any][]).map(([label, val]) => (
                  <div key={label} className="flex justify-between items-center py-1.5"
                    style={{ borderBottom: "1px solid var(--border)" }}>
                    <span className="text-[10px] font-medium" style={{ color: "var(--text-3)" }}>{label}</span>
                    <span className="font-mono text-[12px]" style={{ color: "var(--text-1)" }}>{val}</span>
                  </div>
                ))}
              </div>
            </Card>
          )}
        </div>
      </div>
    </div>
  );
}
