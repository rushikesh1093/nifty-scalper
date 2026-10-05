import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export function formatPrice(v: number | null | undefined, decimals = 2): string {
  if (v == null) return "—";
  return v.toLocaleString("en-IN", { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
}

export function formatNumber(v: number | null | undefined): string {
  if (v == null) return "—";
  if (v >= 1_00_00_000) return `${(v / 1_00_00_000).toFixed(2)}Cr`;
  if (v >= 1_00_000)    return `${(v / 1_00_000).toFixed(2)}L`;
  if (v >= 1_000)       return `${(v / 1_000).toFixed(1)}K`;
  return v.toString();
}

export function priceDiff(current: number, prev: number): number {
  return prev === 0 ? 0 : ((current - prev) / prev) * 100;
}

export function isMarketOpen(): boolean {
  const now = new Date();
  const ist = new Date(now.toLocaleString("en-US", { timeZone: "Asia/Kolkata" }));
  const h = ist.getHours();
  const m = ist.getMinutes();
  const mins = h * 60 + m;
  const day = ist.getDay(); // 0=Sun, 6=Sat
  if (day === 0 || day === 6) return false;
  return mins >= 9 * 60 + 15 && mins < 15 * 60 + 30;
}
