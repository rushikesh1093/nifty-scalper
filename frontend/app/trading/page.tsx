"use client";
import { useState } from "react";
import { SpotTicker }     from "@/components/market/SpotTicker";
import { ModeToggle }     from "@/components/trading/ModeToggle";
import { StrategyPanel }  from "@/components/trading/StrategyPanel";
import { SignalsTable }   from "@/components/trading/SignalsTable";
import { PositionsTable } from "@/components/trading/PositionsTable";
import { PnLChart }       from "@/components/trading/PnLChart";
import { CapitalWidget }  from "@/components/trading/CapitalWidget";
import { cn } from "@/lib/utils";

type PosTab     = "open" | "closed";
type SidebarTab = "capital" | "strategies";

// ── PDF Download ─────────────────────────────────────────────────────────────
const BASE       = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api/v1";
const DAY_OPTS   = [7, 14, 30, 60, 90] as const;
const MODE_OPTS  = ["ALL", "PAPER", "LIVE"] as const;
type  ReportMode = typeof MODE_OPTS[number];

function DownloadPDF() {
  const [days,    setDays]    = useState<number>(30);
  const [mode,    setMode]    = useState<ReportMode>("ALL");
  const [loading, setLoading] = useState(false);
  const [error,   setError]   = useState<string | null>(null);

  async function handleDownload() {
    setLoading(true); setError(null);
    try {
      const res = await fetch(`${BASE}/trading/report/pdf?mode=${mode}&days=${days}`);
      if (!res.ok) throw new Error(`Error ${res.status}`);
      const blob = await res.blob();
      const a = Object.assign(document.createElement("a"), {
        href:     URL.createObjectURL(blob),
        download: `trades_${new Date().toISOString().slice(0,10)}_${mode.toLowerCase()}_${days}d.pdf`,
      });
      document.body.appendChild(a); a.click(); document.body.removeChild(a);
      URL.revokeObjectURL(a.href);
    } catch (e: any) {
      setError(e?.message ?? "Download failed");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="flex items-center gap-2 flex-wrap">
      {/* Mode pills */}
      <div className="flex rounded-lg overflow-hidden" style={{ border: "1px solid var(--border)" }}>
        {MODE_OPTS.map(m => (
          <button key={m} onClick={() => setMode(m)}
            className="px-2.5 py-1.5 text-[10px] font-semibold transition-all"
            style={{
              background: mode === m ? "var(--accent)" : "var(--surface-2)",
              color:      mode === m ? "#fff" : "var(--text-3)",
            }}>
            {m}
          </button>
        ))}
      </div>

      {/* Days */}
      <select value={days} onChange={e => setDays(Number(e.target.value))}
        className="rounded-lg px-2.5 py-1.5 text-[10px] focus:outline-none"
        style={{ background: "var(--surface-2)", border: "1px solid var(--border)", color: "var(--text-2)" }}>
        {DAY_OPTS.map(d => <option key={d} value={d}>{d} days</option>)}
      </select>

      {/* Button */}
      <button onClick={handleDownload} disabled={loading}
        className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-[11px] font-semibold transition-all disabled:opacity-50"
        style={{
          background: "var(--accent-dim)",
          color:      "var(--accent)",
          border:     "1px solid var(--accent-glow)",
        }}>
        {loading ? (
          <>
            <span className="w-3 h-3 rounded-full border-2 animate-spin"
              style={{ borderColor: "var(--accent-glow)", borderTopColor: "var(--accent)" }} />
            Generating…
          </>
        ) : (
          <>
            <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" d="M4 16v2a2 2 0 002 2h12a2 2 0 002-2v-2M7 10l5 5 5-5M12 15V3" />
            </svg>
            PDF Report
          </>
        )}
      </button>

      {error && <span className="text-[10px] max-w-[180px] truncate" style={{ color: "var(--red)" }}>{error}</span>}
    </div>
  );
}


// ── Sidebar tab ───────────────────────────────────────────────────────────────
const SIDEBAR_TABS = [
  { key: "capital",    label: "Capital & Sizing" },
  { key: "strategies", label: "Strategies" },
] as const;


// ── Page ─────────────────────────────────────────────────────────────────────
export default function TradingPage() {
  const [posTab,     setPosTab]     = useState<PosTab>("open");
  const [sidebarTab, setSidebarTab] = useState<SidebarTab>("capital");

  return (
    <div className="p-4 max-w-[1900px] mx-auto space-y-4">
      <SpotTicker />

      <div className="grid grid-cols-1 xl:grid-cols-[1fr_320px] gap-4 items-start">

        {/* ── LEFT ─────────────────────────────────────────────────────────── */}
        <div className="space-y-4">
          <PnLChart />
          <SignalsTable />

          {/* Positions section */}
          <div>
            {/* Tab row + PDF button */}
            <div className="flex items-center justify-between mb-3 gap-2 flex-wrap">
              {/* Tabs */}
              <div className="flex gap-1 p-1 rounded-xl" style={{ background: "var(--surface-2)", border: "1px solid var(--border)" }}>
                {(["open", "closed"] as PosTab[]).map(t => (
                  <button key={t} onClick={() => setPosTab(t)}
                    className="px-3 py-1.5 rounded-lg text-[11px] font-semibold transition-all"
                    style={{
                      background: posTab === t ? "var(--accent)" : "transparent",
                      color:      posTab === t ? "#fff" : "var(--text-3)",
                    }}>
                    {t === "open" ? "Open Positions" : "Closed Today"}
                  </button>
                ))}
              </div>
              <DownloadPDF />
            </div>
            <PositionsTable showClosed={posTab === "closed"} />
          </div>
        </div>

        {/* ── RIGHT SIDEBAR ─────────────────────────────────────────────────── */}
        <div className="space-y-4">
          <ModeToggle />

          {/* Capital / Strategies tabs */}
          <div className="rounded-xl overflow-hidden" style={{ background: "var(--surface)", border: "1px solid var(--border)" }}>
            {/* Tab bar */}
            <div className="flex" style={{ borderBottom: "1px solid var(--border)" }}>
              {SIDEBAR_TABS.map(t => (
                <button key={t.key} onClick={() => setSidebarTab(t.key as SidebarTab)}
                  className="flex-1 py-2.5 text-[11px] font-semibold transition-all relative"
                  style={{ color: sidebarTab === t.key ? "var(--text-1)" : "var(--text-3)" }}>
                  {t.label}
                  {sidebarTab === t.key && (
                    <span className="absolute bottom-0 left-0 right-0 h-px" style={{ background: "var(--accent)" }} />
                  )}
                </button>
              ))}
            </div>
            <div className="p-4">
              {sidebarTab === "capital"    && <CapitalWidget />}
              {sidebarTab === "strategies" && <StrategyPanel />}
            </div>
          </div>
        </div>

      </div>
    </div>
  );
}
