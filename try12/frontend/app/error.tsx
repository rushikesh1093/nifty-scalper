"use client";
// Route-level error boundary (Next 14 requires a Client Component). Does not catch errors thrown
// by the root layout itself (app/global-error.tsx is out of scope — design §11.3).
import { useEffect } from "react";

export default function RouteError({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  useEffect(() => {
    console.error("[route error]", error);
  }, [error]);

  return (
    <div className="mx-auto max-w-xl p-6">
      <section role="alert" aria-labelledby="route-error-title" className="panel">
        <div className="panel-header">
          <h1 id="route-error-title" className="panel-title text-red">This page failed to render</h1>
        </div>
        <div className="space-y-3 p-4">
          <p className="text-body text-text-1">{error.message || "Unexpected error"}</p>
          {error.digest && <p className="font-mono text-table text-text-3">digest {error.digest}</p>}
          <button
            type="button"
            onClick={reset}
            className="rounded-md border border-accent/40 bg-accent/15 px-3 py-1.5 text-table font-semibold text-accent hover:bg-accent/25"
          >
            Try again
          </button>
        </div>
      </section>
    </div>
  );
}
