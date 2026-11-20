"use client";
import { cn, formatNumber } from "@/lib/utils";
import { useOptionChain } from "@/hooks/useOptionChain";
import { SpotTicker }       from "@/components/market/SpotTicker";
import { OptionChainTable } from "@/components/market/OptionChainTable";
import { OIBarChart }       from "@/components/market/OIBarChart";
import { InfoPanel }        from "@/components/market/InfoPanel";
import { DataHealthPanel }  from "@/components/market/DataHealthPanel";
import { CandleChart }      from "@/components/charts/CandleChart";
import { LevelsPanel }      from "@/components/charts/LevelsPanel";
import { Panel }            from "@/components/ui/Panel";
import { DataSurface }      from "@/components/ui/DataSurface";
import { Skeleton }         from "@/components/ui/Skeleton";
import { PriceText }        from "@/components/ui/PriceText";

function ChainStats() {
  const { query } = useOptionChain();
  return (
    <Panel title="Chain stats" dense>
      <DataSurface query={query} skeleton={<Skeleton label="Loading chain stats" />}>
        {(chain) => {
          const strikes = Array.isArray(chain.strikes) ? chain.strikes : [];
          const ceOi = strikes.filter((s) => s.option_type === "CE").reduce((a, s) => a + (s.oi ?? 0), 0);
          const peOi = strikes.filter((s) => s.option_type === "PE").reduce((a, s) => a + (s.oi ?? 0), 0);
          const rows: [string, React.ReactNode][] = [
            ["Underlying", chain.underlying],
            ["Expiry",     chain.expiry ?? "—"],
            ["Spot LTP",   <PriceText key="spot" value={chain.underlying_ltp} />],
            ["PCR (OI)",   chain.pcr != null
              ? <span key="pcr" className={cn("font-semibold", chain.pcr > 1 ? "text-green" : "text-red")}>{chain.pcr.toFixed(3)}</span>
              : "—"],
            ["Max pain",   chain.max_pain != null ? <PriceText key="mp" value={chain.max_pain} decimals={0} /> : "—"],
            ["CE / PE OI", `${formatNumber(ceOi)} / ${formatNumber(peOi)}`],
            ["Strikes",    strikes.length],
          ];
          return (
            <dl className="divide-y divide-border">
              {rows.map(([label, val]) => (
                <div key={label} className="flex items-center justify-between py-1.5">
                  <dt className="text-table text-text-3">{label}</dt>
                  <dd className="font-mono text-table tabular-nums text-text-1">{val}</dd>
                </div>
              ))}
            </dl>
          );
        }}
      </DataSurface>
    </Panel>
  );
}

export default function TerminalPage() {
  return (
    <div className="mx-auto max-w-[2400px] space-y-4 p-4">
      <h1 className="sr-only">Market terminal</h1>
      <SpotTicker />

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-[minmax(0,1fr)_360px] 2xl:grid-cols-[minmax(0,1fr)_420px]">
        {/* Left: chart + levels hero, then the chain and its OI distribution */}
        <div className="min-w-0 space-y-4">
          <div className="grid grid-cols-1 gap-4 2xl:grid-cols-[minmax(0,1fr)_420px]">
            <div className="min-w-0"><CandleChart /></div>
            <LevelsPanel className="min-w-0" />
          </div>
          <OptionChainTable />
          <OIBarChart />
        </div>

        {/* Right: selected instrument + chain stats */}
        <aside aria-label="Instrument details" className="min-w-0 space-y-4">
          <InfoPanel />
          <ChainStats />
          <DataHealthPanel />
        </aside>
      </div>
    </div>
  );
}
