"use client";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { getMode, switchPaper, switchLive } from "@/lib/tradingApi";
import toast from "react-hot-toast";

export function ModeToggle() {
  const qc = useQueryClient();
  const [confirm, setConfirm] = useState(false);

  const { data: modeData } = useQuery({
    queryKey: ["trading-mode"], queryFn: getMode, refetchInterval: 10000,
  });

  const toPaper = useMutation({
    mutationFn: switchPaper,
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["trading-mode"] }); toast.success("Switched to PAPER"); setConfirm(false); },
  });
  const toLive = useMutation({
    mutationFn: () => switchLive(true),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["trading-mode"] }); setConfirm(false); toast.success("LIVE MODE ON"); },
    onError: (e: any) => toast.error(e.response?.data?.detail || "Cannot go live"),
  });

  const isLive = modeData?.mode === "LIVE";

  return (
    <div className="rounded-xl p-4 space-y-3" style={{ background: "var(--surface)", border: "1px solid var(--border)" }}>
      <div className="flex items-center justify-between">
        <span className="text-[11px] font-semibold uppercase tracking-wider" style={{ color: "var(--text-3)" }}>Trading Mode</span>
        <span
          className="tag font-bold"
          style={isLive
            ? { color: "var(--red)",   background: "var(--red-dim)",   border: "1px solid #f43f5e30" }
            : { color: "var(--green)", background: "var(--green-dim)", border: "1px solid #22d3a030" }
          }
        >
          {isLive ? "● LIVE" : "● PAPER"}
        </span>
      </div>

      {isLive ? (
        <button onClick={() => toPaper.mutate()} disabled={toPaper.isPending}
          className="w-full py-2.5 rounded-lg text-[12px] font-semibold transition-all"
          style={{ background: "var(--surface-3)", color: "var(--text-2)", border: "1px solid var(--border-2)" }}>
          Switch to Paper
        </button>
      ) : !confirm ? (
        <button onClick={() => setConfirm(true)}
          className="w-full py-2.5 rounded-lg text-[12px] font-semibold transition-all"
          style={{ background: "var(--red-dim)", color: "var(--red)", border: "1px solid #f43f5e30" }}>
          Go Live
        </button>
      ) : (
        <div className="space-y-2.5">
          <p className="text-[10px] text-center leading-relaxed" style={{ color: "var(--red)" }}>
            Real orders will be placed with your AngelOne account.<br />This cannot be undone mid-session.
          </p>
          <div className="flex gap-2">
            <button onClick={() => toLive.mutate()} disabled={toLive.isPending}
              className="flex-1 py-2 rounded-lg text-[11px] font-bold"
              style={{ background: "var(--red)", color: "#fff" }}>
              Confirm — Go Live
            </button>
            <button onClick={() => setConfirm(false)}
              className="flex-1 py-2 rounded-lg text-[11px] font-medium"
              style={{ background: "var(--surface-3)", color: "var(--text-2)", border: "1px solid var(--border)" }}>
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
