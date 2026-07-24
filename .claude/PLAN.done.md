# Plan: UI fixes + GTT MARKET order guard

## Context
Four visual regressions / inconsistencies:
1. The `|` separator between current and refresh timestamps is missing on desktop. It was removed in the last AlgoTimestamp restructure (the agent incorrectly dropped it entirely). On desktop `.ats-slot` is `display: inline-flex` so both spans render side-by-side without any divider.
2. The chart button in the order modal (`SymbolPanel.svelte`) uses a different background (`var(--algo-cyan-bg)` = visible cyan tint) and `border-radius: 3px`, whereas the canonical chart button in `PageHeaderActions.svelte` uses `rgba(255,255,255,0.03)` (near-transparent) and `border-radius: 4px`. The hover also uses a literal rgba instead of the `var(--c-info-14)` token.
3. The intraday performance trend chart card in the dashboard has no heading text — only the vertical `.ch-sep` bar renders. Root cause: `CardHeader` at `frontend/src/routes/(algo)/dashboard/+page.svelte` line ~1806 is missing the `title` prop (only `label="Chart"` is set; `label` is for accessibility only and does not render visible text).
4. The "All" symbol-type filter dropdown in ChartWorkspace is too wide. `.cw-type-wrap` is `width: 8.5rem` on desktop. Reduce to two-thirds: `5.7rem`.
5. GTT order failure alerts firing for MARKET orders: Kite GTT API only accepts LIMIT orders; the `place_gtt` validation loop in `kite.py` (lines 475–494) has no guard rejecting MARKET legs, so they pass through to the API and fail. Fix: coerce `order_type="MARKET"` → `"LIMIT"` at the Kite adapter boundary, log a warning.

## Task
1. Restore the `ats-sep` divider inside `AlgoTimestamp.svelte`.
2. Align `oes-chart-btn` in `SymbolPanel.svelte` to match `pha-btn pha-chart`.
3. Add `title="Intraday Performance"` to the CardHeader for the equity-curve chart in the dashboard.
4. Reduce `.cw-type-wrap` width in `ChartWorkspace.svelte` from `8.5rem` → `5.7rem`.
5. Guard MARKET→LIMIT coercion in `backend/brokers/adapters/kite.py` `place_gtt` validation loop.

## Agents
- backend: skip
- broker: In `backend/brokers/adapters/kite.py` `place_gtt` validation loop (lines 475–494), add a coercion guard after the existing leg validation. For each leg where `order_type` is `"MARKET"`, log a warning and coerce to `"LIMIT"` (GTT trigger fires at limit price = trigger price, which is the correct Kite behavior):
  ```python
  if _leg_ot == "MARKET":
      logger.warning(
          "GTT leg coerced MARKET→LIMIT (Kite GTT does not support MARKET orders); "
          "trigger price used as limit price"
      )
      _leg["order_type"] = "LIMIT"
  ```
  This goes inside the `for _leg in orders:` loop, before the leg dict is passed to the Kite API.

  Also add a pytest in `backend/tests/test_kite_gtt.py` (or the nearest existing GTT test file) asserting that a `place_gtt` call with a MARKET leg coerces it to LIMIT and does not raise.

- frontend: Four targeted edits:

  **Edit 1 — `frontend/src/lib/AlgoTimestamp.svelte`**

  Restore `<span class="ats-sep" aria-hidden="true">|</span>` inside `.ats-slot`, between `ats-now` and `ats-refresh`:
  ```html
  <span class="ats-slot">
    <span class="ats-now" class:ats-mobile-hide={_showRefresh}>{_nowTs}</span>
    {#if _refreshTs}
      <span class="ats-sep" aria-hidden="true">|</span>
      <span class="ats-refresh" class:ats-mobile-hide={!_showRefresh}>{_refreshTs}</span>
    {/if}
  </span>
  ```

  Restore `.ats-sep` CSS rule in the global block (before media query):
  ```css
  .ats-sep {
    color: var(--text-muted);
    font-size: inherit;
    opacity: 0.5;
  }
  ```

  In the `@media (max-width: 640px)` block add:
  ```css
  .ats-sep { display: none; }
  ```
  (hides it on mobile so the grid-stacked toggle is clean)

  **Edit 2 — `frontend/src/lib/SymbolPanel.svelte`** (lines ~3233–3259)

  Update `.oes-chart-btn` to match `pha-btn pha-chart`:
  - `background`: `var(--algo-cyan-bg)` → `rgba(255, 255, 255, 0.03)`
  - `border-radius`: `3px` → `4px`
  - Hover `background`: `rgba(103, 232, 249, 0.18)` → `var(--c-info-14)`

  **Edit 3 — `frontend/src/routes/(algo)/dashboard/+page.svelte`** (around line 1806)

  Add `title="Intraday Performance"` to the `<CardHeader>` for the equity-curve / intraday chart card:
  ```svelte
  <CardHeader
    bind:isCollapsed={_colEquityCurve}
    bind:isFullscreen={_fsEquityCurve}
    title="Intraday Performance"
    label="Chart"
    onRefresh={_refreshAll}
    bind:refreshLoading={_refreshing}
    showSearch={false}
    detectOverflow={false}
  >
  ```

  **Edit 4 — `frontend/src/lib/ChartWorkspace.svelte`** (line ~2462)

  Reduce `.cw-type-wrap` desktop width from `8.5rem` to `5.7rem` (two-thirds):
  ```css
  .cw-type-wrap {
    width: 5.7rem;   /* was 8.5rem — reduced to 2/3 per operator */
    flex-shrink: 0;
  }
  ```
  Leave the mobile breakpoint (`4.2rem` at ≤520px) unchanged.

- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Tests
- pytest: yes (GTT MARKET coercion test)
- svelte-check: yes
- playwright: no

## Commit message
fix(ui,broker): restore ats-sep divider; align chart btn; dashboard chart title; narrow dropdown; coerce GTT MARKET→LIMIT

## Done when
- Desktop: `[HH:MM IST / HH:MM ET] | [HH:MM IST / HH:MM ET]` separator visible
- Mobile: no separator (grid toggle unaffected)
- Order modal chart button: near-transparent background + 4px radius, matches page-header button
- Dashboard intraday performance trend card shows "Intraday Performance" heading
- Chart workspace "All" dropdown visibly narrower (5.7rem vs 8.5rem)
- GTT with MARKET leg no longer fails: coerced to LIMIT silently, warning logged
- pytest green for the new GTT coercion test
- svelte-check 0 errors
