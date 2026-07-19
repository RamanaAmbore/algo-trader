# Plan: UI Polish Round 6 + Audit Remediation — NavStrip popups, timestamp, activity, charts, legibility, CC reductions, stale code, perf

## Context

Polish Round 5 shipped 14 UX items. This round adds 13 more UX items from operator review, plus all findings from the CC/perf/stale-code audit: 8 CC reductions, 3 dead-param bugs, 10 dead imports, 1 dead function, 3 perf fixes, and 3 CSS stale rules.

---

## Task

### Item 0 — NavStrip label visibility + gap

**`frontend/src/lib/PositionStrip.svelte`**:

Labels (P/M/C/H) are currently `font-size: var(--fs-xs)` = 0.55rem — too small to read clearly. They also sit flush against the first value span with only the flex `gap: 0.2rem` separating them.

Fixes:
1. `.ps-agg-k`: change `font-size: var(--fs-xs)` → `font-size: var(--fs-sm)` (0.6rem). Keep `font-weight: 700` and `text-transform: uppercase`.
2. `.ps-agg-k`: add `margin-right: 0.25rem` — creates a clear visual gap between the label letter and the first value, making P/M/C/H read as a separate "key" rather than being glued to the numbers.
3. Mobile breakpoints (≤640px, ≤480px): keep label at `var(--fs-xs)` minimum (don't drop to `fs-2xs` — that's 7.5px, unreadable). Change existing `font-size: var(--fs-2xs)` references in the mobile media queries for `.ps-agg-k` to `var(--fs-xs)`.

### Item 1 — NavStrip popup redesign (panel style, left highlight)

**Reference**: `DayPnlBreakup.svelte` is opened when clicking the Day P&L value in the P pill — that panel style (`linear-gradient(180deg, #1c2840, #141e33)`, `border: 1px solid var(--algo-amber-border-soft)`, `box-shadow: 0 8px 32px rgba(0,0,0,0.6)`) is the target aesthetic for ALL NavStrip popups.

**`frontend/src/lib/InfoHint.svelte`**: Add `panel` prop (boolean, default false). When `panel=true`, switch the popup from tooltip mode to panel mode:
- Larger: `min-width: 16rem`, padding `0.75rem 1rem`
- Background: `linear-gradient(180deg, #1c2840 0%, #141e33 100%)`
- Border: `1px solid var(--algo-amber-border-soft)`
- Box shadow: `0 8px 32px rgba(0,0,0,0.6)`
- Border radius: `4px`
- No tooltip arrow/pointer
- Left-side accent: `border-left: 3px solid var(--accent-color, var(--algo-amber))` — accept `accentColor` prop (CSS color string) that maps to `--accent-color` custom property on the panel root. Default = amber.
- Title row at top: if `title` prop is provided, render a header `<div class="ih-panel-title">` with `font-size: var(--fs-md)`, `font-weight: 700`, `color: <accent-color>`, `text-transform: uppercase`, `letter-spacing: 0.05em`, bottom border `1px solid rgba(255,255,255,0.07)`, padding-bottom `0.4rem`, margin-bottom `0.5rem`
- Body: `font-size: var(--fs-sm)`, `color: var(--algo-slate)`, `line-height: 1.5`
- Positioning: still JS-computed viewport-relative; `max-width: min(22rem, calc(100vw - 1.5rem))`

**`frontend/src/lib/PositionStrip.svelte`**:

Replace the 4 existing `<InfoHint popup label="P" text="...">` instances with `panel=true` version:

```svelte
<InfoHint popup panel label="P" accentColor="var(--ps-k-p-color, #fbbf24)"
  title="P — Positions P&L"
  text="Three slots: <b>Day P&L</b> (live ticks − prev-close × net qty, all accounts) / <b>Lifetime P&L</b> (cumulative since open) / <b>Expiry P&L</b> (projected F&O value at expiry via lognormal model)." />
```

Per-pill accent colors:
- P: `#fbbf24` (amber — matches `.ps-k-p`)
- M: `#a78bfa` (violet — matches `.ps-k-m`)
- C: `#38bdf8` (sky — matches `.ps-k-c`)
- H: `#22d3ee` (cyan — matches `.ps-k-h`)

**Also add per-slot InfoHint elements** — each value slot in each pill gets its own `<InfoHint popup panel>` wrapping the slot value span. Use `label=""` (empty) or an icon hint so the slot value itself is the trigger. For each slot:

P pill slots (3):
- Day P&L: `title="Day P&L"` `text="Live tick price − prev close × net qty across all accounts. For new intraday positions (overnight_quantity=0), shows pnl directly."` `accentColor="#fbbf24"`
- Lifetime P&L: `title="Lifetime P&L"` `text="Cumulative P&L since the position was opened. Includes realised + unrealised. Survives intraday cycling."` `accentColor="#fbbf24"`
- Expiry P&L: `title="Expiry P&L"` `text="Projected P&L at expiry using lognormal model. Shows what the F&O portfolio returns if held to expiry at current spot."` `accentColor="#fbbf24"`

M pill slots (2):
- Available: `title="Available Margin"` `text="Cash deployable right now for new orders. = Total margin − used margin. Updated after every order fill."` `accentColor="#a78bfa"`
- Total: `title="Total Margin"` `text="Full collateral picture across all accounts. = Available + margin blocked for open positions."` `accentColor="#a78bfa"`

C pill slots (2):
- CA: `title="Cash Available (CA)"` `text="Live deployable cash. Nets realised P&L + premium debits from long options already paid."` `accentColor="#38bdf8"`
- Total C: `title="Total Cash (C)"` `text="CA + premium tied up in long options (recoverable if closed). Represents full liquid wealth excluding positions."` `accentColor="#38bdf8"`

H pill slots (3):
- Today MTM: `title="Holdings Today MTM"` `text="Live LTP − prev close × qty for all long-term holdings. Intraday MTM only; excludes overnight positions."` `accentColor="#22d3ee"`
- Current value: `title="Holdings Value"` `text="Broker-reported current market value of all holdings across all accounts."` `accentColor="#22d3ee"`
- Lifetime P&L: `title="Holdings Lifetime P&L"` `text="Cumulative P&L since purchase. (current price − avg cost) × qty, all holdings."` `accentColor="#22d3ee"`

Implementation: wrap each `<span class="ps-agg-v ...">` value inside an InfoHint panel trigger. Since the value is numeric and should remain tap-friendly, use `label=""` with `showOnHover=false` — clicking the value opens the panel popup. Keep the existing colored value span inside InfoHint's children/default slot so the number remains visible with its sign color.

The Day P&L slot (P pill, slot 1) already has `onclick={() => _dayPnlBreakupOpen = true}` — keep that. Its InfoHint popup is secondary; the DayPnlBreakup modal opens on click as before.

### Item 2 — Timestamp: IST format for both + mobile tap fix

**IST format**: Both the live clock (`$nowStamp`) and the refresh timestamp (`formatDualTz($lastRefreshAt)`) should display in the same IST-only format: `"HH:MM IST"`. The dual-TZ format (adding EDT) adds visual noise. Use `clientTimestamp($lastRefreshAt)` (from `stores.js:651`) for the refresh timestamp instead of `formatDualTz`. Update the 29 page files:

Replace:
```svelte
<span class="algo-ts algo-ts-data" ...>{formatDualTz($lastRefreshAt)}</span>
```
With:
```svelte
<span class="algo-ts algo-ts-data" ...>{clientTimestamp($lastRefreshAt)}</span>
```

Also import `clientTimestamp` from `$lib/stores` in any file that currently only imports `formatDualTz`.

**Mobile tap magnifies instead of toggling**: The browser triggers text zoom/magnify on mobile when tapping text. Fix in `frontend/src/routes/(algo)/+layout.svelte` — add to `.algo-ts-group`:
```css
.algo-ts-group {
  touch-action: manipulation;   /* prevents double-tap zoom */
  user-select: none;            /* prevents selection on tap */
  -webkit-tap-highlight-color: transparent;
  cursor: pointer;
}
```

### Item 3 — Activity card download button

**`frontend/src/lib/ActivityLogSurface.svelte`**: Add an `onDownload` callback that exports the currently visible rows as CSV. Wire to the existing `onDownload` prop that LogPanel already accepts. When activity tab is "Orders", download the order rows; when "Agents"/"System", download the visible log rows.

Add `onDownload` prop to `ActivityLogSurface` and pass it to `<LogPanel {onDownload} />`. The download implementation: collect `orderRows` (or `logRows`) and use the project's existing download pattern (check GridDownloadButton pattern in other pages — e.g., `dashboard/+page.svelte`).

### Item 4 — Desktop gap between "All accounts" and "All" dropdowns (Activity)

The gap still appears on desktop despite removing `margin-left: auto`. Root cause: inspect the LogPanel tab strip layout. The `lp-tab-strip-wrap` is `display:flex`. Inside it: tabs + (spacer flex: 1) + `ActivityAccountSelect` (which contains two inline-flex dropdowns). When tabs don't fill all space, the spacer element grows and pushes the dropdowns to the far right. But the gap between the two dropdowns themselves (All accounts vs All level) comes from `ActivityAccountSelect` internal layout.

**`frontend/src/lib/ActivityAccountSelect.svelte`**: Read the full file carefully. Identify any `gap`, `margin`, or `padding` between the two select elements and reduce it to `0.3rem` maximum (matching the card button gap standard). Also verify there's no `justify-content: space-between` on the wrapper.

### Item 5 — Activity middle strip scroll (tabs + dropdowns)

**`frontend/src/lib/LogPanel.svelte`**:

Current: `.lp-tab-strip-wrap { overflow-x: hidden }` — clips overflow silently.

Fix: The entire middle strip (tabs + account filters) should scroll together as a unit. Change:
```css
.lp-tab-strip-wrap {
  overflow-x: auto;
  -webkit-overflow-scrolling: touch;
  scrollbar-width: none;        /* hide scrollbar — scroll is gesture-only */
}
.lp-tab-strip-wrap::-webkit-scrollbar { display: none; }
```

Ensure the `ActivityAccountSelect` component is INSIDE the same flex container as the tabs (already the case — confirm with code read). With `overflow-x: auto`, all middle elements including both dropdowns participate in horizontal scrolling when viewport is constrained.

Apply same fix to any other card tab strip that has `overflow-x: hidden` — grep for this pattern and fix consistently.

### Item 6 — Orders: lot-size chip after symbol

**`frontend/src/lib/LogPanel.svelte`** — `_orderSymSpan()` function:

Read the current `_orderSymSpan()` implementation and the `_orderRowHtml()` format. Add a lot-size chip inline after the symbol text for F&O instruments. Use the order's `lot_size` field (check what fields are available on `o`). If `o.lot_size > 1`, append `<span class="log-lot-chip">${Math.round(o.quantity / o.lot_size)}L</span>` after the symbol block (showing lot count, not contract count).

Style `.log-lot-chip` in LogPanel's `<style>`:
```css
:global(.log-lot-chip) {
  font-size: var(--fs-xs);
  font-weight: 700;
  background: rgba(251,191,36,0.12);
  color: var(--algo-amber);
  border: 1px solid rgba(251,191,36,0.30);
  border-radius: 2px;
  padding: 0 0.25rem;
  margin-left: 0.2rem;
  font-variant-numeric: tabular-nums;
  vertical-align: middle;
}
```

### Item 7 — Charts: fullscreen button for all chart surfaces

Cards without CardHeader that need it added:

**`frontend/src/lib/NavBreakdown.svelte`**: This is rendered inside Dashboard. It's a table component. Add a `CardHeader` wrapper INSIDE NavBreakdown itself, or add it at the call site in `dashboard/+page.svelte`. Prefer adding at call site. Check how it's called and add `<CardHeader title="NAV Breakdown" bind:isFullscreen bind:isCollapsed detectOverflow={false} />` wrapping the NavBreakdown section.

**`frontend/src/lib/execution/SimulatorPanel.svelte`**: PriceChart instances (lines ~813-815, 839-840) and MultiPriceChart (line ~853). These are embedded charts inside the simulator. Add `CardHeader` with `detectOverflow={false}` above each chart container. Import CardHeader if not already imported.

**`frontend/src/lib/execution/ReplayPanel.svelte`**: Same pattern — PriceChart at line ~355. Add CardHeader wrapper.

**Rule**: Every chart surface (PriceChart, MultiPriceChart, OptionsPayoff, EquityCurve) must be inside a CardHeader with `detectOverflow={false}`.

### Item 8 — Buttons visible in fullscreen mode

The `app.css` rule `.fs-card-on .collapse-btn { display: none !important }` (line 1841) correctly hides collapse in fullscreen (not meaningful there). But verify no other button is hidden by CSS.

Also: the RefreshButton only shows in fullscreen by default. For cards that have their own independent refresh path (not the page-header refresh), set `refreshAlwaysVisible={true}` so the refresh button is visible in BOTH default and fullscreen mode. Read CardControls usages and identify which cards have `onRefresh` but NOT `refreshAlwaysVisible=true` — add `refreshAlwaysVisible={true}` where the card has data independent of the page-header refresh.

Cards to audit: `orders/+page.svelte` activity LogPanel, `automation/activity/+page.svelte`, `admin/brokers/+page.svelte`.

Also ensure: when a card is in fullscreen, the search bar and download button remain visible (they should already — verify no CSS rule hides them and fix if found).

### Item 9 — Agent tab chips legible text

**`frontend/src/routes/(algo)/automation/+page.svelte`**:

The following classes use `font-size: var(--fs-xs)` (= 0.55rem, ~8px) — too small:
- `.preview-chip` (line ~1507) → `font-size: var(--fs-sm)` (0.6rem)
- `.lifespan-chip` (line ~1549) → `font-size: var(--fs-sm)`
- `.agent-lifespan-tag` (line ~1536) → `font-size: var(--fs-sm)`
- `.ai-hint` (line ~1441) → `font-size: var(--fs-sm)`

Also check `automation/agent-templates/+page.svelte` and `automation/templates/+page.svelte` for similar `fs-xs` chip uses — fix to `fs-sm`.

### Item 10 — Grid alternating row background: comprehensive fix

**`frontend/src/app.css`**: The global `--ag-odd-row-background-color: rgba(13,22,42,0.30)` is defined. All `.ag-theme-algo` grids should inherit it automatically. Verify no individual grid overrides `rowStyle` or sets a background that cancels the alternation.

Check these files for `rowStyle` / `getRowStyle` / `suppressRowAlternation`:
- `MarketPulse.svelte`
- `PerformancePage.svelte`
- `NavBreakdown.svelte`
- `admin/derivatives/+page.svelte`
- `dashboard/+page.svelte`
- `orders/+page.svelte` (LogPanel order grid if ag-Grid based)
- `automation/activity/+page.svelte`

If any grid sets `rowStyle` with a background color, remove the background property (keep other style properties like cursor).

**LogPanel activity rows**: The `nth-child(odd)` rule was added in Round 5. Verify it's present and working. If the activity grid uses a different class structure (e.g. different from `.log-panel.log-rows .log-row`), fix the selector.

### Item 11 — Font legibility across the site

Global sweep for unreadable text:

**`frontend/src/app.css`**:
- Any explicit `font-size: 8px` or `font-size: 7px` — change to `var(--fs-xs)` (0.55rem minimum)
- `var(--fs-2xs)` = 0.5rem = 7.5px — this is too small for any interactive/readable text. Change any non-SVG use to `var(--fs-xs)` minimum

**`frontend/src/lib/OptionsPayoff.svelte`**: Line ~1537: `font-size: 8px` (SVG axis labels). This is on a chart SVG — change to `9px` minimum for readability.

**Check these files for sub-readable sizes** (< 0.55rem):
- `admin/derivatives/+page.svelte`
- `MarketPulse.svelte`
- `LogPanel.svelte`
- `PerformancePage.svelte`

Minimum readable sizes by context:
- Interactive chips/labels: `var(--fs-sm)` = 0.6rem
- Passive display text: `var(--fs-xs)` = 0.55rem minimum
- SVG axis labels: 9px minimum

### Item 12 — Stale CSS removal (from stale audit)

**`frontend/src/app.css`** lines 304-386: `.algo-chip`, `.algo-chip-amber`, `.algo-chip-cash`, `.algo-chip-violet`, `.algo-chip-slate`, `.algo-chip-pos`, `.algo-chip-neg`, `.algo-tag` — 8 rules with zero callers in any `.svelte` or `.js` file. Remove all 8.

NOTE: `frontend/e2e/algo_consistency.spec.js` around line 385-387 asserts the presence of these classes. Update/remove those assertions when deleting the CSS.

**`frontend/src/lib/SymbolPanel.svelte`** line ~4365: `.oes-common-mode-chip` — no usage in template. Remove.

**`frontend/src/lib/SymbolPanel.svelte`** line ~4377: `.oes-common-chase-toggle` — comment at ~4384 confirms the original checkbox was removed. Remove the CSS rule.

---

## Audit Remediation Items (from CC/perf/stale audit)

### A1 — CC Reductions (8 functions, backend + broker)

**`backend/api/persistence/migrations.py:146 seed_templates` (CC 20)**:
Extract `_promote_default_if_unclaimed(session, by_slug, slug, applies_to)` — consolidates 3 identical promote-default blocks (bull/bear/short-vol). Call it 3 times instead.

**`backend/api/background.py:2869 _task_sparkline_warm` (CC 19)**:
Extract 5 `_collect_<source>_symbols(seen, pairs) -> None` helpers (watchlist, holdings-cached, holdings-live, positions, movers) — consolidates copy-pasted try/except/append/log boilerplate.

**`backend/api/routes/positions.py:848 _enrich_position_greeks` (CC 19)**:
Extract `_batch_fetch_spots(underlying_keys: set[str]) -> dict[str, float]` — isolates the `broker.quote()` batched spot-fetch pass.

**`backend/api/routes/positions.py:207 _overlay_snapshot_for_closed_exchanges` (CC 18)**:
Extract `_replace_row_price(row, live_ltp, exchange_open, snap_ltp)` — consolidates the resolve + replace_kwargs logic shared between both branches.

**`backend/brokers/broker_apis.py:746 _record_fetch` (CC 18)**:
Extract `_record_breaker_state(account, ok, error, now, e)` — the lock-body circuit-breaker state machine. Outer function retains only the health-stamp emit.

**`backend/api/algo/derivatives.py:1057 multileg_payoff_curve` (CC 17)**:
Extract `_leg_today_expiry_arrays(leg, S_grid, r, eval_T) -> tuple[np.ndarray, np.ndarray]` — per-leg branching (fut/intrinsic/BS re-price/expiry-intrinsic) into one place.

**`backend/brokers/kite_ticker.py:411 TickerManager.status` (CC 16)**:
Extract `_build_stale_list(subscribed_copy, age_snapshot, sym_snapshot, now, threshold) -> tuple[list, float]` and `_failover_snapshot(self) -> dict`.

**`backend/api/auth_guard.py:15 jwt_guard` (CC 17)**:
Extract `_reject_if_user_invalid(row, tv) -> None` — 5 sequential NotAuthorized guard raises.

### A2 — Dead parameters (3 bugs — params accepted but silently ignored)

**`backend/api/algo/actions_preflight.py:771 diagnose_live_failure(kite_error)`**: `kite_error` param never read inside the function. Either implement (log it to the diagnostic output) or remove the param and update 3 call sites at `actions_live.py:168, 309, 787`.

**`backend/brokers/broker_apis.py:1456 _apply_backfill_to_list(qty_col)`**: `qty_col` param accepted, never used. The backfill always calls `backfill_market_data(combined)` ignoring `qty_col`. Two callers pass `qty_col="opening_quantity"` believing the filter is respected. Implement: pass `qty_col` through to `backfill_market_data(combined, qty_col=qty_col)` OR remove the param and update callers.

**`backend/brokers/service/app.py:277 _attempt_failover_swap(slowed_s)`**: `slowed_s` accepted but never read. The log at line 496 re-calls `_watchdog_slowed_interval_s()` independently introducing a config-change race. Fix: use the passed `slowed_s` arg in the log line instead.

### A3 — Dead imports (remove, 7 files)

- `backend/api/algo/expiry.py:28` — `from ... import mask_column` (never called in file)
- `backend/api/routes/orders.py:60` — `from ... import mask_column` (never called)
- `backend/api/routes/orders.py:79` — `from orders_helpers import _VALIDITIES` (never referenced)
- `backend/api/algo/actions.py:638` — `from actions_sim import _sim_ltp_for` (never used)
- `backend/api/background.py:2833` — `func as _sa_func` (only `_sa_select` used)
- `backend/brokers/service/routes.py:70` — `from schemas import BrokerCallResp` (zero call sites)

### A4 — Dead function + alias

**`backend/brokers/broker_apis.py:1017 sort_accounts()`**: Zero production callers in routes/background/algo. Remove the function. Also update `backend/tests/` — find and remove tests that cover only this dead function (verify with grep first).

**`backend/brokers/broker_apis.py:2155 backfill_close_prices = backfill_market_data`**: Dead alias with zero callers. Remove.

### A5 — Performance fixes

**`frontend/src/lib/MarketPulse.svelte:2761`**: `_prefetchTimers.length * 80` stagger uses total array length including completed handles. After 125+ symbols, delay exceeds 10s. Fix: maintain a separate `_prefetchPending = $state(0)` counter; increment on create, decrement in the callback. Use `_prefetchPending * 80` for the stagger instead of `_prefetchTimers.length`.

**`backend/brokers/broker_apis.py:1871`**: `.iterrows()` on missing-rows DataFrame. Replace with `.itertuples(index=False)` or vectorized extraction.

**`backend/scripts/visitor_report.py:332 _upsert_records`**: N round-trips per IP row. `pg_insert` is already imported but unused here. Replace the `SELECT + conditional UPDATE/INSERT` loop with PostgreSQL `INSERT ... ON CONFLICT (ip_address) DO UPDATE SET ...` using `pg_insert`. This reduces N queries to 1 batch upsert.

### A6 — Stale documentation

**`backend/api/schemas.py:578`**: `target_pct` field marked `DEPRECATED in v2.2 — Will be removed in v2.2`. Field is actively used by 5+ files. Remove the deprecation notice; update the docstring to reflect it's canonical.

---

## Agents

- frontend (Pass A — NavStrip popup redesign + per-slot hints): `frontend/src/lib/InfoHint.svelte`, `frontend/src/lib/PositionStrip.svelte`

- frontend (Pass B — timestamp IST format + mobile tap fix): all 29 files with `algo-ts-group` (grep -rl "algo-ts-group" frontend/src/routes/), `frontend/src/routes/(algo)/+layout.svelte` (touch-action CSS)

- frontend (Pass C — activity scroll/gap/download + orders lot chip + agent chip legibility + grid rows + stale CSS): `frontend/src/lib/LogPanel.svelte`, `frontend/src/lib/ActivityAccountSelect.svelte`, `frontend/src/lib/ActivityLogSurface.svelte`, `frontend/src/routes/(algo)/orders/+page.svelte` or `frontend/src/lib/LogPanel.svelte` (order row renderer), `frontend/src/routes/(algo)/automation/+page.svelte`, `frontend/src/routes/(algo)/automation/agent-templates/+page.svelte`, `frontend/src/routes/(algo)/automation/templates/+page.svelte`, `frontend/src/lib/SymbolPanel.svelte` (dead CSS), `frontend/src/app.css` (dead .algo-chip* rules + font fixes + grid row bg check), `frontend/src/lib/OptionsPayoff.svelte` (8px SVG font)

- frontend (Pass D — charts fullscreen + button visibility + MarketPulse _prefetchTimers): `frontend/src/lib/NavBreakdown.svelte`, `frontend/src/routes/(algo)/dashboard/+page.svelte`, `frontend/src/lib/execution/SimulatorPanel.svelte`, `frontend/src/lib/execution/ReplayPanel.svelte`, `frontend/src/lib/CardControls.svelte`, `frontend/src/lib/MarketPulse.svelte` (prefetchTimers fix + ag-Grid rowStyle check), `frontend/src/routes/(algo)/admin/derivatives/+page.svelte` (refreshAlwaysVisible audit), `frontend/src/routes/(algo)/orders/+page.svelte` (refreshAlwaysVisible audit)

- backend (Pass E — CC reductions): `backend/api/persistence/migrations.py`, `backend/api/background.py`, `backend/api/routes/positions.py`, `backend/api/auth_guard.py`, `backend/api/algo/derivatives.py`, `backend/brokers/broker_apis.py`, `backend/brokers/kite_ticker.py`

- broker (Pass F — dead params + dead imports + dead function + perf): `backend/brokers/broker_apis.py`, `backend/brokers/service/app.py`, `backend/brokers/service/routes.py`

- backend (Pass G — dead imports + dead function + stale doc + visitor_report perf): `backend/api/algo/expiry.py`, `backend/api/routes/orders.py`, `backend/api/algo/actions.py`, `backend/api/background.py`, `backend/api/schemas.py`, `backend/scripts/visitor_report.py`

- backend-test (Pass H — update tests for removed sort_accounts + new CC helpers): `backend/tests/` — remove sort_accounts tests, add basic tests for new extracted helpers

- playwright (Pass I — e2e tests): update `frontend/e2e/algo_consistency.spec.js` to remove .algo-chip assertions; add smoke tests for new NavStrip panel popups (click P label → panel opens with left highlight), timestamp IST format check, activity download button presence, lot chip in orders, chart fullscreen buttons

## Tests
- pytest: yes
- svelte-check: yes
- playwright: yes

## Commit message
feat(polish): UI Round 6 + audit — NavStrip panel popups, IST timestamps, activity scroll/download, chart fullscreen, font legibility, CC reductions, dead-code cleanup, perf fixes

## Done when
- NavStrip P/M/C/H label + every value slot has a panel-style popup with left highlight
- Both timestamps show IST-only format; mobile tap toggles without magnifying
- Activity card has download button; middle strip (tabs + dropdowns) scrolls together on all cards
- L/M/H lot chip appears after symbol in order rows
- All chart surfaces (PriceChart, OptionsPayoff, NavBreakdown, EquityCurve in SimulatorPanel/ReplayPanel) have fullscreen button
- Download/search buttons remain visible in fullscreen mode
- Agent tab chips readable (≥ var(--fs-sm))
- All ag-Grid instances use alternating row background consistently
- No text below var(--fs-xs) on any interactive surface
- 8 CC functions reduced from C to B
- 3 dead params fixed, 6 dead imports removed, dead function removed, dead CSS removed
- 3 perf fixes shipped (_prefetchTimers, iterrows, visitor_report upsert)
- svelte-check 0 errors, pytest green, Playwright passing
