"use client";
import { RegimePerformance } from "@/components/regime/RegimePerformance";
import { RegimeTimeline } from "@/components/regime/RegimeTimeline";

export default function RegimePage() {
  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        gap: "16px",
        padding: "24px",
        background: "var(--surface)",
        minHeight: "calc(100vh - 68px)", // account for StatusBar + NavBar
      }}
    >
      <h1
        style={{
          fontSize: "20px",
          fontWeight: 600,
          color: "var(--text-1)",
          marginBottom: "8px",
        }}
      >
        Regime Analysis Dashboard
      </h1>

      <RegimePerformance />
      <RegimeTimeline />
    </div>
  );
}
