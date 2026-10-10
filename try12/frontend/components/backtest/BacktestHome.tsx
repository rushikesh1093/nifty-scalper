"use client";
import { useCallback, useRef, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useQueryClient } from "@tanstack/react-query";
import toast from "react-hot-toast";
import { queryKeys } from "@/lib/queryKeys";
import { DATASET_ID_RE } from "@/lib/backtestMath";
import { useAdminKey } from "@/hooks/useAdminKey";
import { useDatasets, useLaunchOptions, useRuns } from "@/hooks/useBacktest";
import { isHttpStatus } from "@/hooks/useBacktestJobs";
import { PageHeader } from "@/components/ui/PageHeader";
import { Panel } from "@/components/ui/Panel";
import { DataSurface } from "@/components/ui/DataSurface";
import { Skeleton, TableSkeleton } from "@/components/ui/Skeleton";
import { AdminKeyDialog } from "@/components/ui/AdminKeyDialog";
import { RunsTable } from "./RunsTable";
import { LaunchForm } from "./LaunchForm";
import { JobPanel } from "./JobPanel";
import type { DatasetsResponse, RunsResponse } from "@/types/backtest";

// /backtest client content (design §5.2, §5.6, §6.1, §6.4): the runs list, the launcher and the job
// panel. Rendered inside <Suspense> by app/backtest/page.tsx because it reads ?dataset=.

const LIMIT = 50;

function warningText(w: string): string {
  if (w === "artifacts_unavailable") return "The report artefact directory is unavailable — artefact-tier runs are not listed.";
  if (w.startsWith("store_unreachable:")) return `Replay store '${w.slice("store_unreachable:".length)}' is unreachable — its runs are not listed.`;
  return w;
}

function emptyCopy(runs: RunsResponse, datasets: DatasetsResponse | undefined): { title: string; hint?: string } {
  if (runs.empty_reason === "replay_store_not_initialised") {
    return { title: "The configured replay database has not been generated yet — launch with a synthetic dataset first" };
  }
  const noStore = datasets !== undefined && !datasets.replay_store_configured
    && !datasets.items.some((d) => d.kind === "synthetic" || d.kind === "artifacts");
  if (noStore) {
    return {
      title: "No replay store configured",
      hint: "Set REPLAY_DATABASE_URL, or launch a backtest with a generated synthetic dataset.",
    };
  }
  return {
    title: "No backtest runs yet",
    hint: "Launch one below, or point REPLAY_DATABASE_URL at an existing replay database.",
  };
}

export function BacktestHome() {
  const qc = useQueryClient();
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const raw = searchParams.get("dataset");
  const dataset = raw && DATASET_ID_RE.test(raw) ? raw : undefined;
  const [offset, setOffset] = useState(0);

  const { key, setKey, clear } = useAdminKey();
  const [dialog, setDialog] = useState<{ open: boolean; message?: string }>({ open: false });
  const [activeJobId, setActiveJobId] = useState<string | null>(null);
  const jobCardRef = useRef<HTMLDivElement>(null);

  const options = useLaunchOptions();
  const datasets = useDatasets();
  const jobActive = !!options.data?.running_job_id;
  const runs = useRuns({ dataset, limit: LIMIT, offset }, { jobActive });

  const onUnauthorized = useCallback(() => {
    clear();
    toast.error("Admin key rejected");
    setDialog({ open: true, message: "Admin key rejected — enter it again." });
  }, [clear]);

  const onSaveKey = (k: string) => {
    setKey(k);
    // a fresh key starts the job queries from a clean state, so a second 401 is noticed again
    void qc.resetQueries({ queryKey: queryKeys.backtestJobs });
    setDialog({ open: false });
  };

  const focusJobCard = () => requestAnimationFrame(() => jobCardRef.current?.focus());

  const setDataset = (v: string) => {
    setOffset(0);
    const qs = new URLSearchParams(searchParams.toString());
    if (v) qs.set("dataset", v);
    else qs.delete("dataset");
    const s = qs.toString();
    router.replace(s ? `${pathname}?${s}` : pathname, { scroll: false });
  };

  const total = runs.data?.total ?? 0;
  const pager = runs.data && total > LIMIT && (
    <div className="flex items-center gap-2">
      <span>Showing {offset + 1}–{Math.min(offset + LIMIT, total)} of {total}</span>
      <button type="button" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - LIMIT))}
        className="rounded border border-border-2 px-2 py-0.5 text-text-2 hover:bg-surface-3 disabled:opacity-50">
        Previous
      </button>
      <button type="button" disabled={offset + LIMIT >= total} onClick={() => setOffset(offset + LIMIT)}
        className="rounded border border-border-2 px-2 py-0.5 text-text-2 hover:bg-surface-3 disabled:opacity-50">
        Next
      </button>
    </div>
  );

  const datasetFilter = (
    <div className="flex items-center gap-2">
      <label htmlFor="bt-dataset-filter" className="text-micro font-semibold text-text-3">Dataset</label>
      <select
        id="bt-dataset-filter"
        value={dataset ?? ""}
        onChange={(e) => setDataset(e.target.value)}
        className="rounded-md border border-border-2 bg-surface-2 px-2 py-1 text-table text-text-1"
      >
        <option value="">All datasets</option>
        {datasets.data?.items.map((d) => (
          <option key={d.dataset_id} value={d.dataset_id}>
            {d.label}{d.runs !== null ? ` (${d.runs})` : d.error ? " (unreachable)" : ""}
          </option>
        ))}
        {dataset && !datasets.data?.items.some((d) => d.dataset_id === dataset) && <option value={dataset}>{dataset}</option>}
      </select>
    </div>
  );

  const emptyInfo = runs.data ? emptyCopy(runs.data, datasets.data) : { title: "No backtest runs yet" };

  return (
    <div className="mx-auto max-w-[2400px] space-y-4 p-4">
      <PageHeader
        title="Backtests"
        description="Replay runs of the strategy engine. Every run states its provenance: SYNTHETIC runs are generated data and not evidence of edge; UNVERIFIED runs have no recorded provenance."
      />

      <Panel
        title="Runs"
        meta={runs.data ? <span className="text-micro text-text-3">{runs.data.total}</span> : undefined}
        actions={datasetFilter}
        footer={pager || undefined}
        flush
      >
        {runs.isError && jobActive && isHttpStatus(runs.error, 503) && (
          <p className="px-4 pt-3 text-table text-yellow">
            This dataset is being written by a running job — retry when it finishes.
          </p>
        )}
        {runs.data && runs.data.warnings.length > 0 && (
          <ul role="status" className="mx-4 mt-3 space-y-0.5 rounded-md border border-yellow/30 bg-yellow/10 px-3 py-1.5 text-table text-yellow">
            {runs.data.warnings.map((w) => <li key={w}>{warningText(w)}</li>)}
          </ul>
        )}
        <DataSurface
          query={runs}
          skeleton={<div className="p-4"><TableSkeleton rows={6} cols={10} /></div>}
          isEmpty={(d) => d.items.length === 0}
          emptyTitle={emptyInfo.title}
          emptyHint={emptyInfo.hint}
          className="pb-2"
        >
          {(d) => <RunsTable data={d} />}
        </DataSurface>
      </Panel>

      <div className="grid grid-cols-1 items-start gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        <Panel title="Launch a backtest">
          <DataSurface query={options} skeleton={<Skeleton label="Loading launch options" />} hideFreshness>
            {(o) => (
              <LaunchForm
                options={o}
                adminKey={key}
                onRequestKey={() => setDialog({ open: true })}
                onUnauthorized={onUnauthorized}
                onLaunched={(id) => { setActiveJobId(id); focusJobCard(); }}
                onJobRunning={(id) => { if (id) setActiveJobId(id); focusJobCard(); }}
              />
            )}
          </DataSurface>
        </Panel>

        <JobPanel
          adminKey={key}
          activeJobId={activeJobId}
          onSelectJob={setActiveJobId}
          onUnauthorized={onUnauthorized}
          onRequestKey={() => setDialog({ open: true })}
          limits={options.data?.limits}
          cardRef={jobCardRef}
        />
      </div>

      <AdminKeyDialog
        open={dialog.open}
        message={dialog.message}
        onSave={onSaveKey}
        onCancel={() => setDialog({ open: false })}
      />
    </div>
  );
}
