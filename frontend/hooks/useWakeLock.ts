"use client";
import { useEffect, useRef } from "react";

/**
 * Prevent the screen from going to sleep using the Screen Wake Lock API.
 *
 * - Acquires a "screen" wake lock on mount.
 * - Automatically re-acquires it when the page becomes visible again
 *   (browser releases the lock whenever the tab is hidden or the screen
 *   turns off, so we must re-request on visibilitychange).
 * - Releases cleanly on unmount.
 * - Silent no-op in browsers that don't support the API (e.g. older Firefox).
 */
export function useWakeLock() {
  const lockRef = useRef<WakeLockSentinel | null>(null);

  useEffect(() => {
    // API not available (old browser, non-HTTPS in some envs, etc.)
    if (!("wakeLock" in navigator)) return;

    let cancelled = false;

    async function acquire() {
      if (cancelled) return;
      try {
        lockRef.current = await navigator.wakeLock.request("screen");
        lockRef.current.addEventListener("release", () => {
          // Fired when the browser releases the lock (tab hidden, screen off, etc.)
          lockRef.current = null;
        });
      } catch {
        // User denied or API unavailable — silently ignore
      }
    }

    // Re-acquire after the tab becomes visible again (browser always releases
    // the lock when the document is hidden, so we must re-request each time).
    function onVisibilityChange() {
      if (document.visibilityState === "visible") {
        acquire();
      }
    }

    document.addEventListener("visibilitychange", onVisibilityChange);
    acquire();

    return () => {
      cancelled = true;
      document.removeEventListener("visibilitychange", onVisibilityChange);
      lockRef.current?.release().catch(() => {});
      lockRef.current = null;
    };
  }, []);
}
