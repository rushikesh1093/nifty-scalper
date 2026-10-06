"use client";
import { cn } from "@/lib/utils";
import { ApiError, errorMessage } from "@/lib/http";

// Error panel: the ApiError message, FastAPI's detail, and a Retry control (FR-1). This is the
// state that used to render as "No signals yet".
export interface ErrorStateProps {
  error:     unknown;
  onRetry?:  () => void;
  title?:    string;
  /** Single-line variant for chips / tight spaces. */
  compact?:  boolean;
  retrying?: boolean;
  className?: string;
}

export function ErrorState({ error, onRetry, title, compact, retrying, className }: ErrorStateProps) {
  const message = errorMessage(error);
  const detail = error instanceof ApiError ? error.detail : null;
  // Network failures carry the raw axios message as detail ("Network Error") — not useful to show.
  const showDetail = detail && !(error instanceof ApiError && error.kind === "network");

  // Never disabled: React Query's background retries keep a failing query "fetching" for seconds of
  // every poll, and refetch() simply restarts them.
  const retry = onRetry && (
    <span className="inline-flex items-center gap-2">
      <button
        type="button"
        onClick={onRetry}
        className="rounded-md border border-red/40 bg-red/10 px-2.5 py-1 text-table font-semibold text-red hover:bg-red/20"
      >
        Retry
      </button>
      {retrying && <span className="text-micro text-text-3">retrying…</span>}
    </span>
  );

  if (compact) {
    return (
      <div role="alert" className={cn("flex items-center gap-2 text-table", className)}>
        <span aria-hidden="true" className="text-red">●</span>
        <span className="truncate text-red">{title ? `${title}: ` : ""}{message}</span>
        {retry}
      </div>
    );
  }

  return (
    <div
      role="alert"
      className={cn("flex flex-col items-start gap-2 rounded-lg border border-red/30 bg-red/5 p-4", className)}
    >
      <div className="text-body font-semibold text-red">{title ?? "Could not load data"}</div>
      <div className="text-table text-text-1">{message}</div>
      {showDetail && <div className="break-all font-mono text-table text-text-2">{detail}</div>}
      {retry}
    </div>
  );
}
