# Plan: Activity panel — per-tab filter visibility + card button group

## Context
Two bugs + one missing feature in the Activity panel (LogPanel / ActivityLogSurface):

**Bug 1 — Filters shown for wrong tabs:**
`ActivityHeaderFilters` renders the "All accounts" selector and level/error-type selector
regardless of the active tab. But these filters only apply to some tabs:
- Account filter applies: Orders, Agents, System, Conn
- Level/error-type filter applies: Agents, System, Conn (NOT Orders — it has its own status filter)
- Terminal, Ticks, News: neither filter applies (ignored by the data layer)
Result: both filters show on Terminal/Ticks/News where they do nothing.

**Bug 2 — Card button group missing:**
No activity surface (orders card, /activity page, /console, ActivityLogModal) has the
canonical card button group (Search + Expand/Contract + Fullscreen + Download). The /orders
card has CardHeader collapse/fullscreen but no download or search. Others have a page-level
refresh only.

**Architecture:**
- `LogPanel.svelte` owns the tab state (`_activeTab`)
- `ActivityLogSurface.svelte` is the bridge — wraps LogPanel + passes filters through
- `ActivityHeaderFilters.svelte` renders the account + level filter controls
- Filters must be visible/hidden based on `_activeTab` — currently neither surface exposes
  the active tab to `ActivityHeaderFilters`

## Agents
- backend: skip
- frontend: All changes in the activity panel layer:

  **Fix 1 — Per-tab filter visibility**

  In `ActivityLogSurface.svelte`:
  - Surface the active tab from LogPanel via a `bind:activeTab={_activeTab}` prop
    (LogPanel already has `_activeTab` as `$state` — add it to the component's `$props` as
    a bindable export so the parent can observe it)
  - Derive two booleans from `_activeTab`:
    ```javascript
    const _showAccountFilter = $derived(['order', 'agent', 'system', 'conn'].includes(_activeTab));
    const _showLevelFilter   = $derived(['agent', 'system', 'conn'].includes(_activeTab));
    ```
  - Pass these as props to `ActivityHeaderFilters`:
    `showAccountFilter={_showAccountFilter} showLevelFilter={_showLevelFilter}`

  In `LogPanel.svelte`:
  - Add `activeTab = $bindable('')` to `$props()` so the parent can bind to it
  - Keep all existing internal tab logic unchanged; just write `activeTab = _activeTab` when
    `_activeTab` changes (or directly bind `_activeTab` to `activeTab`)

  In `ActivityHeaderFilters.svelte`:
  - Add props: `showAccountFilter = true`, `showLevelFilter = true` (default true = backward compatible)
  - Wrap the account selector render in `{#if showAccountFilter}...{/if}`
  - Wrap the level/error-type selector in `{#if showLevelFilter}...{/if}`

  **Fix 2 — Card button group (canonical order: Search → Expand/Contract → Fullscreen → Download)**

  In `ActivityLogSurface.svelte`:
  - Add four internal state cells: `_searchOpen = $state(false)`, `_searchQuery = $state('')`,
    `_expanded = $state(false)`, `_fullscreen = $state(false)`
  - Add a card button group row INSIDE ActivityLogSurface (not delegated to parent), positioned
    in the header row alongside the AlgoTabs and filters. Use the existing CardControls component
    if it covers these four; otherwise add a `.log-card-controls` flex group with four icon buttons:
    - **Search (🔍):** toggles a search input below the tab row; filters rows by text match
      on the visible column (order ref/symbol for Orders, message text for others)
    - **Expand/Contract:** toggles `_expanded` which adds a CSS class to grow the panel height
    - **Fullscreen:** opens ActivityLogModal (already exists!) with the current tab pre-selected
    - **Download:** exports the currently visible rows as CSV; filename = `activity-{tab}-{date}.csv`

  **Download logic per tab:**
  - Orders: `filteredOrderRows` → columns: time, ref, symbol, type, qty, price, status, account
  - Agents: `_agentRows` → columns: time, event_type, agent, level, message
  - System/Conn: `_sysRows` / `_connRows` → columns: time, level, message
  - Terminal: merged rows → columns: time, source, message
  - Ticks: tick rows → columns: time, symbol, ltp, change
  - News: news items → columns: time, headline, source

  **Search logic:** Add `_searchQuery` to each tab's `$derived` filter chain as a final
  text-match pass. Empty string = no filter (fast path).

  **Consistency across all surfaces:**
  The card button group lives INSIDE `ActivityLogSurface` — so every usage automatically
  gets it. No changes needed in individual page files except to remove any duplicate
  collapse/fullscreen wiring that CardHeader already provides (avoid double buttons).

  For ActivityLogModal: omit Expand/Contract and Fullscreen buttons (already a modal — use
  `context === 'modal'` prop to suppress those two; Download + Search still show).

  For embedded panels (SymbolPanel, ReplayPanel, SimulatorPanel): use `context === 'card'`
  to show only Download + Search (no expand/fullscreen in tight panels).

- broker: skip
- doc: skip
- backend-test: skip
- playwright: Add tests to `frontend/e2e/activity-panel.spec.ts`:
  1. On /orders, switch to Terminal tab — assert account filter and level filter are NOT visible
  2. On /orders, switch to Orders tab — assert account filter IS visible, level filter is NOT
  3. On /orders, switch to System tab — assert BOTH filters are visible
  4. On /orders, assert card button group has search, expand, fullscreen, download buttons
  5. On /activity page, same button group assertions

## Tests
- pytest: no
- svelte-check: yes
- playwright: yes

## Commit message
fix(activity): per-tab filter visibility + card button group (search/expand/fullscreen/download)

Account filter shown only for Orders/Agents/System/Conn tabs; level filter shown only
for Agents/System/Conn. Card button group (search, expand/contract, fullscreen, download)
added to ActivityLogSurface — applies consistently to all activity panel surfaces.

## Done when
- Terminal/Ticks/News tabs: neither account nor level filter visible
- Orders tab: account filter visible, level filter hidden
- Agents/System/Conn tabs: both filters visible
- Card button group with 4 buttons present on /orders activity card and /activity page
- Download exports current tab's visible rows as CSV
- Search filters visible rows by text match
- Fullscreen button opens ActivityLogModal at current tab
- Playwright specs pass
- svelte-check 0 errors
