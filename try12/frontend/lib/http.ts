import axios, { type AxiosError, type AxiosInstance } from "axios";

// ── Base URL resolution (design §6.4, E15) ──────────────────────────────────
// NEXT_PUBLIC_API_URL is inlined at build time. An invalid value falls back to the
// default and is surfaced through `envWarning` (one-time banner) instead of failing mysteriously.
const DEFAULT_API_BASE = "http://localhost:8000/api/v1";

function resolveApiBase(raw: string | undefined): { base: string; warning: string | null } {
  if (raw === undefined || raw.trim() === "") return { base: DEFAULT_API_BASE, warning: null };
  try {
    const u = new URL(raw.trim());
    if (u.protocol !== "http:" && u.protocol !== "https:") throw new Error("protocol");
    return { base: raw.trim().replace(/\/+$/, ""), warning: null };
  } catch {
    return {
      base: DEFAULT_API_BASE,
      warning: `NEXT_PUBLIC_API_URL "${raw}" is not a valid http(s) URL — using ${DEFAULT_API_BASE}`,
    };
  }
}

const resolved = resolveApiBase(process.env.NEXT_PUBLIC_API_URL);

/** Versioned API root, e.g. http://localhost:8000/api/v1 */
export const API_BASE = resolved.base;
/** App root (where /health is mounted — main.py, not under /api/v1). */
export const ROOT_BASE = API_BASE.replace(/\/api\/v1\/?$/, "");
/** Non-null when the configured env URL was rejected; render once as a banner. */
export const envWarning: string | null = resolved.warning;

// ── Normalised error ────────────────────────────────────────────────────────
export type ApiErrorKind = "network" | "timeout" | "http" | "parse";

export class ApiError extends Error {
  constructor(
    readonly kind: ApiErrorKind,
    readonly status: number | null,
    readonly detail: string | null,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export function isApiError(e: unknown): e is ApiError {
  return e instanceof ApiError;
}

/** Human-readable message for any thrown value (ApiError or otherwise). */
export function errorMessage(e: unknown): string {
  if (e instanceof Error) return e.message;
  return typeof e === "string" ? e : "Unknown error";
}

async function extractDetail(data: unknown): Promise<string | null> {
  if (data == null) return null;
  // responseType "blob" (PDF download): the JSON error body arrives as a Blob.
  if (typeof Blob !== "undefined" && data instanceof Blob) {
    try {
      return extractDetail(JSON.parse(await data.text()));
    } catch {
      return null;
    }
  }
  if (typeof data === "string") {
    const t = data.trim();
    return t ? t.slice(0, 300) : null;           // plain-text 500s (e.g. Postgres down)
  }
  if (typeof data === "object" && "detail" in (data as Record<string, unknown>)) {
    const d = (data as { detail: unknown }).detail;
    if (typeof d === "string") return d;
    try { return JSON.stringify(d).slice(0, 300); } catch { return null; }   // 422 validation arrays
  }
  return null;
}

async function toApiError(error: unknown, baseURL: string): Promise<ApiError> {
  if (error instanceof ApiError) return error;
  if (axios.isAxiosError(error)) {
    const e = error as AxiosError;
    const path = e.config?.url ?? "";
    if (e.code === "ECONNABORTED" || e.code === "ETIMEDOUT") {
      return new ApiError("timeout", null, null, `Request timed out (${baseURL}${path})`);
    }
    if (!e.response) {
      return new ApiError("network", null, e.message || null, `Backend unreachable (${baseURL})`);
    }
    const status = e.response.status;
    const detail = await extractDetail(e.response.data);
    return new ApiError("http", status, detail, `HTTP ${status} from ${path || baseURL}`);
  }
  return new ApiError("parse", null, null, errorMessage(error));
}

export function attachApiErrorInterceptor(instance: AxiosInstance): void {
  const baseURL = instance.defaults.baseURL ?? "";
  instance.interceptors.response.use(
    (r) => r,
    async (error: unknown) => Promise.reject(await toApiError(error, baseURL)),
  );
}

// ── Instances ───────────────────────────────────────────────────────────────
// Routing rule (binding): healthCheck() uses rootHttp; everything else uses http.
export const http     = axios.create({ baseURL: API_BASE,  timeout: 10_000 });
export const rootHttp = axios.create({ baseURL: ROOT_BASE, timeout:  5_000 });
attachApiErrorInterceptor(http);
attachApiErrorInterceptor(rootHttp);

// One-time client log of the resolved base URL so a misconfigured env is visible
// (module scope: evaluated once per page load).
if (typeof window !== "undefined") {
  console.info(`[api] base ${API_BASE} (root ${ROOT_BASE})`);
  if (envWarning) console.warn(`[api] ${envWarning}`);
}
