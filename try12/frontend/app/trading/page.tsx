"use client";
import { useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { getDailyStats } from "@/lib/tradingApi";
import { queryKeys } from "@/lib/queryKeys";
import { cn } from "@/lib/utils";
import { PageHeader } from "@/components/ui/PageHeader";
import { Panel } from "@/components/ui/Panel";
import { DataSurface } from "@/components/ui/DataSurface";
import { StatTile } from "@/components/ui/StatTile";
import { DeltaText } from "@/components/ui/PriceText";
import { SpotTicker } from "@/components/market/SpotTicker";
import { ModeToggle } from "@/components/trading/ModeToggle";
import { StrategyPanel } from "@/components/trading/StrategyPanel";
import { SignalsPanel } from "@/components/trading/SignalsPanel";
import { SignalDiagnosticsPanel, type DeskSidebarTab } from "@/components/trading/SignalDiagnosticsPanel";
import { PositionsTable } from "@/components/trading/PositionsTable";
import { PnLChart } from "@/components/trading/PnLChart";
import { CapitalWidget } from "@/components/trading/CapitalWidget";
import { RiskRules } from "@/components/trading/RiskRules";
import { OptionSelectorWidget } from "@/components/trading/OptionSelectorWidget";
import { DownloadPdfButton } from "@/components/trading/DownloadPdfButton";

const SIDEBAR_TABS: { key: DeskSidebarTab; label: string }[] = [
  { key: "capital",    label: "Capital & sizing" },
  { key: "strategies", label: "Strategies" },
  { key: "selector",   label: "Option selector" },
];

// One placeholder per DeskStats tile, keyed by the tile it stands in for.
const STAT_TILE_KEYS = ["pnl", "trades", "win-rate", "open-positions", "available"] as const;

function StatTilesSkeleton() {
  return (
    <div role="status" aria-live="polite" className="grid grid-cols-2 gap-2 md:grid-cols-5">
      <span className="sr-only">Loading daily stats…</span>
      {STAT_TILE_KEYS.map((k) => <div key={k} aria-hidden="true" className="shimmer h-[66px] rounded-lg" />)}
    </div>
  );
}

/** Today's desk numbers. Rendered only from a successful /trading/stats response — never zero-filled. */
function DeskStats() {
  const query = useQuery({ queryKey: queryKeys.stats, queryFn: getDailyStats, refetchInterval: 8_000 });
  return (
    <DataSurface query={query} skeleton={<StatTilesSkeleton />} hideFreshness>
      {(s) => {
        const pnl = typeof s.total_pnl === "number" ? s.total_pnl : null;
        return (
          <div className="space-y-2">
            <div className="grid grid-cols-2 gap-2 md:grid-cols-5" aria-live="polite" aria-label="Today's trading stats">
              <StatTile
                label={`Today P&L · ${s.mode}`}
                value={<DeltaText value={pnl} prefix="₹" decimals={0} />}
                intent={pnl == null || pnl === 0 ? "neutral" : pnl > 0 ? "positive" : "negative"}
              />
              <StatTile label="Trades" value={s.total_trades ?? "—"} />
              <StatTile label="Win rate" value={s.win_rate == null ? "—" : `${s.win_rate}%`} intent="warning" />
              <StatTile label="Open positions" value={s.open_positions ?? "—"} intent="info" />
              <StatTile
                label="Available"
                value={typeof s.available === "number" ? `₹${Math.round(s.available).toLocaleString("en-IN")}` : "—"}
                hint={typeof s.trade_amount === "number" ? `per trade ₹${Math.round(s.trade_amount).toLocaleString("en-IN")}` : undefined}
              />
            </div>
            {s.circuit_breaker_triggered && (
              <div role="alert" className="rounded-md border border-red/40 bg-red/10 px-3 py-2 text-table font-semibold text-red">
                Circuit breaker tripped{s.circuit_reason ? ` — ${s.circuit_reason}` : ""}
              </div>
            )}
          </div>
        );
      }}
    </DataSurface>
  );
}

export default function TradingPage() {
  const [tab, setTab] = useState<DeskSidebarTab>("capital");
  const tabRefs = useRef<Record<DeskSidebarTab, HTMLButtonElement | null>>({ capital: null, strategies: null, selector: null });

  // Diagnostics remedy links land here: switch the tab and move focus to it.
  const openTab = (t: DeskSidebarTab) => {
    setTab(t);
    const el = tabRefs.current[t];
    if (el) {
      el.scrollIntoView({ block: "nearest", behavior: "smooth" });
      el.focus({ preventScroll: true });
    }
  };

  const onTabKey = (e: React.KeyboardEvent, i: number) => {
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft" && e.key !== "Home" && e.key !== "End") return;
    e.preventDefault();
    const last = SIDEBAR_TABS.length - 1;
    const next = e.key === "Home" ? 0 : e.key === "End" ? last : e.key === "ArrowRight" ? (i === last ? 0 : i + 1) : (i === 0 ? last : i - 1);
    openTab(SIDEBAR_TABS[next].key);
  };

  return (
    <div className="mx-auto max-w-[2400px] space-y-4 p-4">
      <PageHeader
        title="Execution desk"
        description="Strategy signals, why they do or do not convert into trades, open risk and sizing."
      />
      <SpotTicker />

      <div className="grid grid-cols-1 items-start gap-4 xl:grid-cols-[minmax(0,1fr)_380px] 2xl:grid-cols-[minmax(0,1fr)_440px]">
        {/* Left: performance, signals, diagnostics, positions */}
        <div className="min-w-0 space-y-4">
          <DeskStats />
          <PnLChart />
          <SignalsPanel />
          <SignalDiagnosticsPanel onNavigate={openTab} />
          <PositionsTable />
        </div>

        {/* Right: mode, settings tabs, report */}
        <div className="min-w-0 space-y-4">
          <ModeToggle />

          <section aria-label="Desk settings" className="panel">
            <div role="tablist" aria-label="Desk settings" className="flex border-b border-border">
              {SIDEBAR_TABS.map((t, i) => (
                <button
                  key={t.key}
                  ref={(el) => { tabRefs.current[t.key] = el; }}
                  id={`desk-tab-${t.key}`}
                  type="button"
                  role="tab"
                  aria-selected={tab === t.key}
                  aria-controls={tab === t.key ? `desk-panel-${t.key}` : undefined}
                  onClick={() => setTab(t.key)}
                  onKeyDown={(e) => onTabKey(e, i)}
                  className={cn(
                    "relative flex-1 py-2.5 text-table font-semibold",
                    tab === t.key ? "text-text-1" : "text-text-2 hover:text-text-1",
                  )}
                >
                  {t.label}
                  {tab === t.key && <span aria-hidden="true" className="absolute inset-x-0 bottom-0 h-0.5 bg-accent" />}
                </button>
              ))}
            </div>
            <div id={`desk-panel-${tab}`} role="tabpanel" aria-labelledby={`desk-tab-${tab}`} className="p-4">
              {tab === "capital" && (
                <div className="space-y-4">
                  <CapitalWidget />
                  <RiskRules />
                </div>
              )}
              {tab === "strategies" && <StrategyPanel />}
              {tab === "selector" && <OptionSelectorWidget />}
            </div>
          </section>

          <Panel title="Trade report">
            <DownloadPdfButton />
          </Panel>
        </div>
      </div>
    </div>
  );
}
