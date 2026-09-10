"use client";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { getConfig, setStrategyFlag } from "@/lib/tradingApi";
import { STRATEGY_META } from "./strategyMeta";

export function StrategyPanel() {
  const qc = useQueryClient();
  const { data: cfg } = useQuery({ queryKey: ["config"], queryFn: getConfig, refetchInterval: 15000 });
  const toggle = useMutation({
    mutationFn: ({ name, enabled }: { name: string; enabled: boolean }) => setStrategyFlag(name, enabled),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["config"] }),
  });

  const flags       = cfg?.strategy_flags || {};
  const activeCount = STRATEGY_META.filter(s => flags[s.key] !== false).length;

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <span className="text-[11px] font-semibold uppercase tracking-wider" style={{ color: "var(--text-3)" }}>Strategies</span>
        <span className="font-mono text-[10px]" style={{ color: "var(--text-3)" }}>
          <span style={{ color: "var(--green)", fontWeight: 700 }}>{activeCount}</span>
          /{STRATEGY_META.length} active
        </span>
      </div>

      <div className="space-y-1">
        {STRATEGY_META.map((s, i) => {
          const enabled = flags[s.key] !== false;
          return (
            <div
              key={s.key}
              onClick={() => toggle.mutate({ name: s.key, enabled: !enabled })}
              className="flex items-center justify-between px-3 py-2 rounded-lg cursor-pointer select-none transition-all"
              style={{
                background: enabled ? "var(--green-dim)" : "transparent",
                border: `1px solid ${enabled ? "#22d3a020" : "var(--border)"}`,
                opacity: enabled ? 1 : 0.45,
              }}
            >
              <div className="flex items-center gap-2.5 min-w-0">
                <span className="font-mono text-[9px] w-4 shrink-0 text-right" style={{ color: "var(--text-4)" }}>{i + 1}</span>
                <div className="min-w-0">
                  <div className="text-[11px] font-medium truncate" style={{ color: "var(--text-1)" }}>{s.label}</div>
                  <div className="text-[9px] truncate" style={{ color: "var(--text-4)" }}>{s.edge}</div>
                </div>
              </div>
              {/* Toggle */}
              <div
                className="w-8 h-4 rounded-full relative shrink-0 ml-3 transition-colors"
                style={{ background: enabled ? "var(--green)" : "var(--surface-3)" }}
              >
                <span
                  className="absolute top-0.5 w-3 h-3 bg-white rounded-full shadow transition-transform"
                  style={{ transform: enabled ? "translateX(18px)" : "translateX(2px)" }}
                />
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
