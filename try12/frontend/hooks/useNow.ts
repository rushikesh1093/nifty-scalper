"use client";
import { useEffect, useState } from "react";

/**
 * Current time, refreshed every `intervalMs`, for relative ages ("5m ago"). Starts as null and is
 * set in a mount effect so SSR and the first client render agree (no hydration mismatch).
 */
export function useNow(intervalMs = 15_000): Date | null {
  const [now, setNow] = useState<Date | null>(null);
  useEffect(() => {
    setNow(new Date());
    const id = setInterval(() => setNow(new Date()), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);
  return now;
}
