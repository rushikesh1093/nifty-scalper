"use client";
import { useEffect, useState } from "react";

/**
 * false during SSR and the hydration render, true after mount. A query shared with an
 * already-hydrated component (e.g. diagnostics, also read by the layout's StatusBar) can hold data
 * by the time a lazily hydrated page segment hydrates; rendering it then would mismatch the
 * server HTML (which never has data). Gate data rendering on this.
 */
export function useHydrated(): boolean {
  const [hydrated, setHydrated] = useState(false);
  useEffect(() => setHydrated(true), []);
  return hydrated;
}
