// Admin key storage for the backtest launcher (design §5.5).
//
// The key lives in sessionStorage only: it dies with the tab, is not shared across tabs and does not
// survive a browser restart. It is never put in a NEXT_PUBLIC_* variable, never written to
// localStorage, never placed in a URL and never logged. It is sent only as the X-API-Key header of the
// four launcher calls in lib/backtestApi.ts, per call — never as an axios default.
//
// Threat model: sessionStorage is readable by any script running on this origin, so this protects
// against key persistence and against bundling, not against XSS. That is acceptable for a localhost
// single-operator tool and is why the key is not stored anywhere more durable.

const STORAGE_KEY = "nifty.adminKey";

type Listener = () => void;
const listeners = new Set<Listener>();

function notify(): void {
  listeners.forEach((l) => l());
}

export function getAdminKey(): string | null {
  if (typeof window === "undefined") return null;
  try {
    const v = window.sessionStorage.getItem(STORAGE_KEY);
    return v ? v : null;
  } catch {
    return null;          // private mode / storage disabled
  }
}

export function setAdminKey(key: string): void {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.setItem(STORAGE_KEY, key);
  } catch {
    // storage unavailable: the key simply is not kept
  }
  notify();
}

export function clearAdminKey(): void {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    // nothing stored
  }
  notify();
}

/** Subscribe to key changes in this tab; returns the unsubscribe function. */
export function subscribeAdminKey(listener: Listener): () => void {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}
