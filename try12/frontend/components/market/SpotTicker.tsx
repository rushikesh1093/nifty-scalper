"use client";
import { useMarketStore } from "@/lib/store";
import { useTickFeed } from "@/hooks/useTickFeed";
import { formatPrice, priceDiff, cn } from "@/lib/utils";

const NIFTY_TOKEN     = "26000";
const BANKNIFTY_TOKEN = "26009";

function SpotCard({ token, label }: { token: string; label: string }) {
  useTickFeed(token);
  const tick = useMarketStore((s) => s.ticks[token]);

  const change = tick ? priceDiff(tick.ltp, tick.close ?? tick.ltp) : 0;
  const isUp   = change >= 0;
  const color  = isUp ? "var(--green)" : "var(--red)";

  return (
    <div
      className="flex items-center gap-5 px-5 py-3 rounded-xl"
      style={{
        background: "var(--surface)",
        border: "1px solid var(--border)",
        minWidth: 220,
      }}
    >
      <div>
        <div
          className="text-[10px] font-semibold uppercase tracking-widest mb-1"
          style={{ color: "var(--text-3)" }}
        >
          {label}
        </div>
        <div
          className="text-2xl font-bold font-mono leading-none"
          style={{ color }}
        >
          {tick ? formatPrice(tick.ltp) : <span className="shimmer inline-block w-28 h-7" />}
        </div>
      </div>

      <div className="text-right">
        <div className="text-sm font-semibold font-mono" style={{ color }}>
          {change >= 0 ? "▲" : "▼"} {Math.abs(change).toFixed(2)}%
        </div>
        <div className="text-[10px] mt-1 font-mono" style={{ color: "var(--text-3)" }}>
          H {formatPrice(tick?.high)} · L {formatPrice(tick?.low)}
        </div>
      </div>
    </div>
  );
}

export function SpotTicker() {
  const feedConnected = useMarketStore((s) => s.feedConnected);
  const session       = useMarketStore((s) => s.session);

  return (
    <div className="flex items-center gap-3 flex-wrap">
      <SpotCard token={NIFTY_TOKEN}     label="Nifty 50" />
      <SpotCard token={BANKNIFTY_TOKEN} label="Bank Nifty" />

      <div className="flex items-center gap-2 ml-auto">
        <span
          className={cn("w-2 h-2 rounded-full shrink-0", feedConnected && "dot-live")}
          style={{ background: feedConnected ? "var(--green)" : "var(--red)" }}
        />
        <span className="text-[11px]" style={{ color: "var(--text-3)" }}>
          {feedConnected ? "Live" : "Disconnected"}
        </span>
        {session?.client_code && (
          <span
            className="text-[11px] font-medium pl-2"
            style={{ color: "var(--text-3)", borderLeft: "1px solid var(--border)" }}
          >
            {session.client_code}
          </span>
        )}
      </div>
    </div>
  );
}
