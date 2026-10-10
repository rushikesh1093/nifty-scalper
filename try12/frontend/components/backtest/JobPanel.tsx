"use client";
import { forwardRef, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useQueryClient } from "@tanstack/react-query";
import { cn } from "@/lib/utils";
import { formatIst } from "@/lib/time";
import { queryKeys } from "@/lib/queryKeys";
import { useNow } from "@/hooks/useNow";
import { isHttpStatus, useJob, useJobs } from "@/hooks/useBacktestJobs";
import { Panel } from "@/components/ui/Panel";
import { DataSurface } from "@/components/ui/DataSurface";
import { TableSkeleton } from "@/components/ui/Skeleton";
import { ErrorState } from "@/components/ui/ErrorState";
import { Badge } from "@/components/ui/Badge";
import { JOB_STATE_INTENT, runHref } from "./format";
import type { JobRecord, JobState, LaunchLimits } from "@/types/backtest";

// Launcher jobs (design §5.4, §6.4, §7.2): the active job card (state chip, step, elapsed, redacted
// tails, link to the produced run) and the recent jobs list. The single job polls at 2 s only while
// RUNNING; tails are a role="log" with aria-live="off", and one role="status" line announces state
// and step transitions only.

const STATE_NOTE: Partial<Record<JobState, string>> = {
  CANCELLED: "The API restarted while this job was running.",
  TIMEOUT:   "Stopped at the REPLAY_JOB_TIMEOUT_S wall clock — the run row may be left at RUNNING.",
  NO_DATA:   "The window had no tradeable day; this is an expected outcome, not a failure.",
  INVALID:   "The replay rejected its arguments or parameters; see the stderr tail.",
  REFUSED:   "The replay target was refused before anything was spawned.",
};

function elapsedSeconds(job: JobRecord, now: Date | null): number | null {
  if (job.duration_s !== null) return job.duration_s;
  const start = Date.parse(job.started_at ?? job.created_at);
  if (!now || Number.isNaN(start)) return null;
  return Math.max(0, (now.getTime() - start) / 1000);
}

export function JobStateChip({ state }: { state: JobState }) {
  return (
    <Badge intent={JOB_STATE_INTENT[state] ?? "negative"} status={state} dot={state === "RUNNING" ? "pulse" : undefined}>
      {state.replace("_", " ")}
    </Badge>
  );
}

function Tail({ label, lines, preRef }: { label: string; lines: string[]; preRef: React.Ref<HTMLPreElement> }) {
  return (
    <div className="min-w-0">
      <div className="mb-1 text-micro font-semibold uppercase text-text-3">{label}</div>
      <pre
        ref={preRef}
        role="log"
        aria-live="off"
        aria-label={`${label} tail`}
        tabIndex={0}
        className="max-h-48 overflow-auto whitespace-pre-wrap break-all rounded-md border border-border bg-bg p-2 font-mono text-micro text-text-2"
      >
        {lines.length ? lines.join("\n") : "(empty)"}
      </pre>
    </div>
  );
}

const JobCard = forwardRef<HTMLDivElement, { job: JobRecord; untracked: boolean }>(function JobCard({ job, untracked }, ref) {
  const now = useNow(1_000);
  const elapsed = elapsedSeconds(job, job.state === "RUNNING" ? now : null);
  const outRef = useRef<HTMLPreElement>(null);
  const errRef = useRef<HTMLPreElement>(null);

  // Announce only state / step transitions, never every elapsed tick.
  const [announce, setAnnounce] = useState("");
  const transition = `${job.state}|${job.step ?? ""}`;
  const lastTransition = useRef<string | null>(null);
  useEffect(() => {
    if (lastTransition.current === transition) return;
    lastTransition.current = transition;
    const secs = elapsedSeconds(job, new Date());
    setAnnounce(
      `${job.step ?? "job"} step ${job.state === "RUNNING" ? "running" : job.state.toLowerCase().replace("_", " ")}` +
      (secs !== null ? `, ${Math.round(secs)} s elapsed` : ""),
    );
  }, [transition, job]);

  const jumpToLatest = () => {
    for (const el of [outRef.current, errRef.current]) if (el) el.scrollTop = el.scrollHeight;
  };

  const finished = job.state === "SUCCEEDED" || job.state === "NO_DATA";
  return (
    <div ref={ref} tabIndex={-1} data-job-id={job.job_id} className="space-y-3 rounded-lg border border-border-2 bg-surface-2 p-3 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-table text-text-1">{job.job_id}</span>
        <JobStateChip state={job.state} />
        {job.step && <Badge intent="neutral" strength="outline" caps={false}>{job.step} step</Badge>}
        <span className="font-mono text-table text-text-2">
          {elapsed === null ? "—" : `${Math.round(elapsed)} s`}{job.state === "RUNNING" ? " elapsed" : ""}
        </span>
        <span className="ml-auto text-micro text-text-3">steps: {job.steps.join(" → ")}</span>
      </div>
      <p role="status" className="sr-only">{announce}</p>

      <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-table sm:grid-cols-4">
        <div><dt className="text-micro text-text-3">Dataset</dt><dd className="font-mono text-text-1">{job.dataset}{job.dataset_reused ? " (reused)" : ""}</dd></div>
        <div><dt className="text-micro text-text-3">Window</dt><dd className="font-mono text-text-1">{job.date_from} → {job.date_to}</dd></div>
        <div><dt className="text-micro text-text-3">Label</dt><dd className="truncate text-text-1" title={job.label}>{job.label || "—"}</dd></div>
        <div><dt className="text-micro text-text-3">Created</dt><dd className="font-mono text-text-1">{formatIst(job.created_at, { withDate: true, seconds: false })}</dd></div>
      </dl>

      {untracked && (
        <p role="alert" className="text-table text-yellow">This job is no longer tracked; showing the last known record.</p>
      )}
      {STATE_NOTE[job.state] && <p className="text-table text-text-2">{STATE_NOTE[job.state]}</p>}
      {job.error && <p className="font-mono text-table text-red">{job.error}</p>}
      {job.warnings.includes("overlapping_prior_runs") && (
        <p role="note" className="rounded-md border border-yellow/30 bg-yellow/10 px-3 py-1.5 text-table text-yellow">
          {job.prior_overlapping_runs} prior run{job.prior_overlapping_runs === 1 ? "" : "s"} over an overlapping window used
          different parameters and the notes are empty — the report will cap this run &ldquo;undeclared parameter search&rdquo;.
        </p>
      )}

      {finished && (
        job.run_id ? (
          <Link href={runHref(job.run_id, job.dataset)} className="inline-block text-table font-semibold text-accent underline underline-offset-2">
            Open run {job.run_id}
          </Link>
        ) : (
          <Link href="/backtest" className="inline-block text-table font-semibold text-accent underline underline-offset-2">
            Run id not reported — see the runs list
          </Link>
        )
      )}

      <div className="flex items-center justify-between">
        <span className="text-micro text-text-3">Output tails (paths and URLs are redacted by the API)</span>
        <button type="button" onClick={jumpToLatest} className="rounded border border-border-2 px-2 py-0.5 text-micro text-text-2 hover:bg-surface-3">
          Jump to latest
        </button>
      </div>
      <div className="grid gap-2 md:grid-cols-2">
        <Tail label="stdout" lines={job.stdout_tail} preRef={outRef} />
        <Tail label="stderr" lines={job.stderr_tail} preRef={errRef} />
      </div>
    </div>
  );
});

export interface JobPanelProps {
  adminKey:        string | null;
  activeJobId:     string | null;
  onSelectJob:     (id: string) => void;
  onUnauthorized:  () => void;
  onRequestKey:    () => void;
  limits?:         LaunchLimits;
  cardRef?:        React.Ref<HTMLDivElement>;
}

export function JobPanel({ adminKey, activeJobId, onSelectJob, onUnauthorized, onRequestKey, limits, cardRef }: JobPanelProps) {
  const qc = useQueryClient();
  const jobs = useJobs(adminKey);
  const fallbackId = activeJobId ?? jobs.data?.running_job_id ?? jobs.data?.items[0]?.job_id ?? null;
  const job = useJob(fallbackId, adminKey);

  // 401 on either query → clear the key and reopen the dialog (the caller does both).
  const unauthorized = isHttpStatus(jobs.error, 401) || isHttpStatus(job.error, 401);
  useEffect(() => { if (unauthorized) onUnauthorized(); }, [unauthorized, onUnauthorized]);

  const current: JobRecord | undefined = job.data ?? jobs.data?.items.find((j) => j.job_id === fallbackId);

  // A job leaving RUNNING produced (or failed to produce) a run: refresh every backtest read.
  const prevState = useRef<JobState | null>(null);
  useEffect(() => {
    const s = current?.state ?? null;
    if (prevState.current === "RUNNING" && s !== null && s !== "RUNNING") {
      void qc.invalidateQueries({ queryKey: queryKeys.backtestAll });
    }
    prevState.current = s;
  }, [current?.state, qc]);

  const note = (
    <p className="text-micro text-text-3">
      A job cannot be cancelled in v1; it stops at the {limits ? `${limits.job_timeout_s} s` : "REPLAY_JOB_TIMEOUT_S"} wall
      clock, which is why the launch window is capped at {limits ? `${limits.max_launch_range_days} days` : "max_launch_range_days"}.
    </p>
  );

  if (!adminKey) {
    return (
      <Panel title="Launcher jobs" footer={note}>
        <div className="flex flex-wrap items-center gap-3 text-table text-text-2">
          <span>Job status needs the admin key.</span>
          <button type="button" onClick={onRequestKey} className="rounded-md border border-border-2 bg-surface-3 px-2.5 py-1 text-table font-semibold text-text-1 hover:bg-surface-2">
            Enter admin key
          </button>
        </div>
      </Panel>
    );
  }

  return (
    <Panel title="Launcher jobs" meta={jobs.data ? <span className="text-micro text-text-3">{jobs.data.total}</span> : undefined} footer={note}>
      <DataSurface
        query={jobs}
        skeleton={<TableSkeleton rows={3} cols={5} />}
        isEmpty={(d) => d.items.length === 0 && !current}
        emptyTitle="No launcher jobs yet"
        emptyHint="Jobs you launch from the form appear here with their live output."
      >
        {(d) => (
          <div className="space-y-3">
            {current && <JobCard ref={cardRef} job={current} untracked={isHttpStatus(job.error, 404)} />}
            {job.isError && !isHttpStatus(job.error, 404) && !isHttpStatus(job.error, 401) && (
              <ErrorState error={job.error} compact title="Job status" onRetry={() => { void job.refetch(); }} />
            )}
            {d.items.length > 0 && (
              <div className="overflow-x-auto">
                <table className="data-table">
                  <caption className="sr-only">Recent launcher jobs, newest first</caption>
                  <thead>
                    <tr>
                      <th scope="col">Job</th>
                      <th scope="col">State</th>
                      <th scope="col">Dataset</th>
                      <th scope="col">Label</th>
                      <th scope="col">Created</th>
                      <th scope="col">Run</th>
                    </tr>
                  </thead>
                  <tbody>
                    {d.items.map((j) => (
                      <tr key={j.job_id} className={cn(j.job_id === current?.job_id && "bg-surface-2")}>
                        <td>
                          <button
                            type="button"
                            onClick={() => onSelectJob(j.job_id)}
                            aria-pressed={j.job_id === current?.job_id}
                            className="font-mono text-text-1 underline decoration-border-2 underline-offset-2 hover:decoration-accent"
                          >
                            {j.job_id}
                          </button>
                        </td>
                        <td><JobStateChip state={j.state} /></td>
                        <td className="font-mono">{j.dataset}</td>
                        <td className="max-w-[180px] truncate" title={j.label}>{j.label || "—"}</td>
                        <td className="whitespace-nowrap font-mono">{formatIst(j.created_at, { withDate: true, seconds: false })}</td>
                        <td>
                          {j.run_id ? (
                            <Link href={runHref(j.run_id, j.dataset)} className="font-mono text-accent underline underline-offset-2">
                              {j.run_id.slice(0, 12)}…
                            </Link>
                          ) : "—"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}
      </DataSurface>
    </Panel>
  );
}
