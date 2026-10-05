"use client";
import { useQuery } from "@tanstack/react-query";
import { getRegimeHistory } from "@/lib/api";
import { useState } from "react";

const REGIME_COLORS: Record<string, string> = {
  // Bullish regimes (green)
  BULL_TREND_NORMAL_VOL:      "#10b981",
  BREAKOUT_EVENT_BULLISH:     "#10b981",
  
  // Bearish regimes (red)
  BEAR_TREND_NORMAL_VOL:      "#ef4444",
  BREAKOUT_EVENT_BEARISH:     "#ef4444",
  HIGH_VOL_CHOP:              "#ef4444",
  
  // Neutral/mixed regimes (yellow/orange)
  TREND_HIGH_VOL:             "#f59e0b",
  LOW_VOL_COMPRESSION:        "#f59e0b",
  RANGE_NORMAL_VOL:           "#f59e0b",
  REGIME_TRANSITION:          "#f59e0b",
  
  // Special (gray)
  EXPIRY_DAY:                 "#6b7280",
  UNKNOWN:                    "#6b7280",
};

export function RegimeTimeline() {
  const { data, isLoading } = useQuery({
    queryKey: ["regime-history"],
    queryFn: () => getRegimeHistory("NIFTY", 24),
    refetchInterval: 60000, // 60s
  });

  const [hoveredIdx, setHoveredIdx] = useState<number | null>(null);

  if (isLoading) {
    return (
      <div style={{ padding: "16px", color: "var(--text-3)", fontSize: "12px" }}>
        Loading timeline...
      </div>
    );
  }

  if (!data || data.length === 0) {
    return (
      <div style={{ padding: "16px", color: "var(--text-3)", fontSize: "12px" }}>
        No regime history available.
      </div>
    );
  }

  // Sort by timestamp ascending (oldest first) for timeline display
  const sorted = [...data].sort(
    (a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime()
  );

  // Calculate durations between transitions
  const blocks = sorted.map((entry, idx) => {
    const startTime = new Date(entry.timestamp).getTime();
    const endTime =
      idx < sorted.length - 1
        ? new Date(sorted[idx + 1].timestamp).getTime()
        : Date.now();
    const durationMs = endTime - startTime;
    const durationMins = Math.floor(durationMs / 60000);

    return {
      regime: entry.regime,
      timestamp: entry.timestamp,
      color: REGIME_COLORS[entry.regime] || REGIME_COLORS.UNKNOWN,
      durationMins,
      adx: entry.adx,
      atr: entry.atr,
      previous_regime: entry.previous_regime,
    };
  });

  const totalDuration = blocks.reduce((sum, b) => sum + b.durationMins, 0);

  return (
    <div
      style={{
        background: "var(--surface-2)",
        border: "1px solid var(--border)",
        borderRadius: "8px",
        overflow: "hidden",
      }}
    >
      <div
        style={{
          padding: "12px 16px",
          borderBottom: "1px solid var(--border)",
          fontWeight: 600,
          fontSize: "13px",
          color: "var(--text-2)",
        }}
      >
        Regime Timeline (Last 24 Hours)
      </div>
      <div style={{ padding: "20px 16px" }}>
        <div
          style={{
            display: "flex",
            height: "40px",
            borderRadius: "6px",
            overflow: "hidden",
            border: "1px solid var(--border)",
            position: "relative",
          }}
        >
          {blocks.map((block, idx) => {
            const widthPercent = (block.durationMins / totalDuration) * 100;
            return (
              <div
                key={idx}
                onMouseEnter={() => setHoveredIdx(idx)}
                onMouseLeave={() => setHoveredIdx(null)}
                style={{
                  width: `${widthPercent}%`,
                  background: block.color,
                  cursor: "pointer",
                  position: "relative",
                  transition: "opacity 0.2s",
                  opacity: hoveredIdx === idx ? 0.8 : 1,
                }}
              >
                {hoveredIdx === idx && (
                  <div
                    style={{
                      position: "absolute",
                      bottom: "100%",
                      left: "50%",
                      transform: "translateX(-50%)",
                      marginBottom: "8px",
                      background: "var(--surface)",
                      border: "1px solid var(--border)",
                      borderRadius: "6px",
                      padding: "8px 12px",
                      fontSize: "11px",
                      color: "var(--text-2)",
                      whiteSpace: "nowrap",
                      zIndex: 10,
                      boxShadow: "0 2px 8px rgba(0,0,0,0.15)",
                    }}
                  >
                    <div style={{ fontWeight: 600, marginBottom: "4px" }}>
                      {block.regime.replace(/_/g, " ")}
                    </div>
                    <div style={{ color: "var(--text-3)" }}>
                      Duration: {formatDuration(block.durationMins)}
                    </div>
                    <div style={{ color: "var(--text-3)" }}>
                      {new Date(block.timestamp).toLocaleTimeString("en-IN", {
                        hour: "2-digit",
                        minute: "2-digit",
                      })}
                    </div>
                    {block.adx !== null && (
                      <div style={{ color: "var(--text-3)" }}>
                        ADX: {block.adx.toFixed(1)}
                      </div>
                    )}
                    {block.atr !== null && (
                      <div style={{ color: "var(--text-3)" }}>
                        ATR: {block.atr.toFixed(2)}
                      </div>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>

        {/* Legend */}
        <div
          style={{
            display: "flex",
            flexWrap: "wrap",
            gap: "12px",
            marginTop: "16px",
            fontSize: "11px",
          }}
        >
          {Object.entries(REGIME_COLORS).map(([regime, color]) => (
            <div key={regime} style={{ display: "flex", alignItems: "center", gap: "6px" }}>
              <div
                style={{
                  width: "12px",
                  height: "12px",
                  background: color,
                  borderRadius: "2px",
                }}
              />
              <span style={{ color: "var(--text-3)" }}>
                {regime.replace(/_/g, " ")}
              </span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function formatDuration(minutes: number): string {
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  const mins = minutes % 60;
  return `${hours}h ${mins}m`;
}
