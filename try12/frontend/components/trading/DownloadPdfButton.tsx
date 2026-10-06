"use client";
import { useId, useState } from "react";
import { downloadReportPdf } from "@/lib/tradingApi";
import { ApiError, errorMessage } from "@/lib/http";
import { cn } from "@/lib/utils";

// Trade-report PDF download (extracted from the old page). downloadReportPdf() rejects non-2xx
// responses with an ApiError carrying FastAPI's JSON `detail` (parsed from the Blob body), and
// rejects a 2xx whose content-type is not application/pdf — so an error body is never saved as .pdf.

const DAY_OPTIONS = [7, 14, 30, 60, 90] as const;
const MODE_OPTIONS = ["ALL", "PAPER", "LIVE"] as const;
type ReportMode = (typeof MODE_OPTIONS)[number];

export function DownloadPdfButton() {
  const [days, setDays] = useState<number>(30);
  const [mode, setMode] = useState<ReportMode>("ALL");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const daysId = useId();

  async function handleDownload() {
    setLoading(true);
    setError(null);
    let url: string | null = null;
    try {
      const blob = await downloadReportPdf(mode, days);
      url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `trades_${new Date().toISOString().slice(0, 10)}_${mode.toLowerCase()}_${days}d.pdf`;
      document.body.appendChild(a);
      a.click();
      a.remove();
    } catch (e: unknown) {
      console.error("[report] PDF download failed", e);
      const detail = e instanceof ApiError && e.detail ? ` — ${e.detail}` : "";
      setError(`${errorMessage(e)}${detail}`);
    } finally {
      if (url) URL.revokeObjectURL(url);
      setLoading(false);
    }
  }

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <div role="group" aria-label="Report mode" className="inline-flex overflow-hidden rounded-md border border-border">
          {MODE_OPTIONS.map((m) => (
            <button
              key={m}
              type="button"
              aria-pressed={mode === m}
              onClick={() => setMode(m)}
              className={cn(
                "px-2.5 py-1 text-table font-semibold",
                mode === m ? "bg-accent/30 text-text-1" : "bg-surface-2 text-text-2 hover:text-text-1",
              )}
            >
              {m}
            </button>
          ))}
        </div>
        <label htmlFor={daysId} className="flex items-center gap-1.5 text-table text-text-2">
          Days
          <select
            id={daysId}
            value={days}
            onChange={(e) => setDays(Number(e.target.value))}
            className="rounded-md border border-border bg-surface-2 px-2 py-1 text-table text-text-1"
          >
            {DAY_OPTIONS.map((d) => <option key={d} value={d}>{d}</option>)}
          </select>
        </label>
        <button
          type="button"
          onClick={handleDownload}
          disabled={loading}
          className="ml-auto inline-flex items-center gap-1.5 rounded-md border border-accent/60 bg-accent/30 px-3 py-1 text-table font-semibold text-text-1 hover:bg-accent/40 disabled:opacity-50"
        >
          <svg aria-hidden="true" className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" d="M4 16v2a2 2 0 002 2h12a2 2 0 002-2v-2M7 10l5 5 5-5M12 15V3" />
          </svg>
          {loading ? "Generating…" : "Download PDF"}
        </button>
      </div>
      {error && (
        <p role="alert" className="break-words text-table text-red">
          {error}
        </p>
      )}
    </div>
  );
}
