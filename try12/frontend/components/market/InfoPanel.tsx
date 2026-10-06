"use client";
import { useMarketStore, selectTick } from "@/lib/store";
import { formatNumber, formatPrice } from "@/lib/utils";
import { useHydrated } from "@/hooks/useHydrated";
import { useTickFeed } from "@/hooks/useTickFeed";
import { Panel } from "@/components/ui/Panel";
import { EmptyState } from "@/components/ui/EmptyState";
import { Badge } from "@/components/ui/Badge";

// Selected-contract details. Every value is from the API (chain snapshot) or the live tick; there
// is no client-side estimate — Delta shows the API's value or "—" (the old estimateDelta is gone).
export function InfoPanel() {
  const hydrated      = useHydrated();
  const selectedToken = useMarketStore((s) => s.selectedToken);
  const optionChain   = useMarketStore((s) => s.optionChain);
  const tick          = useMarketStore(selectTick(selectedToken));
  useTickFeed(selectedToken);

  const inst = selectedToken && Array.isArray(optionChain?.strikes)
    ? optionChain.strikes.find((s) => s.token === selectedToken)
    : undefined;

  if (!hydrated || !selectedToken) {
    return (
      <Panel title="Instrument">
        <EmptyState title="No contract selected" hint="Choose a CE or PE LTP in the option chain to chart it and see its details here." />
      </Panel>
    );
  }

  const bid = tick?.best_bid, ask = tick?.best_ask;
  const spread = typeof bid === "number" && typeof ask === "number" && ask > 0 && bid > 0 ? ask - bid : null;
  const ltp = tick?.ltp ?? inst?.ltp;

  const rows: [string, string][] = [
    ["LTP",       formatPrice(ltp)],
    ["Bid / Ask", typeof bid === "number" || typeof ask === "number" ? `${formatPrice(bid)} / ${formatPrice(ask)}` : "—"],
    ["Spread",    spread !== null && spread >= 0 ? formatPrice(spread) : "—"],
    ["OI",        formatNumber(tick?.oi ?? inst?.oi)],
    ["Volume",    formatNumber(tick?.volume ?? inst?.volume)],
    ["Strike",    inst?.strike != null ? inst.strike.toLocaleString("en-IN") : "—"],
    ["Expiry",    inst?.expiry ?? "—"],
    ["IV",        typeof inst?.iv === "number" && inst.iv > 0 ? `${inst.iv.toFixed(2)}%` : "—"],
    ["Delta",     typeof inst?.delta === "number" ? inst.delta.toFixed(3) : "—"],
  ];

  return (
    <Panel
      title="Instrument"
      meta={inst?.option_type && <Badge intent={inst.option_type === "CE" ? "positive" : "negative"}>{inst.option_type}</Badge>}
      dense
    >
      <div className="mb-2 truncate font-mono text-body font-semibold text-text-1" data-testid="info-symbol">
        {inst?.symbol ?? `Token ${selectedToken}`}
      </div>
      <dl className="divide-y divide-border">
        {rows.map(([label, val]) => (
          <div key={label} className="flex items-center justify-between py-1.5">
            <dt className="text-table text-text-3">{label}</dt>
            <dd className="font-mono text-table tabular-nums text-text-1">{val}</dd>
          </div>
        ))}
      </dl>
      {!tick && <p className="mt-2 text-micro text-text-3">No live tick yet — showing the chain snapshot.</p>}
    </Panel>
  );
}
