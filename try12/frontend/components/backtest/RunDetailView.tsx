"use client";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useRun, useRunMetrics } from "@/hooks/useBacktest";
import { isHttpStatus } from "@/hooks/useBacktestJobs";
import { formatIst } from "@/lib/time";
import { Badge } from "@/components/ui/Badge";
import { DataSurface } from "@/components/ui/DataSurface";
import { EmptyState } from "@/components/ui/EmptyState";
import { ErrorState } from "@/components/ui/ErrorState";
import { PageHeader } from "@/components/ui/PageHeader";
import { Skeleton } from "@/components/ui/Skeleton";
import { VerdictHeader } from "./VerdictHeader";
import { CriteriaScorecard } from "./CriteriaScorecard";
import { HeadlineMetrics } from "./HeadlineMetrics";
import { EquityCurve } from "./EquityCurve";
import { ExitReasonTable } from "./ExitReasonTable";
import { RHistogram } from "./RHistogram";
import { BreakdownTable } from "./BreakdownTable";
import { NoTradeFunnel } from "./NoTradeFunnel";
import { CoveragePanel, skipReasonTotals } from "./CoveragePanel";
import { RunConfigPanel } from "./RunConfigPanel";
import { TradeBlotter } from "./TradeBlotter";
import { STATUS_INTENT } from "./format";
import type { RunDetail } from "@/types/backtest";

// /backtest/[runId] client content (design §5.6, §6.2, §7.2). Rendered inside <Suspense> by the
// page shell because it reads ?dataset=. After the detail loads every sub-query uses the dataset
// the server resolved, and the per-trade queries are only issued when per_trade_available.

const BACK = (
  <Link href="/backtest" className="text-table text-text-2 underline decoration-border-2 underline-offset-2 hover:text-text-1">
    ← Back to runs
  </Link>
);

const WARNING_TEXT: Record<string, string> = {
  validation_from_artifact: "The replay store holds no validation for this run; the verdict is read from its report artefact.",
  params_defaults_used: "The run's frozen params could not be rebuilt by this code version; derived figures use defaults.",
};

function RunNotices({ run }: { run: RunDetail }) {
  const skips = skipReasonTotals(run);
  return (
    <>
      {run.sessions === 0 && (
        <div role="status" className="rounded-lg border border-yellow/40 bg-yellow/10 px-4 py-3">
          <div className="text-body font-semibold text-yellow">0 tradeable days in this window; no trade was possible</div>
          {skips.length > 0 && (
            <div className="mt-1 flex flex-wrap items-center gap-1.5 text-table text-text-2">
              <span>Skip reasons (summed over underlyings):</span>
              {skips.map(([code, n]) => (
                <Badge key={code} intent="warning" strength="outline" caps={false} status={code}>{code} {n}</Badge>
              ))}
            </div>
          )}
        </div>
      )}
      {run.status === "RUNNING" && (
        <div role="status" className="rounded-lg border border-blue/40 bg-blue/10 px-4 py-3 text-table text-text-1">
          This run is still in progress or was interrupted. The page refreshes every 10 s while it reads RUNNING.
        </div>
      )}
      {run.status === "FAILED" && (
        <div role="status" className="rounded-lg border border-red/40 bg-red/10 px-4 py-3 text-table text-text-1">
          <div className="font-semibold text-red">This run failed before it produced a complete report.</div>
          {run.notes ? (
            <details className="mt-1">
              <summary className="cursor-pointer text-text-2 hover:text-text-1">Stored traceback tail</summary>
              <pre className="mt-1 max-h-[300px] overflow-auto whitespace-pre-wrap font-mono text-micro text-text-2">{run.notes}</pre>
            </details>
          ) : (
            <div className="mt-1 text-text-2">No traceback was stored with it.</div>
          )}
        </div>
      )}
      {run.warnings.filter((w) => w !== "params_defaults_used").length > 0 && (
        <ul role="note" className="space-y-0.5 rounded-md border border-yellow/30 bg-yellow/10 px-3 py-1.5 text-table text-yellow">
          {run.warnings.filter((w) => w !== "params_defaults_used").map((w) => <li key={w}>{WARNING_TEXT[w] ?? w}</li>)}
        </ul>
      )}
    </>
  );
}

function Detail({ run }: { run: RunDetail }) {
  const metrics = useRunMetrics(run.run_id, run.dataset, run.metrics_available);
  // trades === 0 with tradeable days: the funnel is the answer to "why nothing traded", so it leads
  const promoteFunnel = run.sessions > 0 && metrics.data?.metrics.trades === 0;

  return (
    <div className="space-y-4">
      <PageHeader
        title={`Run ${run.run_id}`}
        description={
          <span className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <span>{run.label || "no label"}</span>
            <Badge intent={run.dataset === "artifacts" ? "neutral" : "accent"} strength={run.dataset === "artifacts" ? "outline" : "subtle"}
              caps={false} className="font-mono text-micro">{run.dataset}</Badge>
            <span className="font-mono">{run.date_from ?? "—"} → {run.date_to ?? "—"}</span>
            <span>{run.underlyings?.join(", ") || "—"}</span>
            <Badge intent={STATUS_INTENT[run.status] ?? "neutral"} status={run.status} dot={run.status === "RUNNING" ? "pulse" : undefined}>
              {run.status}
            </Badge>
            <span title={run.created_at_source === "file_mtime" ? "artefact file modification time" : undefined}>
              created {formatIst(run.created_at, { withDate: true, seconds: false })}{run.created_at_source === "file_mtime" ? " (file time)" : ""}
            </span>
          </span>
        }
        actions={BACK}
      />

      <VerdictHeader run={run} lead={<RunNotices run={run} />} />
      <CriteriaScorecard run={run} />
      {promoteFunnel && <NoTradeFunnel run={run} promoted />}
      <HeadlineMetrics run={run} query={metrics} />
      <EquityCurve run={run} />
      <div className="grid grid-cols-1 items-start gap-4 xl:grid-cols-2">
        <ExitReasonTable run={run} />
        <RHistogram run={run} />
      </div>
      <BreakdownTable run={run} />
      {!promoteFunnel && <NoTradeFunnel run={run} />}
      <CoveragePanel run={run} />
      <RunConfigPanel run={run} />
      <TradeBlotter run={run} />
    </div>
  );
}

export function RunDetailView({ runId }: { runId: string }) {
  const searchParams = useSearchParams();
  const dataset = searchParams.get("dataset") || undefined;
  const run = useRun(runId, dataset);

  if (isHttpStatus(run.error, 404) && !run.data) {
    return (
      <div className="mx-auto max-w-[2400px] space-y-4 p-4">
        <PageHeader title={`Run ${runId}`} actions={BACK} />
        <div className="panel">
          <EmptyState
            title="Run not found in any dataset"
            hint={dataset ? `No run '${runId}' in dataset '${dataset}'.` : `No replay store or report artefact holds '${runId}'.`}
          />
          <div className="flex justify-center pb-6">{BACK}</div>
        </div>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-[2400px] space-y-4 p-4">
      {isHttpStatus(run.error, 503) && !run.data ? (
        <>
          <PageHeader title={`Run ${runId}`} actions={BACK} />
          <ErrorState
            error={run.error}
            title="Replay store unavailable"
            onRetry={() => { void run.refetch(); }}
            retrying={run.isFetching}
          />
          <p className="text-table text-text-3">
            If a backtest job is running it may be writing this dataset — retry when it finishes.
          </p>
        </>
      ) : (
        <DataSurface query={run} skeleton={<Skeleton label="Loading run" />} hideFreshness>
          {(d) => <Detail run={d} />}
        </DataSurface>
      )}
    </div>
  );
}
