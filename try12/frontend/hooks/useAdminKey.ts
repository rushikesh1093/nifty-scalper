"use client";
import { useCallback, useSyncExternalStore } from "react";
import { clearAdminKey, getAdminKey, setAdminKey, subscribeAdminKey } from "@/lib/adminKey";

// The session-scoped admin key (design §5.5). The server snapshot is always null, so SSR and the
// first client render agree; the stored key appears after hydration.
const serverSnapshot = () => null;

export function useAdminKey(): {
  key:    string | null;
  hasKey: boolean;
  setKey: (k: string) => void;
  clear:  () => void;
} {
  const key = useSyncExternalStore(subscribeAdminKey, getAdminKey, serverSnapshot);
  const setKey = useCallback((k: string) => setAdminKey(k), []);
  const clear = useCallback(() => clearAdminKey(), []);
  return { key, hasKey: key !== null, setKey, clear };
}
