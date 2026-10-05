"use client";
import { useQuery } from "@tanstack/react-query";
import { getCurrentRegime } from "@/lib/api";
import { useEffect, useState } from "react";

const REGIME_COLORS: Record<string, string> = {
  // Bullish regimes (green)
  BULL_TREND_NORMAL_VOL:      "var(--green)",
  BREAKOUT_EVENT_BULLISH:     "var(--green)",
  
  // Bearish regimes (red)
  BEAR_TREND_NORMAL_VOL:      "var(--red)",
  BREAKOUT_EVENT_BEARISH:     "var(--red)",
  HIGH_VOL_CHOP:              "var(--red)",
  
  // Neutral/mixed regimes (yellow/orange)
  TREND_HIGH_VOL:             "#f59e0b",
  LOW_VOL_COMPRESSION:        "#f59e0b",
  RANGE_NORMAL_VOL:           "#f59e0b",
  REGIME_TRANSITION:          "#f59e0b",
  
  // Special (gray)
  EXPIRY_DAY:                 "var(--text-3)",
  UNKNOWN:                    "var(--text-4)",
};

function formatDuration(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  const mins = Math.floor(seconds / 60);
  const secs = seconds % 60;
  if (mins < 60) return `${mins}:${secs.toString().padStart(2, "0")}`;
  const hours = Math.floor(mins / 60);
  const remainMins = mins % 60;
  return `${hours}:${remainMins.toString().padStart(2, "0")}:${secs.toString().padStart(2, "0")}`;
}

export function RegimeStatus() {
  const { data } = useQuery({
    queryKey: ["regime-current"],
    queryFn: () => getCurrentRegime("NIFTY"),
    refetchInterval: 5000, // 5s
  });

  const [liveSeconds, setLiveSeconds] = useState(data?.seconds_in_regime || 0);

  useEffect(() => {
    if (data?.seconds_in_regime !== undefined) {
      setLiveSeconds(data.seconds_in_regime);
    }
  }, [data?.seconds_in_regime]);

  useEffect(() => {
    const timer = setInterval(() => {
      setLiveSeconds((prev) => prev + 1);
    }, 1000);
    return () => clearInterval(timer);
  }, []);

  const regime = data?.regime || "UNKNOWN";
  const color = REGIME_COLORS[regime] || "var(--text-3)";

  return (
    <div
      className="flex items-center gap-1.5 px-3"
      style={{ borderRight: "1px solid var(--border)" }}
    >
      <span
        className="w-1.5 h-1.5 rounded-full shrink-0"
        style={{ background: color }}
      />
      <span style={{ color, fontSize: "11px", fontWeight: 500 }}>
        {regime.replace(/_/g, " ")}
      </span>
      <span
        className="font-mono"
        style={{ color: "var(--text-3)", fontSize: "10px" }}
      >
        {formatDuration(liveSeconds)}
      </span>
    </div>
  );
}
