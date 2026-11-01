# Frontend audit (1 Nov 2026)

`try12/frontend`, Next.js 14.2 App Router, 8 routes: Market, Trading, Backtest, Backtest run,
Compare, Market study, Regime, not-found.

## Health

| Check | Result |
|---|---|
| `tsc --noEmit` | clean |
| `next lint` | no warnings or errors |
| `next build` | compiles; all routes build |
| Every page with the backend down | each panel shows "Backend unreachable" with Retry; no blank screens, no React errors in the console |
| Phone width (375 px) | no page-level horizontal scroll; the status bar and nav scroll inside their own strips |

## Fixed

1. **The dashboard reported regime blocks the engine no longer applies.** Since
   `regime_gating=False`, the regime runs in shadow, but `/trading/signals/diagnostics`
   still computed "allows trading" from the old map. The regime card could show "Blocks
   trading — strategies are regime-disabled", and the diagnostics listed `REGIME_BLOCKS_ALL`,
   while strategies were trading. The backend now asks one function,
   `strategy_orchestrator.regime_permits_trading`, which answers exactly as the engine
   decides, and reports `regime_gating`. The regime card, status-bar chip and signal
   diagnostics label shadow mode.
2. **Nothing showed the evidence gates.** New **Strategy evidence** panel on the Trading page
   (`/analytics/strategy-evidence`, which now also returns the active policy from
   TradingParams). Per strategy it shows Blocked / Puts only / Skips low vol, its evidence
   status (Collecting evidence / Auto-blocked / Passes gates), and trades, expectancy and
   positive months since its rules last changed. It also shows the daily buy cap and
   whether the regime is gating.
3. **Regime v2 was not visible.** The regime card shows the v2 volatility bucket (with its
   percentile) and opening-range side, labelled "shadow".
4. **Regime page description** no longer claims the regime decides whether strategies trade.
5. **Security.** Next 14.2.3 → **14.2.35** (latest 14.x), plus `npm audit fix` for axios's
   `follow-redirects` and `source-map-js`. Next 14 is end-of-life: advisories fixed only in
   15.5+ remain (including Image Optimizer RCE and a Windows-only RCE). Mitigations: the app
   uses no `next/image`, Server Actions, middleware or rewrites; `images.unoptimized` is now
   set; `next dev` binds to 127.0.0.1; Docker publishes the frontend on 127.0.0.1:3000 only.
   **Recommended follow-up: upgrade to Next 15.5+ (React 19)**, a planned migration.

Verified in the browser against a mock of the changed endpoints whose responses come from
the real backend functions. The real backend was not started, to avoid a broker login.
