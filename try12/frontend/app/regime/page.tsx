"use client";
import { useState } from "react";
import { cn } from "@/lib/utils";
import type { Underlying } from "@/hooks/useRegime";
import { PageHeader } from "@/components/ui/PageHeader";
import { CurrentRegimeCard } from "@/components/regime/CurrentRegimeCard";
import { RegimeTimeline } from "@/components/regime/RegimeTimeline";
import { RegimePerformance } from "@/components/regime/RegimePerformance";

export default function RegimePage() {
  const [underlying, setUnderlying] = useState<Underlying>("NIFTY");
  return (
    <div className="mx-auto max-w-[2400px] space-y-4 p-4">
      <PageHeader
        title="Market regime"
        description="What the detector sees now and how it has changed over the last 24 hours. The regime runs in shadow unless regime gating is switched on: it is recorded, and only a detector failure or the expiry-day policy blocks trading."
        actions={
          <div role="group" aria-label="Underlying" className="inline-flex rounded-lg border border-border bg-surface-2 p-0.5">
            {(["NIFTY", "BANKNIFTY"] as const).map((u) => (
              <button
                key={u}
                type="button"
                aria-pressed={underlying === u}
                onClick={() => setUnderlying(u)}
                className={cn(
                  "h-7 rounded-md px-3 text-table font-semibold",
                  underlying === u ? "bg-accent/30 text-text-1" : "text-text-2 hover:text-text-1",
                )}
              >
                {u}
              </button>
            ))}
          </div>
        }
      />
      <div className="grid grid-cols-1 items-start gap-4 xl:grid-cols-[360px_minmax(0,1fr)] 2xl:grid-cols-[420px_minmax(0,1fr)]">
        <CurrentRegimeCard underlying={underlying} />
        <div className="min-w-0 space-y-4">
          <RegimeTimeline underlying={underlying} />
          <RegimePerformance underlying={underlying} />
        </div>
      </div>
    </div>
  );
}
