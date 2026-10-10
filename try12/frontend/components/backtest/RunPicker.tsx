"use client";
import { useId, useState } from "react";
import { useRouter } from "next/navigation";
import { useRuns } from "@/hooks/useBacktest";
import { formatRunRef, parseRunRef, type RunRef } from "@/lib/backtestMath";
import { DataSurface } from "@/components/ui/DataSurface";
import { Panel } from "@/components/ui/Panel";
import { Skeleton } from "@/components/ui/Skeleton";
import type { RunSummary } from "@/types/backtest";

// Pick two runs for /backtest/compare (design §6.3). A missing or malformed ?a= / ?b= is a user
// mistake, not a failure: this picker renders with "Pick two runs to compare" — no run fetch and no
// error toast. Choosing writes ?a=<dataset_id>:<run_id>&b=… and the comparison takes over.

const PICK_LIMIT = 200;

function optionText(r: RunSummary): string {
  const label = r.label ? ` · ${r.label}` : "";
  return `${r.run_id}${label} · ${r.dataset} · ${r.synthetic_state}`;
}

function SideSelect({
  side, value, onChange, runs, disabled,
}: { side: "A" | "B"; value: string; onChange: (v: string) => void; runs: RunSummary[]; disabled?: boolean }) {
  const id = useId();
  const known = runs.some((r) => formatRunRef({ dataset: r.dataset, runId: r.run_id }) === value);
  return (
    <div className="flex min-w-0 flex-1 flex-col gap-1">
      <label htmlFor={id} className="text-micro font-semibold uppercase text-text-3">Run {side}</label>
      <select
        id={id}
        value={value}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value)}
        className="w-full rounded-md border border-border-2 bg-surface-2 px-2 py-1.5 font-mono text-table text-text-1 disabled:opacity-60"
      >
        <option value="">Select a run…</option>
        {value && !known && <option value={value}>{value}</option>}
        {runs.map((r) => {
          const ref = formatRunRef({ dataset: r.dataset, runId: r.run_id });
          return <option key={ref} value={ref}>{optionText(r)}</option>;
        })}
      </select>
    </div>
  );
}

export function RunPicker({
  a, b, message, lockA,
}: {
  a: RunRef | null;
  b: RunRef | null;
  /** Shown as a polite status line above the selects. */
  message?: string;
  /** Keep side A fixed (the a === b case: only side B needs a new pick). */
  lockA?: boolean;
}) {
  const router = useRouter();
  const runs = useRuns({ limit: PICK_LIMIT });
  const [va, setVa] = useState(a ? formatRunRef(a) : "");
  const [vb, setVb] = useState(b ? formatRunRef(b) : "");
  const ra = parseRunRef(va);
  const rb = parseRunRef(vb);
  const same = !!ra && !!rb && va === vb;
  const ready = !!ra && !!rb && !same;

  const go = () => {
    if (!ready) return;
    router.replace(`/backtest/compare?a=${encodeURIComponent(va)}&b=${encodeURIComponent(vb)}`, { scroll: false });
  };

  return (
    <Panel title="Pick runs">
      {message && <p role="status" className="mb-3 text-body text-text-1">{message}</p>}
      <DataSurface query={runs} skeleton={<Skeleton label="Loading runs" />} hideFreshness
        isEmpty={(d) => d.items.length === 0} emptyTitle="No backtest runs yet"
        emptyHint="Launch one from the Backtest page, then come back to compare.">
        {(d) => (
          <form
            className="space-y-3"
            onSubmit={(e) => { e.preventDefault(); go(); }}
          >
            <div className="flex flex-col gap-3 md:flex-row">
              <SideSelect side="A" value={va} onChange={setVa} runs={d.items} disabled={lockA} />
              <SideSelect side="B" value={vb} onChange={setVb} runs={d.items} />
            </div>
            {same && <p className="text-table text-yellow">Pick two different runs.</p>}
            <div className="flex items-center gap-3">
              <button
                type="submit"
                disabled={!ready}
                className="rounded-md border border-accent/60 bg-accent/20 px-3 py-1 text-table font-semibold text-text-1 hover:bg-accent/30 disabled:cursor-not-allowed disabled:opacity-50"
              >
                Compare
              </button>
              {d.truncated && <span className="text-micro text-text-3">Only the most recent runs per dataset are listed.</span>}
            </div>
          </form>
        )}
      </DataSurface>
    </Panel>
  );
}
