# Frontend audit (1 Nov 2026)

`try12/frontend`, Next.js App Router (14.2 at audit time, now 16.4; see Security), 8 routes: Market, Trading, Backtest, Backtest run,
Compare, Market study, Regime, not-found.

## Health

| Check | Result |
|---|---|
| `tsc --noEmit` | clean |
| `next lint` (`npm run lint` = `eslint .` since Next 16) | no warnings or errors |
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
   15.5+ remained (including Image Optimizer RCE and a Windows-only RCE). Mitigations: the app
   uses no `next/image`, Server Actions, middleware or rewrites; `images.unoptimized` is
   set; `next dev` binds to 127.0.0.1; Docker publishes the frontend on 127.0.0.1:3000 only.

   **Follow-up done (1 Nov 2026): Next 14.2.35 → 16.4.3, React 18 → 19.3.1.** 15.5.28 (the
   latest 15.5) was tried first, but `npm audit --omit=dev` still flagged it: it bundles
   postcss 8.4.31 (GHSA-qx2v-qp2m-jg93 and three sourceMappingURL file-read advisories), and
   only Next 16.4 ships the fixed postcss 8.5.23. `npm audit --omit=dev` now reports 0
   vulnerabilities. Changes:
   - `next` 16.4.3, `react`/`react-dom` 19.3.1 (exact pins), `@types/react*` ^19.3.0,
     `eslint-config-next` 16.4.3, `eslint` ^9.39.5; `lucide-react` ^0.400.0 (first release
     whose peer range includes React 19; same icon set).
   - `app/backtest/[runId]/page.tsx`: `params` is a Promise (async Server Component).
   - `next lint` is gone in Next 16: `.eslintrc.json` → `eslint.config.mjs` (flat config,
     `eslint-config-next/core-web-vitals`); `npm run lint` runs `eslint .`. The React Compiler
     rules that eslint-plugin-react-hooks 7 adds (`set-state-in-effect`, `refs`,
     `preserve-manual-memoization`, 14 hits) are off: the app does not use the compiler and
     the guide advises against adopting it just to upgrade. `rules-of-hooks` and
     `exhaustive-deps` are unchanged.
   - `tsconfig.json`: `jsx: react-jsx` and `.next/dev/types` (set by `next build`).
   - `<html data-scroll-behavior="smooth">`: Next 16 no longer forces instant scroll on
     route changes, and `globals.css` sets smooth scrolling; the attribute keeps the old
     behaviour.
   - `next build` uses Turbopack by default (no custom webpack config, so nothing to port).
   - Nav bar: at 375 px the five links (Regime was added last) overflowed the page by
     ~170 px; the nav now scrolls inside its own strip like the status bar.
   No `useRef()` without an argument, `JSX.` namespace, `cookies()`/`headers()`, middleware,
   parallel routes or fetch caching in the app, so nothing else needed migrating. The
   `@next/codemod` dry run could not be downloaded (registry DNS failures); `next build`
   type-checks page props against Next 16's types instead. Kept: `images.unoptimized`,
   `next dev -H 127.0.0.1`, Docker on 127.0.0.1:3000.
   Verified: `tsc --noEmit`, `npm run lint`, `next build` clean; with the backend down every
   route (/, /trading, /backtest, /backtest/[runId], /backtest/compare, /regime, /study) shows
   "Backend unreachable" with Retry at 375 px, page scrollWidth = 375, and the console has
   only the expected connection-refused network errors (no React or hydration errors).

Verified in the browser against a mock of the changed endpoints whose responses come from
the real backend functions. The real backend was not started, to avoid a broker login.
