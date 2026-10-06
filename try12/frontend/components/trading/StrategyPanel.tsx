"use client";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import toast from "react-hot-toast";
import { getConfig, setStrategyFlag } from "@/lib/tradingApi";
import { ApiError, errorMessage } from "@/lib/http";
import { queryKeys } from "@/lib/queryKeys";
import { cn } from "@/lib/utils";
import { DataSurface } from "@/components/ui/DataSurface";
import { Skeleton } from "@/components/ui/Skeleton";
import { STRATEGY_META } from "./strategyMeta";
import type { TradingConfig } from "@/types/trading";

// Strategy enable flags as real keyboard-operable switches (Space/Enter), with an optimistic
// update that rolls back if the API refuses.

const failText = (e: unknown) => (e instanceof ApiError && e.detail ? e.detail : errorMessage(e));

function StrategyList({ cfg }: { cfg: TradingConfig }) {
  const qc = useQueryClient();
  const flags = cfg.strategy_flags && typeof cfg.strategy_flags === "object" ? cfg.strategy_flags : {};

  const toggle = useMutation({
    mutationFn: ({ name, enabled }: { name: string; enabled: boolean }) => setStrategyFlag(name, enabled),
    onMutate: async ({ name, enabled }) => {
      await qc.cancelQueries({ queryKey: queryKeys.config });
      const previous = qc.getQueryData<TradingConfig>(queryKeys.config);
      if (previous) {
        qc.setQueryData<TradingConfig>(queryKeys.config, {
          ...previous,
          strategy_flags: { ...previous.strategy_flags, [name]: enabled },
        });
      }
      return { previous };
    },
    onError: (e: unknown, { name }, ctx) => {
      if (ctx?.previous) qc.setQueryData(queryKeys.config, ctx.previous);
      console.error("[strategies] flag update failed", e);
      toast.error(`Could not update ${name}: ${failText(e)}`);
    },
    onSuccess: (_d, { name, enabled }) => toast.success(`${name} ${enabled ? "enabled" : "disabled"}`),
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: queryKeys.config });
      void qc.invalidateQueries({ queryKey: queryKeys.diagnostics });
    },
  });

  const known = new Set(STRATEGY_META.map((s) => s.key));
  const items = [
    ...STRATEGY_META,
    ...Object.keys(flags).filter((k) => !known.has(k)).map((k) => ({ key: k, label: k.replace(/_/g, " "), edge: "" })),
  ];
  const activeCount = items.filter((s) => flags[s.key] !== false).length;

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between text-table text-text-2">
        <span>Enable or pause individual strategies. Regime rules may still block an enabled one.</span>
        <span className="shrink-0 pl-2 font-mono">
          <span className="font-bold text-green">{activeCount}</span>/{items.length} on
        </span>
      </div>
      <ul className="space-y-1">
        {items.map((s, i) => {
          const enabled = flags[s.key] !== false;
          const labelId = `strategy-${s.key}-label`;
          return (
            <li
              key={s.key}
              className={cn(
                "flex items-center justify-between gap-3 rounded-lg border px-3 py-2",
                enabled ? "border-green/25 bg-green/5" : "border-border bg-transparent",
              )}
            >
              <div className="flex min-w-0 items-center gap-2.5">
                <span aria-hidden="true" className="w-4 shrink-0 text-right font-mono text-micro text-text-3">{i + 1}</span>
                <div className="min-w-0">
                  <div id={labelId} className={cn("truncate text-table font-medium", enabled ? "text-text-1" : "text-text-2")}>{s.label}</div>
                  {s.edge && <div className="truncate text-micro normal-case tracking-normal text-text-2">{s.edge}</div>}
                </div>
              </div>
              <button
                type="button"
                role="switch"
                aria-checked={enabled}
                aria-labelledby={labelId}
                data-strategy={s.key}
                onClick={() => toggle.mutate({ name: s.key, enabled: !enabled })}
                className={cn(
                  "relative h-5 w-9 shrink-0 rounded-full border transition-colors",
                  enabled ? "border-green bg-green" : "border-border-2 bg-surface-3",
                )}
              >
                <span className="sr-only">{enabled ? "on" : "off"}</span>
                <span
                  aria-hidden="true"
                  className={cn(
                    "absolute top-0.5 h-3.5 w-3.5 rounded-full bg-text-1 shadow transition-transform",
                    enabled ? "translate-x-[18px]" : "translate-x-0.5",
                  )}
                />
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

export function StrategyPanel() {
  const query = useQuery({ queryKey: queryKeys.config, queryFn: getConfig, refetchInterval: 15_000 });
  return (
    <DataSurface query={query} skeleton={<Skeleton label="Loading strategies" className="py-4" />}>
      {(cfg) => <StrategyList cfg={cfg} />}
    </DataSurface>
  );
}
