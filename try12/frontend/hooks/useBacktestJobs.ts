"use client";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "@/lib/http";
import { getJob, getJobs, launchJob, validateParams } from "@/lib/backtestApi";
import { queryKeys } from "@/lib/queryKeys";
import type { JobRunningDetail, LaunchRequest } from "@/types/backtest";

// Launcher job hooks (design §5.3, §6.4, §7.2). The admin key is an argument, never ambient: the
// job GETs are not issued at all without one (there can be no jobs without a key to launch them).

export const isHttpStatus = (e: unknown, status: number): boolean =>
  e instanceof ApiError && e.kind === "http" && e.status === status;

/** The running job's id from a POST /jobs 409 ({detail:{code:"JOB_RUNNING",job_id}}), keyed on the
 *  status. `null` when the error is not a 409; `""` when it is but the body could not be read. */
export function runningJobIdFrom409(e: unknown): string | null {
  if (!isHttpStatus(e, 409)) return null;
  const detail = (e as ApiError).detail;
  if (!detail) return "";
  try {
    const d = JSON.parse(detail) as Partial<JobRunningDetail>;
    return typeof d.job_id === "string" ? d.job_id : "";
  } catch {
    return "";
  }
}

/** Jobs list: 10 s while any item is RUNNING, else 60 s. */
export function useJobs(key: string | null) {
  return useQuery({
    queryKey: queryKeys.backtestJobs,
    queryFn: () => getJobs(key as string),
    enabled: !!key,
    refetchInterval: (q) => {
      if (isHttpStatus(q.state.error, 401)) return false;
      return q.state.data?.items.some((j) => j.state === "RUNNING") ? 10_000 : 60_000;
    },
  });
}

/** One job: 2 s while RUNNING, then false (every other JobState is terminal). A 404 or 401 stops
 *  polling and keeps the last known record. */
export function useJob(jobId: string | null, key: string | null) {
  return useQuery({
    queryKey: queryKeys.backtestJob(jobId ?? ""),
    queryFn: () => getJob(jobId as string, key as string),
    enabled: !!jobId && !!key,
    refetchInterval: (q) => {
      if (isHttpStatus(q.state.error, 404) || isHttpStatus(q.state.error, 401)) return false;
      return q.state.data?.state === "RUNNING" ? 2_000 : false;
    },
  });
}

export function useValidateParams() {
  return useMutation({
    mutationFn: ({ overrides, key }: { overrides: Record<string, unknown>; key: string }) =>
      validateParams(overrides, key),
  });
}

/** POST /jobs. On 202 the jobs list and launch options (running_job_id) are refreshed. */
export function useLaunchJob() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ body, key }: { body: LaunchRequest; key: string }) => launchJob(body, key),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: queryKeys.backtestJobs });
      void qc.invalidateQueries({ queryKey: queryKeys.backtestDefaults });
    },
  });
}
