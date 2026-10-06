// IST time helpers. Dependency-free on purpose (tools\time-check.mjs transpiles this file alone).
// Every predicate takes an explicit `now` for testability and must only be called from effects or
// event handlers — never during render (SSR timezone ≠ browser timezone → hydration mismatch).

export const IST_TZ = "Asia/Kolkata";
const IST_OFFSET_MIN = 330; // UTC+05:30, no DST

export interface HourMinute { h: number; m: number }
export interface SessionWindow { open: HourMinute; close: HourMinute }

/** NSE cash/F&O trading hours: 09:15 ≤ t < 15:30 IST. Drives the "Market Open/Closed" chip. */
export const EXCHANGE_OPEN_IST:  HourMinute = { h: 9,  m: 15 };
export const EXCHANGE_CLOSE_IST: HourMinute = { h: 15, m: 30 };

/**
 * Strategy signal-generation window: 09:15 ≤ t ≤ 15:25 IST (inclusive end, like the runner's `<=`).
 * Mirrors backend settings.SESSION_START_IST / SESSION_END_IST — FALLBACK ONLY: the UI renders the
 * diagnostics payload's `session_window` once fetched (finding 13).
 */
export const SIGNAL_OPEN_IST:  HourMinute = { h: 9,  m: 15 };
export const SIGNAL_CLOSE_IST: HourMinute = { h: 15, m: 25 };

export const DEFAULT_SIGNAL_WINDOW: SessionWindow = { open: SIGNAL_OPEN_IST, close: SIGNAL_CLOSE_IST };

function istParts(now: Date): { weekday: number; minutes: number } {
  const shifted = new Date(now.getTime() + IST_OFFSET_MIN * 60_000);
  return { weekday: shifted.getUTCDay(), minutes: shifted.getUTCHours() * 60 + shifted.getUTCMinutes() };
}

const toMinutes = (t: HourMinute) => t.h * 60 + t.m;
const isWeekday = (d: number) => d >= 1 && d <= 5;

/** Exchange open: IST weekday and 09:15 ≤ t < 15:30. Exchange holidays are not detected. */
export function isExchangeOpenIst(now: Date = new Date()): boolean {
  const { weekday, minutes } = istParts(now);
  return isWeekday(weekday) && minutes >= toMinutes(EXCHANGE_OPEN_IST) && minutes < toMinutes(EXCHANGE_CLOSE_IST);
}

/** Signal window open: IST weekday and open ≤ t ≤ close (minute resolution, inclusive end). */
export function isSignalWindowOpenIst(now: Date = new Date(), window: SessionWindow = DEFAULT_SIGNAL_WINDOW): boolean {
  const { weekday, minutes } = istParts(now);
  return isWeekday(weekday) && minutes >= toMinutes(window.open) && minutes <= toMinutes(window.close);
}

/** Parse the diagnostics payload's `session_window`, e.g. "09:15-15:25 IST". */
export function parseSessionWindow(s: string | null | undefined): SessionWindow | null {
  if (!s) return null;
  const m = /^\s*(\d{1,2}):(\d{2})\s*[-–]\s*(\d{1,2}):(\d{2})/.exec(s);
  if (!m) return null;
  const [oh, om, ch, cm] = m.slice(1).map(Number);
  if (oh > 23 || ch > 23 || om > 59 || cm > 59) return null;
  return { open: { h: oh, m: om }, close: { h: ch, m: cm } };
}

const pad = (n: number) => n.toString().padStart(2, "0");

export function formatHourMinute(t: HourMinute): string {
  return `${pad(t.h)}:${pad(t.m)}`;
}

/** "09:15–15:25" */
export function formatSessionWindow(w: SessionWindow): string {
  return `${formatHourMinute(w.open)}–${formatHourMinute(w.close)}`;
}

function toDate(v: string | number | Date): Date {
  return v instanceof Date ? v : new Date(v);
}

/** Format an instant in IST. Default "HH:mm:ss"; `withDate` prefixes "dd MMM". Invalid/null → "—". */
export function formatIst(
  v: string | number | Date | null | undefined,
  opts: { seconds?: boolean; withDate?: boolean } = {},
): string {
  if (v === null || v === undefined || v === "") return "—";
  const d = toDate(v);
  if (Number.isNaN(d.getTime())) return "—";
  const { seconds = true, withDate = false } = opts;
  const shifted = new Date(d.getTime() + IST_OFFSET_MIN * 60_000);
  const time = `${pad(shifted.getUTCHours())}:${pad(shifted.getUTCMinutes())}${seconds ? `:${pad(shifted.getUTCSeconds())}` : ""}`;
  if (!withDate) return time;
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  return `${pad(shifted.getUTCDate())} ${MONTHS[shifted.getUTCMonth()]} ${time}`;
}

/** "just now", "42s ago", "5m ago", "2h 10m ago", "3d ago". Invalid/null → "—". */
export function relativeAge(v: string | number | Date | null | undefined, now: Date = new Date()): string {
  if (v === null || v === undefined || v === "") return "—";
  const d = toDate(v);
  if (Number.isNaN(d.getTime())) return "—";
  const secs = Math.max(0, Math.floor((now.getTime() - d.getTime()) / 1000));
  if (secs < 5) return "just now";
  if (secs < 60) return `${secs}s ago`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return mins % 60 ? `${hours}h ${mins % 60}m ago` : `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}
