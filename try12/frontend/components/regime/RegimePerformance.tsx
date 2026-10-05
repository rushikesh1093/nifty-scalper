"use client";
import { useQuery } from "@tanstack/react-query";
import axios from "axios";
import { formatNumber, formatPrice } from "@/lib/utils";

const BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api/v1";
const api = axios.create({ baseURL: BASE, timeout: 10000 });

interface RegimePerformanceData {
  regime:        string;
  underlying:    string;
  strategy_name: string;
  total_trades:  number;
  win_rate:      number;
  total_pnl:     number;
  avg_pnl:       number | null;
}

async function getRegimePerformance(): Promise<RegimePerformanceData[]> {
  const res = await api.get<RegimePerformanceData[]>("/regime/performance");
  return res.data;
}

export function RegimePerformance() {
  const { data, isLoading } = useQuery({
    queryKey: ["regime-performance"],
    queryFn: getRegimePerformance,
    refetchInterval: 30000, // 30s
  });

  if (isLoading) {
    return (
      <div style={{ padding: "16px", color: "var(--text-3)", fontSize: "12px" }}>
        Loading performance data...
      </div>
    );
  }

  if (!data || data.length === 0) {
    return (
      <div style={{ padding: "16px", color: "var(--text-3)", fontSize: "12px" }}>
        No performance data available.
      </div>
    );
  }

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
        Regime Performance Statistics
      </div>
      <div style={{ overflowX: "auto" }}>
        <table style={{ width: "100%", borderCollapse: "collapse" }}>
          <thead>
            <tr style={{ background: "var(--surface)", borderBottom: "1px solid var(--border)" }}>
              <th style={thStyle}>Regime</th>
              <th style={thStyle}>Strategy</th>
              <th style={thStyle}>Total Trades</th>
              <th style={thStyle}>Win Rate</th>
              <th style={thStyle}>Total P&L</th>
              <th style={thStyle}>Avg P&L</th>
            </tr>
          </thead>
          <tbody>
            {data.map((row, idx) => (
              <tr
                key={idx}
                style={{
                  borderBottom: idx < data.length - 1 ? "1px solid var(--border)" : "none",
                }}
              >
                <td style={tdStyle}>{row.regime.replace(/_/g, " ")}</td>
                <td style={tdStyle}>{row.strategy_name}</td>
                <td style={{ ...tdStyle, textAlign: "center" }}>{row.total_trades}</td>
                <td style={{ ...tdStyle, textAlign: "center" }}>
                  {row.win_rate.toFixed(1)}%
                </td>
                <td
                  style={{
                    ...tdStyle,
                    textAlign: "right",
                    color: row.total_pnl >= 0 ? "var(--green)" : "var(--red)",
                    fontWeight: 600,
                  }}
                >
                  ₹{formatPrice(row.total_pnl)}
                </td>
                <td
                  style={{
                    ...tdStyle,
                    textAlign: "right",
                    color:
                      row.avg_pnl && row.avg_pnl >= 0 ? "var(--green)" : "var(--red)",
                  }}
                >
                  {row.avg_pnl !== null ? `₹${formatPrice(row.avg_pnl)}` : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

const thStyle: React.CSSProperties = {
  padding: "8px 12px",
  textAlign: "left",
  fontSize: "11px",
  fontWeight: 600,
  color: "var(--text-3)",
  textTransform: "uppercase",
  letterSpacing: "0.03em",
};

const tdStyle: React.CSSProperties = {
  padding: "10px 12px",
  fontSize: "12px",
  color: "var(--text-2)",
};
