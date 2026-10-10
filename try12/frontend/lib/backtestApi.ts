import { http } from "@/lib/http";
import type {
  BinWidth, BreakdownDimension, DatasetsResponse, GroupedMetrics, JobRecord, JobsResponse,
  LaunchOptions, LaunchRequest, LaunchResponse, NoTradeSummary, RunDetail, RunMetrics, RunsQuery,
  RunsResponse, SeriesPayload, TradesPage, TradesQuery, ValidateResponse,
} from "@/types/backtest";

// Backtest API (design §5.3). All calls go through the shared `http` instance; failures arrive as
// ApiError. The admin key is passed per call as the X-API-Key header on the four launcher calls
// only — never as an axios default, so it cannot ride along on unrelated requests.
const api = http;
const run = (runId: string) => `/backtest/runs/${encodeURIComponent(runId)}`;
const ds = (dataset?: string) => (dataset ? { dataset } : undefined);
const keyed = (key: string) => ({ headers: { "X-API-Key": key } });

// ── Reads (unauthenticated) ────────────────────────────────────────────────
export const getDatasets      = () =>
  api.get<DatasetsResponse>("/backtest/datasets").then(r => r.data);
export const getRuns          = (p: RunsQuery = {}) =>
  api.get<RunsResponse>("/backtest/runs", { params: p }).then(r => r.data);
export const getRun           = (runId: string, dataset?: string) =>
  api.get<RunDetail>(run(runId), { params: ds(dataset) }).then(r => r.data);
export const getRunMetrics    = (runId: string, dataset?: string) =>
  api.get<RunMetrics>(`${run(runId)}/metrics`, { params: ds(dataset) }).then(r => r.data);
export const getRunMetricsBy  = (runId: string, dimension: BreakdownDimension, dataset?: string) =>
  api.get<GroupedMetrics>(`${run(runId)}/metrics/by/${dimension}`, { params: ds(dataset) }).then(r => r.data);
export const getRunNoTrade    = (runId: string, dataset?: string) =>
  api.get<NoTradeSummary>(`${run(runId)}/no-trade-decisions/summary`, { params: ds(dataset) }).then(r => r.data);
export const getRunTrades     = (runId: string, p: TradesQuery & { dataset?: string } = {}) =>
  api.get<TradesPage>(`${run(runId)}/trades`, { params: p }).then(r => r.data);
export const getRunSeries     = (runId: string, dataset?: string, binWidth?: BinWidth) =>
  api.get<SeriesPayload>(`${run(runId)}/series`, { params: { dataset, bin_width: binWidth } }).then(r => r.data);
export const getLaunchOptions = () =>
  api.get<LaunchOptions>("/backtest/launch-options").then(r => r.data);

// ── Launcher (X-API-Key) ───────────────────────────────────────────────────
export const validateParams   = (overrides: Record<string, unknown>, key: string) =>
  api.post<ValidateResponse>("/backtest/params/validate", { overrides }, keyed(key)).then(r => r.data);
export const launchJob        = (body: LaunchRequest, key: string) =>
  api.post<LaunchResponse>("/backtest/jobs", body, keyed(key)).then(r => r.data);
export const getJobs          = (key: string) =>
  api.get<JobsResponse>("/backtest/jobs", keyed(key)).then(r => r.data);
export const getJob           = (jobId: string, key: string) =>
  api.get<JobRecord>(`/backtest/jobs/${encodeURIComponent(jobId)}`, keyed(key)).then(r => r.data);
