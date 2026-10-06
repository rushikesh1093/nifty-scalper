"use client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Toaster } from "react-hot-toast";
import { useState } from "react";
import { useWakeLock } from "@/hooks/useWakeLock";
import { ApiError } from "@/lib/http";

function WakeLockGuard() {
  useWakeLock();
  return null;
}

/**
 * Retry policy (design §6.4): never retry a 4xx (the request itself is wrong), retry a timeout
 * once, otherwise (network / 5xx / parse) up to 2 times.
 */
function shouldRetry(failureCount: number, error: unknown): boolean {
  if (error instanceof ApiError) {
    if (error.kind === "http" && error.status !== null && error.status >= 400 && error.status < 500) return false;
    if (error.kind === "timeout") return failureCount < 1;
  }
  return failureCount < 2;
}

const retryDelay = (attempt: number) => Math.min(1000 * 2 ** attempt, 8000);

export function Providers({ children }: { children: React.ReactNode }) {
  // QueryClient stays inside useState (App Router: one client per browser session, none shared on the server).
  const [queryClient] = useState(() => new QueryClient({
    defaultOptions: {
      queries: {
        retry:                shouldRetry,
        retryDelay,
        staleTime:            5_000,
        refetchOnWindowFocus: true,
        refetchOnReconnect:   true,
      },
    },
  }));

  return (
    <QueryClientProvider client={queryClient}>
      <WakeLockGuard />
      {children}
      <Toaster
        position="bottom-right"
        toastOptions={{
          style: {
            background: "var(--surface-2)",
            color:      "var(--text-1)",
            border:     "1px solid var(--border-2)",
            fontSize:   "13px",
          },
        }}
      />
    </QueryClientProvider>
  );
}
