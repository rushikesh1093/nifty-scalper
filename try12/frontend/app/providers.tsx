"use client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Toaster } from "react-hot-toast";
import { useState } from "react";
import { useWakeLock } from "@/hooks/useWakeLock";

function WakeLockGuard() {
  useWakeLock();
  return null;
}

export function Providers({ children }: { children: React.ReactNode }) {
  const [queryClient] = useState(() => new QueryClient({
    defaultOptions: {
      queries: {
        retry:      1,
        staleTime:  5000,
        refetchOnWindowFocus: false,
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
            background: "#1a1d27",
            color:      "#f3f4f6",
            border:     "1px solid #2a2d3a",
            fontSize:   "13px",
          },
        }}
      />
    </QueryClientProvider>
  );
}
