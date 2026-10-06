"use client";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import toast from "react-hot-toast";
import { getMode, switchPaper, switchLive } from "@/lib/tradingApi";
import { ApiError, errorMessage } from "@/lib/http";
import { queryKeys } from "@/lib/queryKeys";
import { Panel } from "@/components/ui/Panel";
import { DataSurface } from "@/components/ui/DataSurface";
import { Skeleton } from "@/components/ui/Skeleton";
import { Badge } from "@/components/ui/Badge";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";

const failText = (e: unknown) => (e instanceof ApiError && e.detail ? e.detail : errorMessage(e));

export function ModeToggle() {
  const qc = useQueryClient();
  const [confirm, setConfirm] = useState(false);
  const query = useQuery({ queryKey: queryKeys.mode, queryFn: getMode, refetchInterval: 10_000 });

  const invalidate = () => {
    for (const key of [queryKeys.mode, queryKeys.config, queryKeys.capital, queryKeys.diagnostics]) {
      void qc.invalidateQueries({ queryKey: key });
    }
  };

  const toPaper = useMutation({
    mutationFn: switchPaper,
    onSuccess: () => { invalidate(); toast.success("Switched to PAPER"); },
    onError: (e: unknown) => { console.error("[mode] paper switch failed", e); toast.error(`Cannot switch to paper: ${failText(e)}`); },
  });
  const toLive = useMutation({
    mutationFn: () => switchLive(true),
    onSuccess: () => { invalidate(); setConfirm(false); toast.success("LIVE mode on"); },
    onError: (e: unknown) => { console.error("[mode] live switch failed", e); toast.error(`Cannot go live: ${failText(e)}`); },
  });

  return (
    <Panel title="Trading mode">
      <DataSurface query={query} skeleton={<Skeleton label="Loading mode" />} hideFreshness>
        {(m) => {
          const isLive = m.mode === "LIVE";
          return (
            <div className="space-y-3">
              <div className="flex items-center justify-between">
                <span className="text-table text-text-2">
                  {isLive ? "Real orders are placed through AngelOne." : "Simulated fills; no broker orders."}
                </span>
                <span aria-live="polite" aria-atomic="true">
                  <span className="sr-only">Current mode: </span>
                  <Badge intent={isLive ? "negative" : "positive"} strength="outline" status={m.mode} dot={isLive ? "pulse" : true}>
                    {m.mode}
                  </Badge>
                </span>
              </div>
              {isLive ? (
                <button
                  type="button"
                  onClick={() => toPaper.mutate()}
                  disabled={toPaper.isPending}
                  className="w-full rounded-lg border border-border-2 bg-surface-3 py-2 text-body font-semibold text-text-1 hover:bg-surface-2 disabled:opacity-50"
                >
                  {toPaper.isPending ? "Switching…" : "Switch to paper"}
                </button>
              ) : (
                <button
                  type="button"
                  onClick={() => setConfirm(true)}
                  className="w-full rounded-lg border border-red/40 bg-red/10 py-2 text-body font-semibold text-red hover:bg-red/20"
                >
                  Go live…
                </button>
              )}
            </div>
          );
        }}
      </DataSurface>

      <ConfirmDialog
        open={confirm}
        title="Switch to LIVE trading?"
        confirmLabel="Confirm — go live"
        danger
        pending={toLive.isPending}
        onConfirm={() => toLive.mutate()}
        onCancel={() => setConfirm(false)}
      >
        Real orders will be placed with your AngelOne account using the current capital and sizing settings.
        You can switch back to paper at any time, but orders already sent are not reversed.
      </ConfirmDialog>
    </Panel>
  );
}
