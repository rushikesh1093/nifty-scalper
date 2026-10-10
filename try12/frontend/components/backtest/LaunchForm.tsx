"use client";
import { useRef, useState } from "react";
import toast from "react-hot-toast";
import { useQueryClient } from "@tanstack/react-query";
import { addBusinessDays, addDays, format, parseISO, startOfWeek, subWeeks } from "date-fns";
import { ApiError, errorMessage } from "@/lib/http";
import { queryKeys } from "@/lib/queryKeys";
import { windowDays } from "@/lib/backtestMath";
import { cn } from "@/lib/utils";
import { isHttpStatus, runningJobIdFrom409, useLaunchJob, useValidateParams } from "@/hooks/useBacktestJobs";
import type { LaunchOptions, LaunchRequest, OverrideDefaults, ValidateResponse } from "@/types/backtest";

// The launcher form (design §5.5, §6.4, §8.2). Client validation mirrors the server's bounds and is
// never the only check: on submit the overrides go to POST /params/validate, whose violations are
// mapped to fields by key name and listed in a role="alert" summary; a clean result POSTs /jobs.
// Inputs are kept as the strings the user typed — nothing is trimmed or coerced silently.

const SEED_MAX = 2 ** 31 - 1;
const START_MIN = "2000-01-01";
const START_MAX = "2100-01-01";
const LABEL_MAX = 120;
const NOTES_MAX = 2000;
const SYNTHETIC_ID_RE = /^synthetic-(\d{1,3})d-s(\d{1,10})-(\d{4}-\d{2}-\d{2})$/;

type ScalarKey = Exclude<keyof OverrideDefaults, "max_hold_minutes" | "partial_exit_enabled">;

interface ScalarMeta { label: string; int?: boolean; min?: number; max?: number; step?: string }

// Bounds are the ones TradingParams.validate() states; everything else is left to /params/validate.
const SCALARS: Record<ScalarKey, ScalarMeta> = {
  capital:                     { label: "Capital (₹)", step: "any" },
  fixed_fraction_pct:          { label: "Fixed fraction (% of capital)", min: 0, max: 100, step: "any" },
  kelly_fraction_cap_pct:      { label: "Kelly fraction cap (%)", min: 0, max: 25, step: "any" },
  max_lots_per_trade:          { label: "Max lots per trade", int: true, step: "1" },
  max_open_positions:          { label: "Max open positions", int: true, step: "1" },
  max_daily_loss:              { label: "Max daily loss (₹)", step: "any" },
  max_daily_loss_pct:          { label: "Max daily loss (% of capital)", min: 0, max: 100, step: "any" },
  max_trades_per_day:          { label: "Max trades per day", int: true, step: "1" },
  open_exposure_cap_pct:       { label: "Open exposure cap (%)", step: "any" },
  transition_cooldown_candles: { label: "Transition cooldown (candles)", int: true, min: 1, max: 8, step: "1" },
};
// Field order of §6.4 (the knobs either side of partial_exit_enabled).
const SCALARS_BEFORE: ScalarKey[] = ["capital", "fixed_fraction_pct", "kelly_fraction_cap_pct", "max_lots_per_trade"];
const SCALARS_AFTER: ScalarKey[] = [
  "max_open_positions", "max_daily_loss", "max_daily_loss_pct", "max_trades_per_day", "open_exposure_cap_pct",
];
const OVERRIDE_KEYS: string[] = [
  ...SCALARS_BEFORE, "partial_exit_enabled", ...SCALARS_AFTER, "max_hold_minutes", "transition_cooldown_candles",
];
const MAX_HOLD_MAX = 374;

const fieldId = (k: string) => `bt-${k.replace(/[^A-Za-z0-9_-]/g, "-")}`;
const isoDay = (d: Date) => format(d, "yyyy-MM-dd");
const hasControl = (s: string, allowNewline = false) =>
  Array.from(s).some((c) => {
    const code = c.charCodeAt(0);
    if (allowNewline && c === "\n") return false;
    return code < 0x20 || (code >= 0x7f && code <= 0x9f);
  });

interface FormState {
  dataset:      string;                 // "replay-db" | "synthetic"
  date_from:    string;
  date_to:      string;
  underlyings:  string[];
  synth_days:   string;
  synth_start:  string;
  synth_seed:   string;
  seed:         string;
  oos_split:    string;
  bootstrap:    string;
  label:        string;
  notes:        string;
  scalars:      Record<ScalarKey, string>;
  partial_exit: boolean;
  max_hold:     Record<string, string>;
}

type Errors = Record<string, string[]>;

function overrideState(d: OverrideDefaults): Pick<FormState, "scalars" | "partial_exit" | "max_hold"> {
  const scalars = {} as Record<ScalarKey, string>;
  for (const k of Object.keys(SCALARS) as ScalarKey[]) scalars[k] = String(d[k]);
  const max_hold: Record<string, string> = {};
  for (const [k, v] of Object.entries(d.max_hold_minutes)) max_hold[k] = String(v);
  return { scalars, partial_exit: d.partial_exit_enabled, max_hold };
}

function synthDefaults(o: LaunchOptions): { days: number; seed: number; start: string } {
  const alias = o.datasets.find((d) => d.kind === "replay_db")?.alias_of;
  const m = alias ? SYNTHETIC_ID_RE.exec(alias) : null;
  if (m) return { days: Number(m[1]), seed: Number(m[2]), start: m[3] };
  const days = Math.min(Math.max(25, o.limits.synthetic_min_days), o.limits.synthetic_max_days);
  // A Monday eight weeks back: always a weekday, and the generated span stays in the past.
  const start = isoDay(startOfWeek(subWeeks(new Date(), 8), { weekStartsOn: 1 }));
  return { days, seed: o.defaults.seed, start };
}

/** The calendar span a synthetic dataset of `days` weekdays from `start` covers. */
function synthSpan(start: string, days: number): { from: string; to: string } | null {
  const s = parseISO(start);
  if (Number.isNaN(s.getTime()) || !Number.isInteger(days) || days < 1) return null;
  return { from: start, to: isoDay(addBusinessDays(s, days - 1)) };
}

function initialState(o: LaunchOptions): FormState {
  const replay = o.datasets.find((d) => d.kind === "replay_db");
  const syn = synthDefaults(o);
  const span = synthSpan(syn.start, syn.days) ?? { from: syn.start, to: syn.start };
  return {
    dataset: replay && replay.launchable ? replay.dataset_id : "synthetic",
    date_from: span.from,
    date_to: span.to,
    underlyings: o.underlyings.slice(0, 1),
    synth_days: String(syn.days),
    synth_start: syn.start,
    synth_seed: String(syn.seed),
    seed: String(o.defaults.seed),
    oos_split: String(o.limits.oos_split.default),
    bootstrap: String(o.limits.bootstrap_iterations.default),
    label: "",
    notes: "",
    ...overrideState(o.defaults.overrides),
  };
}

/** Number parse that refuses "", whitespace and partial input instead of coercing it. */
function parseNum(s: string, int = false): number | null {
  if (s.trim() === "" || s !== s.trim()) return null;
  const n = Number(s);
  if (!Number.isFinite(n)) return null;
  if (int && !Number.isInteger(n)) return null;
  return n;
}

function validateClient(f: FormState, o: LaunchOptions): Errors {
  const e: Errors = {};
  const add = (k: string, msg: string) => { (e[k] ??= []).push(msg); };
  const limit = Math.min(o.limits.max_range_days, o.limits.max_launch_range_days);

  if (!f.date_from) add("date_from", "date_from is required");
  if (!f.date_to) add("date_to", "date_to is required");
  if (f.date_from && f.date_to) {
    const span = windowDays(f.date_from, f.date_to);
    if (span === null) add("date_from", "dates must be valid ISO dates");
    else if (span < 0) add("date_to", "date_from must be <= date_to");
    else if (span > limit) add("date_to", `date range must be <= ${limit} days`);
  }
  if (f.underlyings.length === 0) add("underlyings", `underlyings must be a non-empty subset of ${o.underlyings.join(", ")}`);

  if (f.dataset === "synthetic") {
    const days = parseNum(f.synth_days, true);
    if (days === null || days < o.limits.synthetic_min_days || days > o.limits.synthetic_max_days) {
      add("dataset.days", `dataset.days must be a whole number between ${o.limits.synthetic_min_days} and ${o.limits.synthetic_max_days}`);
    }
    const seed = parseNum(f.synth_seed, true);
    if (seed === null || seed < 0 || seed > SEED_MAX) add("dataset.seed", `dataset.seed must be between 0 and ${SEED_MAX}`);
    const start = parseISO(f.synth_start);
    const wd = start.getDay();
    if (!f.synth_start || Number.isNaN(start.getTime()) || f.synth_start < START_MIN || f.synth_start > START_MAX || wd === 0 || wd === 6) {
      add("dataset.start", `dataset.start must be a weekday between ${START_MIN} and ${START_MAX}`);
    }
  }

  const seed = parseNum(f.seed, true);
  if (seed === null || seed < 0 || seed > SEED_MAX) add("seed", `seed must be between 0 and ${SEED_MAX}`);
  const oos = parseNum(f.oos_split);
  if (oos === null || oos < o.limits.oos_split.min || oos > o.limits.oos_split.max) {
    add("oos_split", `oos_split must be between ${o.limits.oos_split.min} and ${o.limits.oos_split.max}`);
  }
  const boot = parseNum(f.bootstrap, true);
  const bl = o.limits.bootstrap_iterations;
  if (boot === null || boot < bl.min || boot > bl.max) add("bootstrap_iterations", `bootstrap_iterations must be a whole number between ${bl.min} and ${bl.max}`);
  if (f.label.length > LABEL_MAX || hasControl(f.label)) add("label", `label must be at most ${LABEL_MAX} characters with no control characters`);
  if (f.notes.length > NOTES_MAX || hasControl(f.notes, true)) add("notes", `notes must be at most ${NOTES_MAX} characters with no control characters other than newline`);

  const nums: Partial<Record<ScalarKey, number>> = {};
  for (const k of Object.keys(SCALARS) as ScalarKey[]) {
    const m = SCALARS[k];
    const v = parseNum(f.scalars[k], m.int);
    if (v === null) { add(k, `${k} must be ${m.int ? "a whole number" : "a number"}`); continue; }
    nums[k] = v;
  }
  const n = nums;
  if (n.capital !== undefined && n.capital <= 0) add("capital", "capital must be > 0");
  if (n.fixed_fraction_pct !== undefined && !(n.fixed_fraction_pct > 0 && n.fixed_fraction_pct <= 100)) add("fixed_fraction_pct", "fixed_fraction_pct must be in (0, 100]");
  if (n.kelly_fraction_cap_pct !== undefined && !(n.kelly_fraction_cap_pct > 0 && n.kelly_fraction_cap_pct <= 25)) add("kelly_fraction_cap_pct", "kelly_fraction_cap_pct must be in (0, 25.0]");
  if (n.kelly_fraction_cap_pct !== undefined && n.fixed_fraction_pct !== undefined && n.kelly_fraction_cap_pct < n.fixed_fraction_pct) {
    add("kelly_fraction_cap_pct", "kelly_fraction_cap_pct must be >= fixed_fraction_pct");
  }
  if (n.max_daily_loss !== undefined && n.capital !== undefined && !(n.max_daily_loss > 0 && n.max_daily_loss <= n.capital)) add("max_daily_loss", "max_daily_loss must be in (0, capital]");
  if (n.max_daily_loss_pct !== undefined && !(n.max_daily_loss_pct > 0 && n.max_daily_loss_pct <= 100)) add("max_daily_loss_pct", "max_daily_loss_pct must be in (0, 100]");
  if (n.transition_cooldown_candles !== undefined && !(n.transition_cooldown_candles >= 1 && n.transition_cooldown_candles <= 8)) add("transition_cooldown_candles", "transition_cooldown_candles must be in [1, 8]");
  if (f.partial_exit && n.max_lots_per_trade !== undefined && n.max_lots_per_trade < 2) {
    const msg = "max_lots_per_trade must be >= 2 when partial_exit_enabled";
    add("max_lots_per_trade", msg);
    add("partial_exit_enabled", msg);
  }
  for (const [k, v] of Object.entries(f.max_hold)) {
    const h = parseNum(v, true);
    if (h === null || h < 1 || h > MAX_HOLD_MAX) add(`max_hold_minutes.${k}`, `max_hold_minutes[${k}] must be a whole number in [1, ${MAX_HOLD_MAX}]`);
  }
  return e;
}

function buildOverrides(f: FormState): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const k of SCALARS_BEFORE) out[k] = Number(f.scalars[k]);
  out.partial_exit_enabled = f.partial_exit;
  for (const k of SCALARS_AFTER) out[k] = Number(f.scalars[k]);
  out.max_hold_minutes = Object.fromEntries(Object.entries(f.max_hold).map(([k, v]) => [k, Number(v)]));
  out.transition_cooldown_candles = Number(f.scalars.transition_cooldown_candles);
  return out;
}

function buildRequest(f: FormState): LaunchRequest {
  return {
    dataset: f.dataset === "synthetic"
      ? { kind: "synthetic", days: Number(f.synth_days), seed: Number(f.synth_seed), start: f.synth_start }
      : { kind: "replay_db" },
    date_from: f.date_from,
    date_to: f.date_to,
    underlyings: f.underlyings,
    seed: Number(f.seed),
    oos_split: Number(f.oos_split),
    bootstrap_iterations: Number(f.bootstrap),
    label: f.label,
    notes: f.notes,
    overrides: buildOverrides(f),
  };
}

/** Server violations → fields, by the override key names each string mentions (a violation such as
 *  "max_lots_per_trade=1 must be >= 2 when partial_exit_enabled" belongs to two fields). */
function mapViolations(violations: string[]): Errors {
  const e: Errors = {};
  for (const v of violations) {
    for (const k of OVERRIDE_KEYS) {
      if (new RegExp(`(^|[^A-Za-z0-9_])${k}([^A-Za-z0-9_]|$)`).test(v)) (e[k] ??= []).push(v);
    }
  }
  return e;
}

// ── field chrome ─────────────────────────────────────────────────────────────

const INPUT = "w-full rounded-md border border-border-2 bg-surface-2 px-2 py-1 text-table text-text-1 disabled:opacity-60 aria-[invalid=true]:border-red";

function FieldErrors({ id, errors }: { id: string; errors?: string[] }) {
  if (!errors?.length) return null;
  return (
    <ul id={`${id}-err`} className="mt-0.5 space-y-0.5 text-micro text-red">
      {errors.map((m) => <li key={m}>{m}</li>)}
    </ul>
  );
}

function errProps(id: string, errors: string[] | undefined, hintId?: string) {
  const invalid = !!errors?.length;
  const described = [invalid ? `${id}-err` : null, hintId ?? null].filter(Boolean).join(" ");
  return { "aria-invalid": invalid || undefined, "aria-describedby": described || undefined };
}

function NumberField({
  k, label, value, onChange, errors, min, max, step,
}: {
  k: string; label: string; value: string; onChange: (v: string) => void; errors?: string[];
  min?: number | string; max?: number | string; step?: string;
}) {
  const id = fieldId(k);
  return (
    <div>
      <label htmlFor={id} className="mb-0.5 block text-micro font-semibold text-text-2">{label}</label>
      <input id={id} type="number" inputMode="decimal" value={value} min={min} max={max} step={step}
        onChange={(e) => onChange(e.target.value)} className={cn(INPUT, "font-mono")} {...errProps(id, errors)} />
      <FieldErrors id={id} errors={errors} />
    </div>
  );
}

const RESET_BTN = "rounded border border-border-2 px-2 py-0.5 text-micro text-text-2 hover:bg-surface-3";
const LEGEND = "px-1 text-micro font-semibold uppercase text-text-2";

// ── the form ─────────────────────────────────────────────────────────────────

export interface LaunchFormProps {
  options:        LaunchOptions;
  adminKey:       string | null;
  onRequestKey:   () => void;
  /** 401: clear the key and reopen the dialog. */
  onUnauthorized: () => void;
  onLaunched:     (jobId: string) => void;
  /** 409: a job is already running — focus it. */
  onJobRunning:   (jobId: string | null) => void;
}

export function LaunchForm({ options, adminKey, onRequestKey, onUnauthorized, onLaunched, onJobRunning }: LaunchFormProps) {
  const qc = useQueryClient();
  const [f, setF] = useState<FormState>(() => initialState(options));
  const [errors, setErrors] = useState<Errors>({});
  const [summary, setSummary] = useState<string[]>([]);
  const [serverAlert, setServerAlert] = useState<string | null>(null);
  const summaryRef = useRef<HTMLDivElement>(null);
  const validate = useValidateParams();
  const launch = useLaunchJob();
  const pending = validate.isPending || launch.isPending;

  const disabled = !options.admin_key_configured;
  const limit = Math.min(options.limits.max_range_days, options.limits.max_launch_range_days);
  const span = f.date_from && f.date_to ? windowDays(f.date_from, f.date_to) : null;
  const set = <K extends keyof FormState>(k: K, v: FormState[K]) => setF((s) => ({ ...s, [k]: v }));
  const defaults = initialState(options);

  const showErrors = (e: Errors, list: string[], alert: string | null = null) => {
    setErrors(e);
    setSummary(list);
    setServerAlert(alert);
    requestAnimationFrame(() => summaryRef.current?.focus());
  };

  const showViolations = (violations: string[]) =>
    showErrors(mapViolations(violations), violations);

  const handleKeyError = (e: unknown): boolean => {
    if (isHttpStatus(e, 401)) { onUnauthorized(); return true; }
    if (isHttpStatus(e, 503)) { showErrors({}, [], "Admin key not configured on the API"); return true; }
    return false;
  };

  const submit = async (ev: React.FormEvent) => {
    ev.preventDefault();
    if (disabled || pending) return;
    const clientErrors = validateClient(f, options);
    const list = Array.from(new Set(Object.values(clientErrors).flat()));
    if (list.length) { showErrors(clientErrors, list); return; }
    if (!adminKey) { onRequestKey(); return; }
    setErrors({}); setSummary([]); setServerAlert(null);

    const body = buildRequest(f);
    let result: ValidateResponse;
    try {
      result = await validate.mutateAsync({ overrides: body.overrides ?? {}, key: adminKey });
    } catch (e) {
      if (handleKeyError(e)) return;
      if (e instanceof ApiError && (e.kind === "network" || e.kind === "timeout")) {
        toast.error("Could not validate parameters — the launch was not started");
      } else {
        showErrors({}, [], e instanceof ApiError && e.detail ? e.detail : errorMessage(e));
      }
      return;
    }
    if (!result.valid) { showViolations(result.violations); return; }

    try {
      const res = await launch.mutateAsync({ body, key: adminKey });
      toast.success(`Backtest job ${res.job_id} started`);
      onLaunched(res.job_id);
    } catch (e) {
      if (handleKeyError(e)) return;
      const running = runningJobIdFrom409(e);
      if (running !== null) {
        showErrors({}, [], "A backtest job is already running.");
        onJobRunning(running || null);
        return;
      }
      if (isHttpStatus(e, 400) && e instanceof ApiError) {
        if (e.detail === "PARAM_VIOLATIONS") {
          // the 400 carries no list by design; the validate endpoint returns it untruncated
          try {
            const again = await validate.mutateAsync({ overrides: body.overrides ?? {}, key: adminKey });
            showViolations(again.violations.length ? again.violations : ["the server rejected the parameter overrides"]);
          } catch (e2) {
            if (!handleKeyError(e2)) showErrors({}, [], errorMessage(e2));
          }
          return;
        }
        if (e.detail === "replay_store_not_initialised") {
          void qc.invalidateQueries({ queryKey: queryKeys.backtestDefaults });
          showErrors({}, [], "The configured replay database names a synthetic dataset that has not been generated yet — launch with a synthetic dataset first.");
          return;
        }
      }
      showErrors({}, [], e instanceof ApiError && e.detail ? e.detail : errorMessage(e));
    }
  };

  const applyDatasetSpan = () => {
    const s = synthSpan(f.synth_start, Number(f.synth_days));
    if (s) setF((st) => ({ ...st, date_from: s.from, date_to: s.to }));
  };

  const err = (k: string) => errors[k];
  const replayEntry = options.datasets.find((d) => d.kind === "replay_db");
  const hasAlert = summary.length > 0 || serverAlert !== null;

  return (
    <form onSubmit={submit} noValidate aria-label="Launch a backtest" className="space-y-4">
      {disabled && (
        <p role="note" className="rounded-md border border-yellow/40 bg-yellow/10 px-3 py-2 text-table font-semibold text-yellow">
          Launching is disabled: <code>ADMIN_API_KEY</code> is not set on the API.
        </p>
      )}

      <div ref={summaryRef} tabIndex={-1} className="focus:outline-none">
        {hasAlert && (
          <div role="alert" className="rounded-md border border-red/40 bg-red/10 px-3 py-2 text-table text-red">
            {serverAlert && <p className="font-semibold">{serverAlert}</p>}
            {summary.length > 0 && (
              <>
                <p className="font-semibold">The launch was not started: {summary.length} problem{summary.length === 1 ? "" : "s"}</p>
                <ul className="mt-1 list-disc space-y-0.5 pl-5">
                  {summary.map((v) => {
                    const target = Object.keys(errors).find((k) => errors[k]?.includes(v));
                    return (
                      <li key={v}>
                        {target ? <a href={`#${fieldId(target)}`} className="underline underline-offset-2">{v}</a> : v}
                      </li>
                    );
                  })}
                </ul>
              </>
            )}
          </div>
        )}
      </div>

      <fieldset disabled={disabled || pending} className="space-y-3 rounded-lg border border-border p-3">
        <legend className={LEGEND}>Window and data</legend>
        <div>
          <label htmlFor={fieldId("dataset")} className="mb-0.5 block text-micro font-semibold text-text-2">Dataset</label>
          <select id={fieldId("dataset")} value={f.dataset} onChange={(e) => set("dataset", e.target.value)} className={INPUT}>
            {options.datasets.map((d) => (
              <option key={d.dataset_id} value={d.dataset_id} disabled={!d.launchable}>
                {d.kind === "replay_db" && !d.launchable
                  ? `${d.label} — not generated yet; launch a synthetic dataset first`
                  : d.kind === "replay_db" && d.alias_of
                    ? `${d.label} — synthetic file ${d.alias_of}`
                    : d.label}
              </option>
            ))}
          </select>
          {replayEntry?.synthetic && f.dataset === replayEntry.dataset_id && (
            <p className="mt-0.5 text-micro text-text-3">This database is a generated synthetic dataset; runs on it are labelled synthetic.</p>
          )}
        </div>

        {f.dataset === "synthetic" && (
          <div className="grid gap-3 sm:grid-cols-3">
            <NumberField k="dataset.days" label="Days (trading days)" value={f.synth_days} onChange={(v) => set("synth_days", v)}
              errors={err("dataset.days")} min={options.limits.synthetic_min_days} max={options.limits.synthetic_max_days} step="1" />
            <div>
              <label htmlFor={fieldId("dataset.start")} className="mb-0.5 block text-micro font-semibold text-text-2">Start (weekday)</label>
              <input id={fieldId("dataset.start")} type="date" value={f.synth_start} min={START_MIN} max={START_MAX}
                onChange={(e) => set("synth_start", e.target.value)} className={cn(INPUT, "font-mono")} {...errProps(fieldId("dataset.start"), err("dataset.start"))} />
              <FieldErrors id={fieldId("dataset.start")} errors={err("dataset.start")} />
            </div>
            <NumberField k="dataset.seed" label="Dataset seed" value={f.synth_seed} onChange={(v) => set("synth_seed", v)}
              errors={err("dataset.seed")} min={0} max={SEED_MAX} step="1" />
            <div className="sm:col-span-3">
              <button type="button" onClick={applyDatasetSpan} className={RESET_BTN}>Set the window to the dataset&apos;s span</button>
            </div>
          </div>
        )}

        <div className="grid gap-3 sm:grid-cols-2">
          <div>
            <label htmlFor={fieldId("date_from")} className="mb-0.5 block text-micro font-semibold text-text-2">Date from</label>
            <input id={fieldId("date_from")} type="date" value={f.date_from} max={f.date_to || undefined}
              onChange={(e) => set("date_from", e.target.value)} className={cn(INPUT, "font-mono")}
              {...errProps(fieldId("date_from"), err("date_from"), "bt-window-hint")} />
            <FieldErrors id={fieldId("date_from")} errors={err("date_from")} />
          </div>
          <div>
            <label htmlFor={fieldId("date_to")} className="mb-0.5 block text-micro font-semibold text-text-2">Date to</label>
            <input id={fieldId("date_to")} type="date" value={f.date_to} min={f.date_from || undefined}
              max={f.date_from ? isoDay(addDays(parseISO(f.date_from), limit)) : undefined}
              onChange={(e) => set("date_to", e.target.value)} className={cn(INPUT, "font-mono")}
              {...errProps(fieldId("date_to"), err("date_to"), "bt-window-hint")} />
            <FieldErrors id={fieldId("date_to")} errors={err("date_to")} />
          </div>
          <p id="bt-window-hint" className={cn("text-micro sm:col-span-2", span !== null && span > limit ? "text-red" : "text-text-3")}>
            {span === null ? `Window limit ${limit} days` : `Window is ${span} days (limit ${limit})`}
          </p>
        </div>

        <fieldset id={fieldId("underlyings")} tabIndex={-1} {...errProps(fieldId("underlyings"), err("underlyings"))}>
          <legend className="mb-0.5 text-micro font-semibold text-text-2">Underlyings</legend>
          <div className="flex gap-4">
            {options.underlyings.map((u) => (
              <label key={u} className="inline-flex items-center gap-1.5 text-table text-text-1">
                <input type="checkbox" checked={f.underlyings.includes(u)}
                  onChange={(e) => set("underlyings", e.target.checked ? [...f.underlyings, u] : f.underlyings.filter((x) => x !== u))} />
                {u}
              </label>
            ))}
          </div>
          <FieldErrors id={fieldId("underlyings")} errors={err("underlyings")} />
        </fieldset>
        <div className="flex justify-end">
          <button type="button" className={RESET_BTN} onClick={() => setF((s) => ({
            ...s, dataset: defaults.dataset, date_from: defaults.date_from, date_to: defaults.date_to,
            underlyings: defaults.underlyings, synth_days: defaults.synth_days, synth_start: defaults.synth_start,
            synth_seed: defaults.synth_seed,
          }))}>Reset to defaults</button>
        </div>
      </fieldset>

      <fieldset disabled={disabled || pending} className="space-y-3 rounded-lg border border-border p-3">
        <legend className={LEGEND}>Run</legend>
        <div className="grid gap-3 sm:grid-cols-2">
          <NumberField k="seed" label="Seed" value={f.seed} onChange={(v) => set("seed", v)} errors={err("seed")} min={0} max={SEED_MAX} step="1" />
          <NumberField k="oos_split" label="Out-of-sample split" value={f.oos_split} onChange={(v) => set("oos_split", v)}
            errors={err("oos_split")} min={options.limits.oos_split.min} max={options.limits.oos_split.max} step="0.05" />
        </div>
        <details open={!!err("bootstrap_iterations")}>
          <summary className="cursor-pointer text-micro font-semibold text-text-2">Advanced</summary>
          <div className="mt-2 max-w-xs">
            <NumberField k="bootstrap_iterations" label="Bootstrap iterations" value={f.bootstrap} onChange={(v) => set("bootstrap", v)}
              errors={err("bootstrap_iterations")} min={options.limits.bootstrap_iterations.min} max={options.limits.bootstrap_iterations.max} step="1" />
          </div>
        </details>
        <div>
          <label htmlFor={fieldId("label")} className="mb-0.5 block text-micro font-semibold text-text-2">Label</label>
          <input id={fieldId("label")} type="text" value={f.label} maxLength={LABEL_MAX} onChange={(e) => set("label", e.target.value)}
            className={INPUT} {...errProps(fieldId("label"), err("label"), "bt-label-hint")} />
          <p id="bt-label-hint" className="mt-0.5 text-micro text-text-3">
            Optional. On a synthetic dataset the API appends &ldquo;(synthetic)&rdquo; when the label does not already say so.
          </p>
          <FieldErrors id={fieldId("label")} errors={err("label")} />
        </div>
        <div>
          <label htmlFor={fieldId("notes")} className="mb-0.5 block text-micro font-semibold text-text-2">Notes</label>
          <textarea id={fieldId("notes")} value={f.notes} maxLength={NOTES_MAX} rows={3} onChange={(e) => set("notes", e.target.value)}
            className={INPUT} {...errProps(fieldId("notes"), err("notes"), "bt-notes-hint")} />
          <p id="bt-notes-hint" className="mt-0.5 text-micro text-yellow">
            Declare any parameter you fitted to earlier results — an overlapping re-run with changed parameters and empty
            notes is capped &ldquo;undeclared parameter search&rdquo; by the report.
          </p>
          <FieldErrors id={fieldId("notes")} errors={err("notes")} />
        </div>
        <div className="flex justify-end">
          <button type="button" className={RESET_BTN} onClick={() => setF((s) => ({
            ...s, seed: defaults.seed, oos_split: defaults.oos_split, bootstrap: defaults.bootstrap, label: "", notes: "",
          }))}>Reset to defaults</button>
        </div>
      </fieldset>

      <fieldset disabled={disabled || pending} className="space-y-3 rounded-lg border border-border p-3">
        <legend className={LEGEND}>Parameter overrides</legend>
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {SCALARS_BEFORE.map((k) => (
            <NumberField key={k} k={k} label={SCALARS[k].label} value={f.scalars[k]} errors={err(k)}
              min={SCALARS[k].min} max={SCALARS[k].max} step={SCALARS[k].step}
              onChange={(v) => setF((s) => ({ ...s, scalars: { ...s.scalars, [k]: v } }))} />
          ))}
          <div>
            <label className="mt-4 inline-flex items-center gap-1.5 text-table text-text-1">
              <input id={fieldId("partial_exit_enabled")} type="checkbox" checked={f.partial_exit}
                onChange={(e) => set("partial_exit", e.target.checked)} {...errProps(fieldId("partial_exit_enabled"), err("partial_exit_enabled"))} />
              Partial exit enabled
            </label>
            <FieldErrors id={fieldId("partial_exit_enabled")} errors={err("partial_exit_enabled")} />
          </div>
          {SCALARS_AFTER.map((k) => (
            <NumberField key={k} k={k} label={SCALARS[k].label} value={f.scalars[k]} errors={err(k)}
              min={SCALARS[k].min} max={SCALARS[k].max} step={SCALARS[k].step}
              onChange={(v) => setF((s) => ({ ...s, scalars: { ...s.scalars, [k]: v } }))} />
          ))}
          <NumberField k="transition_cooldown_candles" label={SCALARS.transition_cooldown_candles.label}
            value={f.scalars.transition_cooldown_candles} errors={err("transition_cooldown_candles")}
            min={1} max={8} step="1"
            onChange={(v) => setF((s) => ({ ...s, scalars: { ...s.scalars, transition_cooldown_candles: v } }))} />
        </div>
        <fieldset id={fieldId("max_hold_minutes")} tabIndex={-1} {...errProps(fieldId("max_hold_minutes"), err("max_hold_minutes"))}>
          <legend className="mb-1 text-micro font-semibold text-text-2">Max hold (minutes, per strategy)</legend>
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            {Object.keys(f.max_hold).map((k) => (
              <NumberField key={k} k={`max_hold_minutes.${k}`} label={k} value={f.max_hold[k]} errors={err(`max_hold_minutes.${k}`)}
                min={1} max={MAX_HOLD_MAX} step="1"
                onChange={(v) => setF((s) => ({ ...s, max_hold: { ...s.max_hold, [k]: v } }))} />
            ))}
          </div>
          <FieldErrors id={fieldId("max_hold_minutes")} errors={err("max_hold_minutes")} />
        </fieldset>
        <div className="flex justify-end">
          <button type="button" className={RESET_BTN} onClick={() => setF((s) => ({ ...s, ...overrideState(options.defaults.overrides) }))}>
            Reset to defaults
          </button>
        </div>
      </fieldset>

      <div className="flex flex-wrap items-center gap-3">
        <button
          type="submit"
          disabled={disabled || pending}
          className="rounded-md border border-accent/60 bg-accent/30 px-4 py-1.5 text-table font-semibold text-text-1 hover:bg-accent/40 disabled:cursor-not-allowed disabled:opacity-60"
        >
          {pending ? "Launching…" : adminKey ? "Launch backtest" : "Enter admin key to launch"}
        </button>
        {options.running_job_id && (
          <span className="text-micro text-text-3">Job {options.running_job_id} is running; one job runs at a time.</span>
        )}
      </div>
    </form>
  );
}
