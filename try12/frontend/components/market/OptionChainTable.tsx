"use client";
import { memo, useEffect, useMemo, useRef } from "react";
import { useMarketStore, selectTick } from "@/lib/store";
import { useOptionChain } from "@/hooks/useOptionChain";
import { useOptionChainFeed } from "@/hooks/useTickFeed";
import { formatPrice, formatNumber, cn } from "@/lib/utils";
import { Panel } from "@/components/ui/Panel";
import { DataSurface } from "@/components/ui/DataSurface";
import { TableSkeleton } from "@/components/ui/Skeleton";
import { Badge } from "@/components/ui/Badge";
import { PriceText } from "@/components/ui/PriceText";
import type { OptionStrike } from "@/types/market";

export const STRIKE_STEP: Record<"NIFTY" | "BANKNIFTY", number> = { NIFTY: 50, BANKNIFTY: 100 };

export function atmStrikeFor(underlying: "NIFTY" | "BANKNIFTY", spot: number | null | undefined): number {
  const step = STRIKE_STEP[underlying];
  return spot && spot > 0 ? Math.round(spot / step) * step : 0;
}

interface StrikeRowProps {
  strike:   number;
  ce?:      OptionStrike;
  pe?:      OptionStrike;
  atm:      number;
  selected: string | null;
  onSelect: (token: string) => void;
}

function LtpButton({ leg, ltp, itm, selected, onSelect, align }: {
  leg?: OptionStrike; ltp: number | undefined; itm: boolean; selected: boolean;
  onSelect: (token: string) => void; align: "left" | "right";
}) {
  if (!leg) return <span className="text-text-3">—</span>;
  return (
    <button
      type="button"
      onClick={() => onSelect(leg.token)}
      aria-pressed={selected}
      aria-label={`Chart ${leg.symbol}${ltp ? `, LTP ${formatPrice(ltp)}` : ""}`}
      className={cn(
        "w-full rounded px-1.5 py-0.5 font-mono tabular-nums hover:bg-accent/15",
        align === "right" ? "text-right" : "text-left",
        selected ? "bg-accent/25 text-text-1" : leg.option_type === "CE" ? (itm ? "text-text-2" : "text-green") : itm ? "text-text-2" : "text-red",
      )}
    >
      {ltp ? formatPrice(ltp) : "—"}
    </button>
  );
}

// Scalar per-leg selectors (E11): each returns the stored Tick reference, so a row re-renders only
// when its own CE/PE tick changes — never an object literal (new object per call → getSnapshot loop).
const StrikeRow = memo(function StrikeRow({ strike, ce, pe, atm, selected, onSelect }: StrikeRowProps) {
  const ceTick = useMarketStore(selectTick(ce?.token));
  const peTick = useMarketStore(selectTick(pe?.token));
  const isATM    = strike === atm;
  const isITM_CE = atm > 0 && strike < atm;
  const isITM_PE = atm > 0 && strike > atm;

  const ceLtp = ceTick?.ltp ?? ce?.ltp;
  const peLtp = peTick?.ltp ?? pe?.ltp;
  const ceOI  = ceTick?.oi  ?? ce?.oi;
  const peOI  = peTick?.oi  ?? pe?.oi;
  const ceVol = ceTick?.volume ?? ce?.volume;
  const peVol = peTick?.volume ?? pe?.volume;

  return (
    <tr data-strike={strike} className={cn(isATM && "bg-accent/10")}>
      <td className={cn("num !p-0", isITM_CE && "bg-green/5")}>
        <LtpButton leg={ce} ltp={ceLtp} itm={isITM_CE} selected={!!ce && selected === ce.token} onSelect={onSelect} align="right" />
      </td>
      <td className={cn("num font-mono", isITM_CE && "bg-green/5")}>{ce?.iv ? `${ce.iv.toFixed(1)}%` : "—"}</td>
      <td className={cn("num font-mono", isITM_CE && "bg-green/5")}>{formatNumber(ceOI)}</td>
      <td className={cn("num font-mono", isITM_CE && "bg-green/5")}>{ceVol ? formatNumber(ceVol) : "—"}</td>
      {/* `!static !normal-case !bg-transparent` undo the sticky .data-table th header styling for a row header */}
      <th scope="row" className={cn("!static whitespace-nowrap !bg-transparent !text-center font-mono !text-[12px] !font-bold !normal-case !tracking-normal", isATM ? "!text-yellow" : "!text-text-1")}>
        {strike.toLocaleString("en-IN")}
        {isATM && <span className="ml-1 text-micro text-yellow">ATM</span>}
      </th>
      <td className={cn("font-mono", isITM_PE && "bg-red/5")}>{peVol ? formatNumber(peVol) : "—"}</td>
      <td className={cn("font-mono", isITM_PE && "bg-red/5")}>{formatNumber(peOI)}</td>
      <td className={cn("font-mono", isITM_PE && "bg-red/5")}>{pe?.iv ? `${pe.iv.toFixed(1)}%` : "—"}</td>
      <td className={cn("!p-0", isITM_PE && "bg-red/5")}>
        <LtpButton leg={pe} ltp={peLtp} itm={isITM_PE} selected={!!pe && selected === pe.token} onSelect={onSelect} align="left" />
      </td>
    </tr>
  );
});

export function OptionChainTable() {
  const selectedUnderlying = useMarketStore((s) => s.selectedUnderlying);
  const setUnderlying      = useMarketStore((s) => s.setUnderlying);
  const setExpiry          = useMarketStore((s) => s.setExpiry);
  const selectedToken      = useMarketStore((s) => s.selectedToken);
  const setSelectedToken   = useMarketStore((s) => s.setSelectedToken);
  const { query, chain } = useOptionChain();
  useOptionChainFeed(selectedUnderlying);

  const atm = atmStrikeFor(selectedUnderlying, chain?.underlying_ltp);

  const strikeMap = useMemo(() => {
    const map: Record<number, { ce?: OptionStrike; pe?: OptionStrike }> = {};
    for (const s of Array.isArray(chain?.strikes) ? chain.strikes : []) {
      if (!map[s.strike]) map[s.strike] = {};
      if (s.option_type === "CE") map[s.strike].ce = s;
      else                        map[s.strike].pe = s;
    }
    return map;
  }, [chain?.strikes]);

  const sortedStrikes = useMemo(
    () => Object.keys(strikeMap).map(Number).sort((a, b) => a - b),
    [strikeMap],
  );

  // Centre the ATM row once per underlying/expiry (the chain spans ~±60 strikes; the top is deep ITM).
  const scrollRef = useRef<HTMLDivElement>(null);
  const centredKeyRef = useRef<string | null>(null);
  const centreKey = `${selectedUnderlying}|${chain?.expiry ?? ""}`;
  useEffect(() => {
    const box = scrollRef.current;
    if (!box || atm <= 0 || centredKeyRef.current === centreKey) return;
    const row = box.querySelector<HTMLTableRowElement>(`tr[data-strike="${atm}"]`);
    if (!row) return;
    const offset = row.getBoundingClientRect().top - box.getBoundingClientRect().top + box.scrollTop;
    box.scrollTop = Math.max(0, offset - box.clientHeight / 2 + row.offsetHeight / 2);
    centredKeyRef.current = centreKey;
    // dataUpdatedAt: re-check after each poll in case the table mounted after the first data render.
  }, [atm, centreKey, sortedStrikes.length, query.dataUpdatedAt]);

  const switchUnderlying = (u: "NIFTY" | "BANKNIFTY") => {
    if (u === selectedUnderlying) return;
    // A strike token and expiry belong to one underlying — reset both so the chart returns to the
    // new index and the nearest expiry is re-selected.
    setSelectedToken(null);
    setExpiry(null);
    setUnderlying(u);
  };

  return (
    <Panel
      title="Option chain"
      meta={
        <div role="group" aria-label="Underlying" className="ml-2 inline-flex rounded-lg border border-border bg-surface-2 p-0.5">
          {(["NIFTY", "BANKNIFTY"] as const).map((u) => (
            <button
              key={u}
              type="button"
              aria-pressed={selectedUnderlying === u}
              onClick={() => switchUnderlying(u)}
              className={cn(
                "h-6 rounded-md px-2.5 text-table font-semibold",
                selectedUnderlying === u ? "bg-accent/30 text-text-1" : "text-text-2 hover:text-text-1",
              )}
            >
              {u}
            </button>
          ))}
        </div>
      }
      actions={
        chain && (
          <div className="flex items-center gap-3 font-mono text-table text-text-2">
            <span>Spot <PriceText value={chain.underlying_ltp} className="font-semibold text-text-1" /></span>
            {chain.pcr != null && (
              <span>PCR <span className={cn("font-semibold", chain.pcr > 1 ? "text-green" : "text-red")}>{chain.pcr.toFixed(3)}</span></span>
            )}
            <Badge intent="neutral" caps={false}>{chain.expiry ?? "—"}</Badge>
          </div>
        )
      }
      bodyClassName="p-3"
    >
      <DataSurface
        query={query}
        isEmpty={(d) => !Array.isArray(d.strikes) || d.strikes.length === 0}
        emptyTitle="No strikes returned for this expiry"
        emptyHint="The instrument master may not have synced the selected expiry yet."
        skeleton={<TableSkeleton rows={10} cols={9} />}
      >
        {() => (
          <div ref={scrollRef} className="-mx-3 -mt-3 max-h-[60vh] overflow-y-auto" data-testid="option-chain">
            <table className="data-table">
              <caption className="sr-only">
                {selectedUnderlying} option chain{chain?.expiry ? `, expiry ${chain.expiry}` : ""}: calls left, puts right; choose an LTP to chart that contract
              </caption>
              <thead>
                <tr>
                  <th scope="col" className="num !text-green">CE LTP</th>
                  <th scope="col" className="num">IV</th>
                  <th scope="col" className="num">OI</th>
                  <th scope="col" className="num">Vol</th>
                  <th scope="col" className="!text-center !text-text-1">Strike</th>
                  <th scope="col">Vol</th>
                  <th scope="col">OI</th>
                  <th scope="col">IV</th>
                  <th scope="col" className="!text-red">PE LTP</th>
                </tr>
              </thead>
              <tbody>
                {sortedStrikes.map((strike) => (
                  <StrikeRow
                    key={strike}
                    strike={strike}
                    ce={strikeMap[strike].ce}
                    pe={strikeMap[strike].pe}
                    atm={atm}
                    selected={selectedToken}
                    onSelect={setSelectedToken}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </DataSurface>
    </Panel>
  );
}
