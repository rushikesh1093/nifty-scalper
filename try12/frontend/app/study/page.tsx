"use client";
import { useEffect, useId, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { AreaChart, Area, LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, ReferenceLine } from "recharts";
import { getDailySnapshots, getIntradayFeatures } from "@/lib/tradingApi";
import { queryKeys } from "@/lib/queryKeys";
import { getChartTheme, type ChartTheme } from "@/lib/chartTheme";
import { formatIst } from "@/lib/time";
import { cn } from "@/lib/utils";
import { PageHeader } from "@/components/ui/PageHeader";
import { Panel } from "@/components/ui/Panel";
import { DataSurface } from "@/components/ui/DataSurface";
import { ChartSkeleton, TableSkeleton } from "@/components/ui/Skeleton";
import { Badge } from "@/components/ui/Badge";
import { PriceText, DeltaText } from "@/components/ui/PriceText";
import type { DailySnapshot, IntradayFeature } from "@/types/study";

type Underlying = "NIFTY" | "BANKNIFTY";
const DAYS = 22;
const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
const fixed = (v: unknown, d: number) => (num(v) === null ? "—" : (v as number).toFixed(d));
const isArr = <T,>(d: T[] | unknown): d is T[] => Array.isArray(d);

/** Theme colours come from CSS variables, which only exist in the browser. */
function useTheme(): ChartTheme | null {
  const [theme, setTheme] = useState<ChartTheme | null>(null);
  useEffect(() => setTheme(getChartTheme()), []);
  return theme;
}

function DailyCharts({ daily, theme }: { daily: DailySnapshot[]; theme: ChartTheme }) {
  const rc = theme.recharts, c = theme.color;
  const gid = `close-${useId().replace(/:/g, "")}`;
  const axisX = { dataKey: "date", stroke: rc.axis.stroke, tick: rc.axis.tick, tickLine: false, axisLine: rc.axis.axisLine, tickFormatter: (v: string) => String(v).slice(5), minTickGap: 16 } as const;
  const axisY = { stroke: rc.axis.stroke, tick: rc.axis.tick, tickLine: false, axisLine: false, width: 56 } as const;
  const tip = { contentStyle: rc.tooltip.contentStyle, labelStyle: rc.tooltip.labelStyle, itemStyle: rc.tooltip.itemStyle } as const;
  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
      <div className="lg:col-span-3">
        <div className="mb-1 text-table text-text-2">Daily close and VWAP at close</div>
        <div style={{ height: 180 }} role="img" aria-label={`Daily close and VWAP for ${daily.length} days`}>
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={daily} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
              <defs>
                <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={c("blue")} stopOpacity={0.25} />
                  <stop offset="100%" stopColor={c("blue")} stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid stroke={rc.grid.stroke} strokeDasharray={rc.grid.strokeDasharray} vertical={false} />
              <XAxis {...axisX} />
              <YAxis {...axisY} domain={["auto", "auto"]} />
              <Tooltip {...tip} />
              <Area type="monotone" dataKey="close" name="Close" stroke={c("blue")} fill={`url(#${gid})`} strokeWidth={2} dot={false} isAnimationActive={false} />
              <Area type="monotone" dataKey="vwap_close" name="VWAP" stroke={c("yellow")} fill="none" strokeWidth={1.5} strokeDasharray="4 2" dot={false} isAnimationActive={false} />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      </div>
      {([
        ["PCR (OI)", "pcr_oi", "accent", 1],
        ["India VIX", "vix", "yellow", null],
        ["ATM IV %", "atm_iv", "green", null],
      ] as const).map(([title, key, token, ref]) => (
        <div key={key}>
          <div className="mb-1 text-table text-text-2">{title}</div>
          {daily.every((d) => num(d[key]) === null) ? (
            // Older snapshots lack these fields; say so instead of drawing an empty plot.
            <div style={{ height: 120 }} className="flex items-center justify-center rounded-md border border-dashed border-border text-table text-text-3">
              Not recorded for these {daily.length} days
            </div>
          ) : (
          <div style={{ height: 120 }} role="img" aria-label={`${title} for ${daily.length} days`}>
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={daily} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
                <CartesianGrid stroke={rc.grid.stroke} strokeDasharray={rc.grid.strokeDasharray} vertical={false} />
                <XAxis {...axisX} />
                <YAxis {...axisY} width={40} domain={["auto", "auto"]} />
                <Tooltip {...tip} />
                {ref !== null && <ReferenceLine y={ref} stroke={c("text-3")} strokeDasharray="4 2" />}
                <Area type="monotone" dataKey={key} name={title} stroke={c(token)} fill={c(token, 0.12)} strokeWidth={1.5} dot={false} isAnimationActive={false} />
              </AreaChart>
            </ResponsiveContainer>
          </div>
          )}
        </div>
      ))}
    </div>
  );
}

const TREND_INTENT: Record<string, "positive" | "negative" | "warning"> = { BULLISH: "positive", BEARISH: "negative" };

function DailyTable({ daily }: { daily: DailySnapshot[] }) {
  const rows = [...daily].reverse();
  return (
    <div className="-mx-3 -mt-3 max-h-[420px] overflow-auto" data-testid="study-daily-table">
      <table className="data-table">
        <caption className="sr-only">Daily market snapshots, most recent first</caption>
        <thead>
          <tr>
            <th scope="col">Date</th>
            <th scope="col" className="num">Close</th>
            <th scope="col" className="num">Chg %</th>
            <th scope="col">Trend</th>
            <th scope="col" className="num">VIX</th>
            <th scope="col" className="num">PCR</th>
            <th scope="col" className="num">ATM IV</th>
            <th scope="col" className="num">ORB H</th>
            <th scope="col" className="num">ORB L</th>
            <th scope="col" className="num">VWAP</th>
            <th scope="col" className="num">PDH</th>
            <th scope="col" className="num">PDL</th>
            <th scope="col" className="num">DTE</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((d) => (
            <tr key={d.date}>
              <td className="whitespace-nowrap font-mono text-text-1">{d.date}</td>
              <td className="num"><PriceText value={num(d.close)} className="text-text-1" /></td>
              <td className="num"><DeltaText value={num(d.change_pct)} suffix="%" /></td>
              <td>{d.trend ? <Badge intent={TREND_INTENT[d.trend] ?? "warning"}>{d.trend}</Badge> : <span className="text-text-3">—</span>}</td>
              <td className="num font-mono">{fixed(d.vix, 2)}</td>
              <td className={cn("num font-mono", num(d.pcr_oi) !== null && ((d.pcr_oi as number) > 1 ? "text-green" : "text-red"))}>{fixed(d.pcr_oi, 3)}</td>
              <td className="num font-mono">{fixed(d.atm_iv, 1)}</td>
              <td className="num"><PriceText value={num(d.orb_high)} /></td>
              <td className="num"><PriceText value={num(d.orb_low)} /></td>
              <td className="num"><PriceText value={num(d.vwap_close)} /></td>
              <td className="num"><PriceText value={num(d.pdh)} /></td>
              <td className="num"><PriceText value={num(d.pdl)} /></td>
              <td className="num font-mono">{num(d.dte) ?? "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function IntradayChart({ rows, theme }: { rows: IntradayFeature[]; theme: ChartTheme }) {
  const rc = theme.recharts, c = theme.color;
  const last = rows[rows.length - 1];
  const data = rows.map((r) => ({ ...r, t: formatIst(r.time, { seconds: false }) }));
  return (
    <div className="space-y-3">
      <dl className="grid grid-cols-2 gap-2 sm:grid-cols-5">
        {([
          ["Spot", <PriceText key="s" value={num(last.spot)} />],
          ["VWAP", <PriceText key="v" value={num(last.vwap)} />],
          ["Spot vs VWAP", <DeltaText key="d" value={num(last.spot_vs_vwap)} suffix="%" />],
          ["ATM straddle", <PriceText key="st" value={num(last.straddle)} />],
          ["PCR · RSI", <span key="p" className="font-mono">{fixed(last.pcr, 3)} · {fixed(last.rsi, 1)}</span>],
        ] as const).map(([label, val]) => (
          <div key={label} className="rounded-md border border-border bg-surface-2 px-2.5 py-2">
            <dt className="text-micro uppercase text-text-3">{label}</dt>
            <dd className="text-body text-text-1">{val}</dd>
          </div>
        ))}
      </dl>
      <div style={{ height: 180 }} role="img" aria-label={`Intraday spot and VWAP, ${rows.length} snapshots, last at ${formatIst(last.time, { seconds: false })}`}>
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={data} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
            <CartesianGrid stroke={rc.grid.stroke} strokeDasharray={rc.grid.strokeDasharray} vertical={false} />
            <XAxis dataKey="t" stroke={rc.axis.stroke} tick={rc.axis.tick} tickLine={false} axisLine={rc.axis.axisLine} minTickGap={24} />
            <YAxis stroke={rc.axis.stroke} tick={rc.axis.tick} tickLine={false} axisLine={false} width={56} domain={["auto", "auto"]} />
            <Tooltip contentStyle={rc.tooltip.contentStyle} labelStyle={rc.tooltip.labelStyle} itemStyle={rc.tooltip.itemStyle} />
            <Line type="monotone" dataKey="spot" name="Spot" stroke={c("blue")} strokeWidth={1.5} dot={false} isAnimationActive={false} />
            <Line type="monotone" dataKey="vwap" name="VWAP" stroke={c("yellow")} strokeWidth={1.5} strokeDasharray="4 2" dot={false} isAnimationActive={false} />
          </LineChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}

export default function StudyPage() {
  const [underlying, setUnderlying] = useState<Underlying>("NIFTY");
  const theme = useTheme();

  const daily = useQuery({
    queryKey: queryKeys.studyDaily(underlying, DAYS),
    queryFn: () => getDailySnapshots(underlying, DAYS),
    refetchInterval: 60_000,
  });
  const intraday = useQuery({
    queryKey: queryKeys.studyIntraday(underlying),
    queryFn: () => getIntradayFeatures(underlying),
    refetchInterval: 30_000,
  });

  const dailyEmpty = (d: DailySnapshot[]) => !isArr(d) || d.length === 0;

  return (
    <div className="mx-auto max-w-[2400px] space-y-4 p-4">
      <PageHeader
        title="Market study"
        description={`Daily snapshots and today's intraday features collected for ${underlying}.`}
        actions={
          <div role="group" aria-label="Underlying" className="inline-flex rounded-lg border border-border bg-surface-2 p-0.5">
            {(["NIFTY", "BANKNIFTY"] as const).map((u) => (
              <button
                key={u}
                type="button"
                aria-pressed={underlying === u}
                onClick={() => setUnderlying(u)}
                className={cn("h-7 rounded-md px-3 text-table font-semibold", underlying === u ? "bg-accent/30 text-text-1" : "text-text-2 hover:text-text-1")}
              >
                {u}
              </button>
            ))}
          </div>
        }
      />

      <Panel title="Daily trends" meta={daily.data && isArr(daily.data) ? <Badge intent="neutral">{daily.data.length} days</Badge> : undefined}>
        <DataSurface
          query={daily}
          isEmpty={dailyEmpty}
          emptyTitle="No daily snapshots yet"
          emptyHint="The study collector writes one snapshot per trading day after the close."
          skeleton={<ChartSkeleton height={330} />}
        >
          {(d) => (theme ? <DailyCharts daily={d} theme={theme} /> : <ChartSkeleton height={330} />)}
        </DataSurface>
      </Panel>

      <Panel title="Today intraday" meta={<Badge intent="neutral">{underlying}</Badge>}>
        <DataSurface
          query={intraday}
          isEmpty={(d) => !isArr(d) || d.length === 0}
          emptyTitle="No intraday snapshots today"
          emptyHint="Intraday features are recorded during market hours (09:15–15:30 IST)."
          skeleton={<ChartSkeleton height={240} />}
        >
          {(rows) => (theme ? <IntradayChart rows={rows} theme={theme} /> : <ChartSkeleton height={240} />)}
        </DataSurface>
      </Panel>

      <Panel title="Daily summary" bodyClassName="p-3">
        <DataSurface
          query={daily}
          isEmpty={dailyEmpty}
          emptyTitle="No daily snapshots yet"
          emptyHint="Rows appear after the first collected trading day."
          skeleton={<TableSkeleton rows={6} cols={13} />}
          hideFreshness
        >
          {(d) => <DailyTable daily={d} />}
        </DataSurface>
      </Panel>
    </div>
  );
}
