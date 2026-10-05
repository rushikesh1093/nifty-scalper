# Implementation Plan

## Problem 1: WebSocket Feed `_WsState` Bug

The backend feed is broken. In `c:\Users\rushi\Downloads\try12fin\try12\backend\app\services\market_data\feed.py`, the class `MarketFeed` uses `_WsState.OPEN` in two places but `_WsState` is never imported or defined, causing a `NameError` at runtime.

## Problem 2: No Regime UI

The backend has regime detection but the frontend has no regime UI. Need to build backend endpoints for current regime and history, plus three frontend components (status badge, performance table, timeline chart) integrated into the StatusBar and a new /regime page.

---

- [ ] 1. **Fix WebSocket feed `_WsState` bug**
      
      Replace undefined `_WsState.OPEN` with `not self._ws.closed` in two locations in feed.py.
      
      **Files:**
      - `c:\Users\rushi\Downloads\try12fin\try12\backend\app\services\market_data\feed.py`
        - Line 234 (is_connected property): change `self._ws.state == _WsState.OPEN` to `not self._ws.closed`
        - Line 291 (_heartbeat_loop method): change `self._ws.state == _WsState.OPEN` to `not self._ws.closed`
      
      **Verify:** 
      ```powershell
      cd c:\Users\rushi\Downloads\try12fin\try12\backend
      python -m py_compile app/services/market_data/feed.py
      # Should exit with code 0 (no syntax errors)
      
      # Start backend and check health endpoint
      curl http://localhost:8000/health
      # Should return JSON with feed_connected field, no NameError in logs
      ```

- [ ] 2. **Add backend regime endpoints**
      
      Add two new endpoints to regime.py: GET /api/regime/current (reads Redis regime:{underlying}:current key, returns regime/since/seconds_in_regime) and GET /api/regime/history (queries regime_states table for last N hours of transitions).
      
      **Files:**
      - `c:\Users\rushi\Downloads\try12fin\try12\backend\app\api\routes\regime.py`
        - Add `get_current_regime(underlying: str)` endpoint: reads `f"regime:{underlying}:current"` from Redis, parses JSON, calculates seconds_in_regime from timestamp
        - Add `get_regime_history(underlying: str, hours: int)` endpoint: queries `RegimeStateModel` table filtered by underlying and timestamp >= now - hours, returns list ordered by timestamp DESC
      
      **Verify:**
      ```powershell
      curl "http://localhost:8000/api/regime/current?underlying=NIFTY"
      # Should return: {"regime": "...", "since": "...", "seconds_in_regime": 123}
      
      curl "http://localhost:8000/api/regime/history?underlying=NIFTY&hours=24"
      # Should return: [{regime, timestamp, adx, atr, previous_regime}, ...]
      ```

- [ ] 3. **Add frontend API functions and types**
      
      Add TypeScript interfaces for regime API responses and corresponding API client functions.
      
      **Files:**
      - Create `c:\Users\rushi\Downloads\try12fin\try12\frontend\types\market.ts` (if missing) or add to existing types:
        - `RegimeStatus` interface: `{regime: string; since: string | null; seconds_in_regime: number}`
        - `RegimeHistoryEntry` interface: `{regime: string; timestamp: string; adx: number | null; atr: number | null; previous_regime: string | null}`
      - `c:\Users\rushi\Downloads\try12fin\try12\frontend\lib\api.ts`:
        - Add `getCurrentRegime(underlying = "NIFTY")` function
        - Add `getRegimeHistory(underlying = "NIFTY", hours = 24)` function
      
      **Verify:** TypeScript compilation succeeds
      ```powershell
      cd c:\Users\rushi\Downloads\try12fin\try12\frontend
      npx tsc --noEmit
      # Should exit with code 0
      ```

- [ ] 4. **Create RegimeStatus component**
      
      Create a compact regime badge component that polls the current regime every 5s and displays regime name (color-coded) plus time-in-regime counter.
      
      **Files:**
      - Create `c:\Users\rushi\Downloads\try12fin\try12\frontend\components\regime\RegimeStatus.tsx`:
        - "use client" component
        - useQuery with `getCurrentRegime("NIFTY")`, refetchInterval: 5000
        - Display regime name as colored badge (green for TRENDING_*, yellow for SIDEWAYS_CHOP/VWAP_MAGNET, red for EXPIRY_DAY/REGIME_TRANSITION, gray for UNKNOWN)
        - Display seconds_in_regime as MM:SS or HH:MM:SS counter
        - Use inline styles with CSS vars (var(--green), var(--red), etc.) following StatusBar pattern
      
      **Verify:** Component compiles, displays in isolation

- [ ] 5. **Create RegimePerformance component**
      
      Create a table component showing regime performance stats (regime, strategy, trades, win rate, P&L).
      
      **Files:**
      - Create `c:\Users\rushi\Downloads\try12fin\try12\frontend\components\regime\RegimePerformance.tsx`:
        - "use client" component
        - useQuery fetching existing `/api/regime/performance` endpoint, refetchInterval: 30000
        - Table with columns: Regime, Strategy, Total Trades, Win Rate (%), Total P&L, Avg P&L
        - Color-code P&L (green positive, red negative)
        - Use formatNumber/formatPrice from @/lib/utils
        - Inline styles with CSS vars
      
      **Verify:** Component compiles and renders table when data available

- [ ] 6. **Create RegimeTimeline component**
      
      Create a horizontal timeline chart showing regime changes over the last 24 hours.
      
      **Files:**
      - Create `c:\Users\rushi\Downloads\try12fin\try12\frontend\components\regime\RegimeTimeline.tsx`:
        - "use client" component
        - useQuery with `getRegimeHistory("NIFTY", 24)`, refetchInterval: 60000
        - Simple div-based horizontal bar visualization (no chart library needed)
        - Each regime period = colored horizontal bar, width proportional to duration
        - Show regime name and duration on hover
        - Inline styles with CSS vars, color-code by regime type
      
      **Verify:** Component compiles and displays timeline bars

- [ ] 7. **Integrate RegimeStatus into StatusBar**
      
      Add the RegimeStatus badge to the StatusBar as a new section between Broker and Clock.
      
      **Files:**
      - `c:\Users\rushi\Downloads\try12fin\try12\frontend\components\market\StatusBar.tsx`:
        - Import RegimeStatus component
        - Add `<div className="flex items-center gap-2 px-3" style={{borderRight: "1px solid var(--border)"}}><RegimeStatus /></div>` after Broker section, before spacer
      
      **Verify:** 
      ```powershell
      # Start frontend, open http://localhost:3000
      # StatusBar should show regime badge with color and counter
      ```

- [ ] 8. **Create /regime dashboard page**
      
      Create a new page that displays RegimePerformance and RegimeTimeline stacked vertically.
      
      **Files:**
      - Create `c:\Users\rushi\Downloads\try12fin\try12\frontend\app\regime\page.tsx`:
        - "use client" page component
        - Title: "Regime Analysis Dashboard"
        - Layout: flex column with gap-4, padding p-6
        - Render `<RegimePerformance />` and `<RegimeTimeline />` stacked
        - Use background var(--surface) with border
      
      **Verify:**
      ```powershell
      # Navigate to http://localhost:3000/regime
      # Page loads with performance table and timeline chart
      ```

- [ ] 9. **Add Regime nav link**
      
      Add "Regime" navigation link to the navbar.
      
      **Files:**
      - `c:\Users\rushi\Downloads\try12fin\try12\frontend\components\market\NavBar.tsx`:
        - Add `{ href: "/regime", label: "Regime" }` to LINKS array after the Market Study entry
      
      **Verify:**
      ```powershell
      # Open http://localhost:3000
      # Navbar shows: Market | Trading | Market Study | Regime
      # Click Regime link → navigates to /regime page
      ```

- [ ] 10. **Final integration verification**
      
      Run all verification checks to ensure the complete system works end-to-end.
      
      **Verify:**
      ```powershell
      # Backend running on port 8000, frontend on port 3000
      
      # 1. Feed fix
      curl http://localhost:8000/health
      # feed_connected: true (no errors in backend logs)
      
      # 2. Regime endpoints
      curl "http://localhost:8000/api/regime/current?underlying=NIFTY"
      curl "http://localhost:8000/api/regime/history?underlying=NIFTY&hours=24"
      # Both return valid JSON
      
      # 3. Frontend TypeScript
      cd c:\Users\rushi\Downloads\try12fin\try12\frontend
      npx tsc --noEmit
      # No errors
      
      # 4. Browser checks
      # Open http://localhost:3000
      # - StatusBar shows regime badge with color and live counter
      # - Navbar has Regime link
      # - Click Regime → /regime page loads with table and timeline
      # - Browser console: no errors
      # - Network tab: polling requests every 5s/30s/60s
      ```

---

## Implementation Notes

### Feed Fix Details
- The websockets library v12.0+ provides a `closed` boolean property on WebSocketClientProtocol
- `not ws.closed` is equivalent to checking if state == OPEN but doesn't require importing internal state enums
- This is the simplest fix that aligns with websockets v12+ API

### Regime Endpoints Details
- Redis key: `regime:{underlying}:current` stores serialized RegimeState JSON (TTL=600s)
- DB table: `regime_states` has columns: underlying, regime, timestamp, adx, atr, previous_regime, transition_detected
- Current endpoint calculates `seconds_in_regime` as `(now - state.timestamp).total_seconds()`
- History endpoint uses SQLAlchemy async query with timestamp filter

### Frontend Component Details
- All regime components are "use client" (polling/state)
- Use Tanstack Query `useQuery` with appropriate refetchInterval
- Inline styles with CSS vars maintain visual consistency with existing components
- No external chart libraries needed — timeline is simple div-based visualization
- Color coding:
  - Green: TRENDING_UP, TRENDING_DOWN
  - Yellow: SIDEWAYS_CHOP, VWAP_MAGNET
  - Red: EXPIRY_DAY, REGIME_TRANSITION
  - Gray: UNKNOWN

### Dependencies
- All required dependencies already installed (websockets 12.0, axios, @tanstack/react-query, date-fns)
- No new packages needed
