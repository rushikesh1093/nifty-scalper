"use client";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { getDailySnapshots, getIntradayFeatures } from "@/lib/tradingApi";
import { formatPrice, cn } from "@/lib/utils";
import { AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, ReferenceLine } from "recharts";

export default function StudyPage() {
  const [underlying, setUnderlying] = useState<"NIFTY"|"BANKNIFTY">("NIFTY");

  const { data: daily = [] } = useQuery({
    queryKey:       ["daily-snapshots", underlying],
    queryFn:        () => getDailySnapshots(underlying, 22),
    refetchInterval: 60000,
  });

  const { data: intraday = [] } = useQuery({
    queryKey:       ["intraday-features", underlying],
    queryFn:        () => getIntradayFeatures(underlying),
    refetchInterval: 30000,
  });

  return (
    <div className="p-4 max-w-[1800px] mx-auto space-y-4">
      {/* Header */}
      <div className="flex items-center gap-4">
        <h1 className="text-lg font-bold text-white">Market Study — {underlying}</h1>
        <div className="flex gap-2">
          {(["NIFTY","BANKNIFTY"] as const).map(u => (
            <button key={u} onClick={() => setUnderlying(u)}
              className={cn("px-3 py-1 rounded text-sm font-medium",
                underlying === u ? "bg-accent text-white" : "text-gray-500 hover:text-gray-300"
              )}
            >{u}</button>
          ))}
        </div>
        <span className="text-xs text-gray-600 ml-auto">{daily.length} days collected</span>
      </div>

      {/* Price + VWAP chart */}
      <div className="bg-surface border border-border rounded-xl p-4">
        <div className="text-xs text-gray-500 mb-2">Daily Close + VWAP at Close</div>
        <ResponsiveContainer width="100%" height={180}>
          <AreaChart data={daily}>
            <defs>
              <linearGradient id="closeGrad" x1="0" y1="0" x2="0" y2="1">
                <stop offset="5%" stopColor="#3b82f6" stopOpacity={0.2}/>
                <stop offset="95%" stopColor="#3b82f6" stopOpacity={0}/>
              </linearGradient>
            </defs>
            <CartesianGrid strokeDasharray="3 3" stroke="#2a2d3a"/>
            <XAxis dataKey="date" tick={{fontSize:9,fill:"#6b7280"}} tickLine={false} axisLine={false} tickFormatter={v=>v.slice(5)}/>
            <YAxis tick={{fontSize:9,fill:"#6b7280"}} tickLine={false} axisLine={false} domain={["auto","auto"]}/>
            <Tooltip contentStyle={{background:"#1a1d27",border:"1px solid #2a2d3a",borderRadius:8,fontSize:11}}/>
            <Area type="monotone" dataKey="close" stroke="#3b82f6" fill="url(#closeGrad)" strokeWidth={2} dot={false} name="Close"/>
            <Area type="monotone" dataKey="vwap_close" stroke="#eab308" fill="none" strokeWidth={1.5} strokeDasharray="4 2" dot={false} name="VWAP"/>
          </AreaChart>
        </ResponsiveContainer>
      </div>

      {/* PCR + VIX */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div className="bg-surface border border-border rounded-xl p-4">
          <div className="text-xs text-gray-500 mb-2">PCR (OI)</div>
          <ResponsiveContainer width="100%" height={120}>
            <AreaChart data={daily}>
              <CartesianGrid strokeDasharray="3 3" stroke="#2a2d3a"/>
              <XAxis dataKey="date" tick={{fontSize:9,fill:"#6b7280"}} tickLine={false} axisLine={false} tickFormatter={v=>v.slice(5)}/>
              <YAxis tick={{fontSize:9,fill:"#6b7280"}} tickLine={false} axisLine={false}/>
              <Tooltip contentStyle={{background:"#1a1d27",border:"1px solid #2a2d3a",borderRadius:8,fontSize:11}}/>
              <ReferenceLine y={1} stroke="#6b7280" strokeDasharray="4 2"/>
              <Area type="monotone" dataKey="pcr_oi" stroke="#a78bfa" fill="#a78bfa22" strokeWidth={2} dot={false} name="PCR"/>
            </AreaChart>
          </ResponsiveContainer>
        </div>

        <div className="bg-surface border border-border rounded-xl p-4">
          <div className="text-xs text-gray-500 mb-2">India VIX</div>
          <ResponsiveContainer width="100%" height={120}>
            <AreaChart data={daily}>
              <CartesianGrid strokeDasharray="3 3" stroke="#2a2d3a"/>
              <XAxis dataKey="date" tick={{fontSize:9,fill:"#6b7280"}} tickLine={false} axisLine={false} tickFormatter={v=>v.slice(5)}/>
              <YAxis tick={{fontSize:9,fill:"#6b7280"}} tickLine={false} axisLine={false}/>
              <Tooltip contentStyle={{background:"#1a1d27",border:"1px solid #2a2d3a",borderRadius:8,fontSize:11}}/>
              <Area type="monotone" dataKey="vix" stroke="#f97316" fill="#f9731622" strokeWidth={2} dot={false} name="VIX"/>
            </AreaChart>
          </ResponsiveContainer>
        </div>
      </div>

      {/* Daily table */}
      <div className="bg-surface border border-border rounded-xl overflow-hidden">
        <div className="px-4 py-3 border-b border-border text-sm font-semibold text-gray-400 uppercase tracking-wider">
          Daily Summary ({daily.length} days)
        </div>
        <div className="overflow-auto max-h-80">
          <table className="w-full text-xs">
            <thead className="sticky top-0 bg-surface border-b border-border">
              <tr className="text-gray-500 uppercase">
                {["Date","Close","Chg%","Trend","VIX","PCR","ATM IV","ORB H","ORB L","VWAP","PDH","PDL","DTE"].map(h=>
                  <th key={h} className="px-3 py-2 text-right first:text-left font-medium">{h}</th>
                )}
              </tr>
            </thead>
            <tbody>
              {[...daily].reverse().map((d: any) => (
                <tr key={d.date} className="border-b border-border/30 hover:bg-white/[0.02]">
                  <td className="px-3 py-2 text-gray-400 font-mono">{d.date}</td>
                  <td className="px-3 py-2 text-right font-mono text-white">{formatPrice(d.close)}</td>
                  <td className={cn("px-3 py-2 text-right font-mono", (d.change_pct||0)>=0?"text-green-400":"text-red-400")}>
                    {d.change_pct?.toFixed(2)}%
                  </td>
                  <td className={cn("px-3 py-2 text-right text-[10px]",
                    d.trend==="BULLISH"?"text-green-400":d.trend==="BEARISH"?"text-red-400":"text-yellow-400"
                  )}>{d.trend||"—"}</td>
                  <td className="px-3 py-2 text-right font-mono text-orange-400">{d.vix?.toFixed(2)||"—"}</td>
                  <td className={cn("px-3 py-2 text-right font-mono", (d.pcr_oi||0)>1?"text-green-400":"text-red-400")}>
                    {d.pcr_oi?.toFixed(3)||"—"}
                  </td>
                  <td className="px-3 py-2 text-right font-mono text-purple-400">{d.atm_iv?.toFixed(1)||"—"}</td>
                  <td className="px-3 py-2 text-right font-mono text-gray-400">{formatPrice(d.orb_high)}</td>
                  <td className="px-3 py-2 text-right font-mono text-gray-400">{formatPrice(d.orb_low)}</td>
                  <td className="px-3 py-2 text-right font-mono text-yellow-400">{formatPrice(d.vwap_close)}</td>
                  <td className="px-3 py-2 text-right font-mono text-gray-500">{formatPrice(d.pdh)}</td>
                  <td className="px-3 py-2 text-right font-mono text-gray-500">{formatPrice(d.pdl)}</td>
                  <td className="px-3 py-2 text-right font-mono text-blue-400">{d.dte||"—"}</td>
                </tr>
              ))}
              {daily.length === 0 && (
                <tr><td colSpan={13} className="px-4 py-8 text-center text-gray-600 text-xs">
                  No data yet — will populate after first market day
                </td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
