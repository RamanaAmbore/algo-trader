# Plan: Timestamp SSOT + NavStrip cleanup + Card button group ordering

## Context

Three change groups:
1. **Timestamp SSOT**: All 54 pages have duplicate timestamp toggle logic; Round 6 regressed refresh timestamp to IST-only (not dual-TZ); desktop never shows both side-by-side.
2. **NavStrip slot hints**: Round 6 added 10 per-slot ⓘ InfoHint icons that clutter the strip. Consolidate into the 4 pill labels.
3. **Card button group order**: Wrong order in CardControls and LogPanel; "Restore" text appears in fullscreen; activity download disabled/wrong position.

---

## Task

### A — Card button group reordering

**Target order (default mode):** Search → Download → Expand/Contract (Collapse) → Fullscreen

**Full screen mode:** Search → Download → (Collapse hidden) → X (DefaultSize, last button)

**Files:**

**`frontend/src/lib/CardControls.svelte`**: Reorder the template. Current order: Refresh → Search → Collapse → Fullscreen → DefaultSize → Download.
New order:
1. Refresh (keep first — it's a data action, not display control)
2. Search
3. Download
4. Collapse
5. FullscreenButton (`{#if !isFullscreen}`) / DefaultSizeButton (`{#if isFullscreen}`) — these are mutually exclusive, both go last

**`frontend/src/app.css`**: Remove the `.fs-card-on .default-btn .dsb-label { display: inline; ... }` rule and the `.fs-card-on .default-btn { padding: 0 0.4rem; width: auto; gap: 0.3rem; }` rule. No "Restore" text in fullscreen — just the icon, same size as normal.

**`frontend/src/lib/DefaultSizeButton.svelte`**: Remove the `<span class="dsb-label">Restore</span>` element entirely (it's only rendered for the fullscreen label, which we're removing).

**`frontend/src/lib/LogPanel.svelte`** — `lp-card-btns` button group: Current order is Search → Collapse/FS → Download. Reorder to:
1. Search
2. Download
3. Collapse (if applicable)
4. Fullscreen / Close (if applicable)

LogPanel has its own button group template (`lp-card-btns`) — find it (around lines 1432-1526) and reorder the buttons.

**All pages using CardHeader**: No page-level changes needed since CardControls handles order. Verify order is visually correct after changes.

---

### B — Dashboard activity download disabled vs orders page enabled

**Root cause (confirmed)**: Dashboard passes `defaultTab="news"` to ActivityLogSurface →
LogPanel initialises `logTab = 'news'` → download button `disabled={logTab === 'news'}`.
Orders page passes `defaultTab="order"` → download is active immediately.

Both pages pass `label="ACTIVITY"` and no `onDownload` — same rendering path, same logic.
The ONLY difference is the `defaultTab` prop.

**Fix** (`frontend/src/routes/(algo)/dashboard/+page.svelte`, ~line 2221):
Change `defaultTab="news"` → `defaultTab="order"` on the ActivityLogSurface call.
News tab is still accessible via tab click; download becomes active on all non-news tabs.

---

### C — NavStrip slot hints cleanup

**`frontend/src/lib/PositionStrip.svelte`**:

Remove all 10 per-slot `<InfoHint popup panel showOnHover label="ⓘ" ...>` elements.
Remove the per-slot ⓘ CSS override: `.ps-agg-v + :global(.info-wrap) :global(.info-btn) { ... }`

Update 4 label InfoHint `text` props to describe ALL slots:

**P label:**
```
text="<b>Day P&L:</b> Live ticks − prev-close × net qty, all accounts. For new intraday (overnight_qty=0), uses pnl directly.<br><br><b>Lifetime P&L:</b> Cumulative since position opened. Includes realised + unrealised.<br><br><b>Expiry P&L:</b> Projected F&O value at expiry via lognormal model."
```

**M label:**
```
text="<b>Available:</b> Cash deployable for new orders = Total − used margin. Updated after every fill.<br><br><b>Total:</b> Full collateral across all accounts = Available + margin blocked for open positions."
```

**C label:**
```
text="<b>Cash Available (CA):</b> Live deployable cash. Nets realised P&L + long option premiums paid.<br><br><b>Total Cash:</b> CA + premium tied up in long options (recoverable if closed)."
```

**H label:**
```
text="<b>Today MTM:</b> Live LTP − prev close × qty for long-term holdings. Intraday only.<br><br><b>Value:</b> Broker-reported current market value across all accounts.<br><br><b>Lifetime P&L:</b> Cumulative since purchase = (current − avg cost) × qty."
```

---

### D — Timestamp SSOT: `AlgoTimestamp.svelte`

Create `frontend/src/lib/AlgoTimestamp.svelte`:
- Reads `$nowStamp` + `$lastRefreshAt` from stores — no props
- Desktop (> 640px): shows `[current IST+EDT] | [refresh IST+EDT]` always when `$lastRefreshAt > 0`
- Mobile (≤ 640px): current by default; click toggles to refresh; separator hidden
- Colors: current = `var(--c-info)` (sky/cyan); refresh = `var(--algo-amber)` (amber)

```svelte
<script>
  import { nowStamp, lastRefreshAt, formatDualTz } from '$lib/stores';
  let _showRefresh = $state(false);
  let _refreshTs = $derived($lastRefreshAt ? formatDualTz(new Date($lastRefreshAt)) : null);
  function _toggle() { if (_refreshTs) _showRefresh = !_showRefresh; }
</script>

<span class="ats-group" onclick={_toggle} role="button" tabindex="0"
      onkeydown={(e) => e.key === 'Enter' && _toggle()}
      style="touch-action: manipulation; user-select: none; -webkit-tap-highlight-color: transparent; cursor: pointer;">
  <span class="ats-now" class:ats-mobile-hide={_showRefresh}>{$nowStamp}</span>
  {#if _refreshTs}
    <span class="ats-sep" aria-hidden="true">|</span>
    <span class="ats-refresh" class:ats-mobile-hide={!_showRefresh}>{_refreshTs}</span>
  {/if}
</span>
```

CSS: `.ats-now { color: var(--c-info) }`, `.ats-refresh { color: var(--algo-amber) }`,
`.ats-sep` only visible on desktop (hidden via media query on mobile since only 1 shows).

**Update 54 page files**: Replace `.algo-ts-group` blocks with `<AlgoTimestamp />`.
Remove unused imports: `clientTimestamp`, `formatIstOnly`, `formatDualTz` (per-page), `lastRefreshAt` (per-page, if only used for timestamp).
Remove unused state: `_showLiveTs`, `_moversAsOf` (ONLY if used exclusively for timestamp toggle).

**`frontend/src/lib/stores.js`**: Remove `formatIstOnly` function (added Round 6, now unused).

**`frontend/src/routes/(algo)/+layout.svelte`**: Remove `.algo-ts`, `.algo-ts-group`, `.algo-ts-hidden`, `.algo-ts-data` CSS rules IF they are not referenced anywhere outside the timestamp block. Check before removing.

---

### E — Activity dropdown gap + unified tab-strip scroll

**`frontend/src/lib/ActivityHeaderFilters.svelte`**:
Set `gap: 0` on `.act-filters` — the two dropdowns (All Accounts, All level) should sit flush
with no gap between them on desktop. On mobile the same applies (they're already compact).

**`frontend/src/lib/LogPanel.svelte`** — scroll conflict fix:
`.lp-tab-strip-wrap` is the scroll container (`overflow-x: auto`), but `AlgoTabs` inside it
also has `overflow-x: auto` on `.algo-tabs-strip`. Two nested scroll containers means the
inner one absorbs scroll before the outer one, so tabs and dropdowns don't scroll as a unit.

Fix: Override `.algo-tabs-strip` overflow inside `.lp-tab-strip-wrap`:
```css
.lp-tab-strip-wrap :global(.algo-tabs-strip) {
  overflow-x: visible;  /* Let the parent wrapper handle scroll */
}
```

This forces the entire strip (tabs + dropdowns) to scroll as one unit. Also ensure all tab
buttons are `flex-shrink: 0` within the strip so they don't compress (AlgoTabs already does
this via `:global(.algo-tabs-strip .algo-tab) { flex-shrink: 0 }`).

**CardHeader `ch-middle` slot** (for all other cards with tabs):
`.ch-middle` already has `overflow-x: auto` — any card placing tabs in the `middle` slot
already scrolls correctly. No change needed here. Verify that MarketPulse's `.mp-head-tabs`
wrapper inside `ch-middle` does NOT also have its own `overflow-x: auto` (remove if present
to prevent the same double-scroll-container issue).

**Button sequence in CardControls**: See Task A above — already planned.

---

### F — Activity tab row alternation fix (all non-Orders tabs)

**Root cause**: `:nth-child(odd)` on `.log-row` breaks whenever any non-`.log-row` element
(empty-state divs, conditional anchors) exists as a sibling inside `.log-panel.log-rows`.
Also, log-level classes (`log-error`, `log-warn`, `log-debug`, `log-info`) may set their
own `background` that overrides the alternation for those specific row types.

**Files:** `frontend/src/lib/LogPanel.svelte`

**Fix — JS-applied striped class**: Instead of nth-child CSS, apply `lp-row-odd` / `lp-row-even`
class to each row based on its index in the rendered array. This is immune to sibling-count issues.

In the row-building code for each non-Orders tab (agent, terminal, simulator, system, conn):
- For each row array (`_agentRows`, `_sysRows`, `_connRows`, etc.), when building row HTML
  via `_logRow()` or when mapping the array, pass the index and add class:
  ```javascript
  // In _logRow() or the array mapping:
  const stripe = idx % 2 === 0 ? 'lp-row-odd' : 'lp-row-even';
  // Add stripe to the row div class: `<div class="log-row ${rowClass} ${stripe}">`
  ```

- For terminal tab which uses `{@html _terminalHtmlDerived}` built from a string: apply
  the same index-based class when building `_terminalHtmlDerived`.

**CSS**: Replace the current nth-child rule with:
```css
:global(.log-panel.log-rows .log-row.lp-row-odd) {
  background: var(--ag-odd-row-background-color, rgba(13,22,42,0.30));
}
:global(.log-panel.log-rows .log-row.lp-row-even) {
  background: transparent;
}
```

Remove the old `:nth-child(odd)` rule.

**Also check**: If log-level classes (`log-error`, `log-warn`, etc.) set `background`,
verify they still appear visually distinct (e.g., error rows can keep a colored background;
that overrides the alternation for those specific rows which is acceptable since the color
carries semantic meaning).

**Text + row format consistency**: Verify all non-Orders tabs use identical `.log-row` CSS:
same `font-size`, `color`, `padding`, `border-bottom`. Any tab-specific overrides that
differ from the base should be removed unless intentional.

---

### G — Chase L/M/H: move from card header to symbol row in order entry

**Root cause of omission**: Round 6 refactored the order page header to use `CardHeader`,
but the planned chase-row relocation was never implemented — the cluster stayed in the
`{#snippet left()}` of the `<CardHeader>`.

**Current location** (`frontend/src/routes/(algo)/orders/+page.svelte`, ~lines 394-424):
Chase L/M/H sits in the card header left slot as `.oc-header-cluster` alongside "ORDER ENTRY".

**Target location**: Move the entire `.oc-header-cluster` (CHASE label + L/M/H button group +
optional Clear button) to appear inline in the **symbol/instrument row** of the order entry
form — immediately after the symbol selector, same flex row.

Find the order entry form's symbol row (look for the instrument/symbol autocomplete input),
wrap symbol input + chase cluster in a flex row. Remove `.oc-header-cluster` from the header.

The header left slot should then only contain:
```svelte
<span class="oc-entry-label"><svg>...</svg>ORDER ENTRY</span>
```

Adjust `.oc-header-cluster` CSS if needed to fit inline in the form row (compact, no extra margin).

---

### H — Exp close grid alternating rows (CandidateLegRow / derivatives page)

**Root cause**: `CandidateLegRow.svelte` conditionally renders an `.expiry-band-header`
element before the `.cand-row` element inside the same component. Both are direct children
of `.cand-grid`. This makes `.expiry-band-header` elements consume nth-child positions,
so the `.cand-grid > :nth-child(even)` alternation rule applies to band headers, not to
data rows. Data rows end up with wrong or missing stripes.

**Files**:
- `frontend/src/routes/(algo)/admin/derivatives/+page.svelte` (`.cand-grid > :nth-child(even)` rule ~line 5701)
- `frontend/src/routes/(algo)/admin/derivatives/CandidateLegRow.svelte` (band header render + `.cand-row`)

**Fix**: Switch from CSS nth-child to Svelte-loop index classes.

In `+page.svelte`, wherever `CandidateLegRow` is rendered in a `{#each}` loop, pass
`stripe={idx % 2 === 0 ? 'cand-row-odd' : 'cand-row-even'}` as a prop.

In `CandidateLegRow.svelte`:
- Accept `stripe = ''` prop
- Add `{stripe}` class to the `.cand-row` div

CSS in `+page.svelte`: Replace the `nth-child(even)` rule with:
```css
:global(.cand-row.cand-row-odd):not(:global(.cand-row-total)) {
  background-color: rgba(13,22,42,0.30);
}
:global(.cand-row.cand-row-even):not(:global(.cand-row-total)) {
  background-color: transparent;
}
```

Remove the old `nth-child(even)` rule entirely.

**This is the same root cause as Task F** — nth-child breaks when non-data siblings exist.
The same fix pattern (index class passed from `{#each}` loop) applies to both.

---

### I — Grid consistency SSOT: `.algo-table` class + migrate ALL 15+ hand-rolled tables

**Root cause**: 25+ pages each define their own table CSS independently — no shared class.
Font-sizes range from 0.55rem to 0.72rem, hover is missing on 14 of 15 tables, alternating
rows are absent on most, border colors are inconsistent. ag-Grid instances share `ag-theme-algo`
and are consistent; hand-rolled `<table>` elements are not.

**Fix strategy**: Define `.algo-table` (+ `.algo-table-wrap`, `.algo-table-num`) as the SSOT
in `frontend/src/app.css`. Then for every hand-rolled table: add `class="algo-table"` and
delete the per-table CSS override block. Semantic overrides (e.g., amber for token values,
pending row amber in statements) layer on top and remain.

---

**New CSS in `frontend/src/app.css`** (add under `.ag-theme-algo` block):

```css
/* SSOT for hand-rolled tables — matches ag-theme-algo */
.algo-table {
  width: 100%;
  border-collapse: collapse;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.72rem;
  color: var(--algo-slate);
}
.algo-table thead th {
  font-size: 0.6rem;
  font-weight: 700;
  color: var(--text-muted);
  padding: 0 4px;
  height: 28px;
  background: rgba(15,23,42,0.30);
  text-align: left;
  white-space: nowrap;
  border-bottom: 1px solid rgba(126,151,184,0.18);
}
.algo-table tbody td {
  padding: 0 4px;
  height: 24px;
  vertical-align: middle;
  border-bottom: 1px solid rgba(126,151,184,0.10);
}
.algo-table tbody tr:nth-child(odd) td {
  background: var(--ag-odd-row-background-color, rgba(13,22,42,0.30));
}
.algo-table tbody tr:hover td {
  background: var(--ag-row-hover-color, rgba(34,211,238,0.05));
}
.algo-table-num { text-align: right; font-variant-numeric: tabular-nums; }
.algo-table-wrap { overflow-x: auto; }
```

Note: `nth-child(odd)` works correctly inside `<tbody>` — only `<tr>` elements live there,
so no non-row sibling break (unlike the CSS-grid/flex layouts fixed in Tasks F and H).

---

**Complete migration list** — for each file: add `class="algo-table"` to `<table>`, delete
the now-redundant per-table CSS block, keep only semantic overrides:

| File | Old class(es) | Notes |
|---|---|---|
| `admin/tokens/+page.svelte` | inline `text-[0.65rem]` | Remove Tailwind size; keep amber for token value column |
| `admin/brokers/+page.svelte` | `.brokers-table`, `.conn-table` | Two tables; keep connection-state color chips |
| `admin/audit/+page.svelte` | `.audit-table` | Has alternating rows already; remove font-size override |
| `admin/statements/+page.svelte` | `.ms-table` | Keep `.ms-row.row-pending` amber override |
| `admin/history/+page.svelte` | `.hist-table` (3 tables) | Has hover already; remove per-table CSS, keep `.td-num` → `.algo-table-num` |
| `admin/metrics/+page.svelte` | `.metrics-table` | Remove `var(--fs-xl)` override; rename `.num` → `.algo-table-num` |
| `admin/settings/+page.svelte` | inline `text-[0.65rem]` | Remove inline Tailwind size |
| `admin/alerts/+page.svelte` | `.alerts-table` | Standard migration |
| `admin/research/+page.svelte` | `.drafts-table`, `.audit-table`, `.mint-grid` | Rename `.audit-table` → `.research-table` to avoid collision with audit page |
| `admin/perf/+page.svelte` | `.perf-hotspot-table` | Standard migration |
| `admin/simulator/iterations/+page.svelte` | `.iter-table` | Standard migration |
| `admin/+page.svelte` | `.ip-modal-tbl` | Modal table — apply algo-table |
| `strategies/+page.svelte` | `.strat-table` | Standard migration; keep strategy-status color chips |
| `strategies/[id]/+page.svelte` | `.strat-table` | Same as above |
| `lib/NavBreakdown.svelte` | Hand-rolled with hardcoded RGBA | Apply algo-table; remove hardcoded values |

**Health grid** (`admin/health/+page.svelte`): Uses a card-based `.health-grid` layout (not `<table>`).
Apply equivalent CSS variables: same font-size 0.72rem, same color `var(--algo-slate)`, same
border `rgba(126,151,184,0.10)` — use inline CSS vars, not `algo-table` class since it's not a `<table>`.

**Agent card text legibility** (`automation/+page.svelte`):
- Change `text-[0.55rem]` on long_name → `text-xs` (12px); color → `var(--algo-slate)`

---

### J — Activity panel consistency: replace UnifiedLog with ActivityLogSurface

**Root cause**: `/automation/activity/+page.svelte` renders `<UnifiedLog>` — a hand-rolled
single-tab log view — instead of `<ActivityLogSurface>`. Every other activity surface (console,
orders, dashboard, activity page) uses `ActivityLogSurface` → `LogPanel` and is therefore
identical in tab layout, row format, download, filter, and button order. The automation activity
panel looks and behaves differently.

**Fix** (`frontend/src/routes/(algo)/automation/activity/+page.svelte`):
- Remove `<UnifiedLog>` import and usage
- Import `ActivityLogSurface` from `$lib/ActivityLogSurface.svelte`
- Replace with:
  ```svelte
  <ActivityLogSurface
    context="page"
    label="ACTIVITY"
    defaultTab="agent"
    cardId="automation-activity"
    bind:accountFilter={_accountFilter}
    bind:availableAccounts={_availableAccounts}
    bind:levelFilter={_levelFilter} />
  ```
- Remove any `UnifiedLog`-specific state/props that are no longer needed
- Verify that the automation activity page shows the same multi-tab panel as console/orders

---

### K — Status card + layout grid SSOT

**Goal**: NavStrip, page header, card header, cards, status cards, and agent cards all read as one visual family. Exception: Pulse (ag-Grid with row-level tints) is intentionally different.

**Root cause**: `.algo-status-card` is the shared shell (both OrderCard and AgentCard use it ✓), but status ENCODING inside diverges:
- AgentCard: `data-status` attribute drives CSS border-glow + opacity via scoped rules in `+layout.svelte` — design-token approach ✓  
- OrderCard: inline Tailwind (`bg-green-500/15 text-green-400 border-green-500/40` etc.) on the status pill — disconnected from `data-status`, duplicates the encoding ✗

Additionally, card layout grids are ad-hoc per page (Tailwind `grid grid-cols-*` or page-scoped CSS) — no canonical class.

---

#### K1 — Unified status pill CSS via `data-status`

**`frontend/src/app.css`** — extend `.algo-status-card` data-status variants to also expose CSS custom properties that the inner pill can inherit:

```css
/* Status token layer — drives both card chrome AND inner pill */
.algo-status-card[data-status="running"]  { --st-fg: #fbbf24; --st-bg: rgba(251,191,36,0.12); --st-border: rgba(251,191,36,0.45); }
.algo-status-card[data-status="error"]    { --st-fg: #f87171; --st-bg: rgba(248,113,113,0.12); --st-border: rgba(248,113,113,0.45); }
.algo-status-card[data-status="complete"] { --st-fg: #4ade80; --st-bg: rgba(74,222,128,0.12); --st-border: rgba(74,222,128,0.40); }
.algo-status-card[data-status="rejected"] { --st-fg: #f87171; --st-bg: rgba(248,113,113,0.12); --st-border: rgba(248,113,113,0.45); }
.algo-status-card[data-status="cancelled"]{ --st-fg: #94a3b8; --st-bg: rgba(148,163,184,0.10); --st-border: rgba(148,163,184,0.30); }
.algo-status-card[data-status="inactive"] { --st-fg: var(--text-muted); --st-bg: rgba(126,151,184,0.08); --st-border: rgba(126,151,184,0.25); }
.algo-status-card[data-status="triggered"]{ --st-fg: #38bdf8; --st-bg: rgba(56,189,248,0.12); --st-border: rgba(56,189,248,0.40); }

/* Shared pill — apply via class="algo-status-pill" */
.algo-status-pill {
  font-size: 0.55rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  padding: 0.15rem 0.45rem;
  border-radius: 3px;
  white-space: nowrap;
  color: var(--st-fg, var(--text-muted));
  background: var(--st-bg, rgba(126,151,184,0.08));
  border: 1px solid var(--st-border, rgba(126,151,184,0.25));
}
```

**`frontend/src/lib/order/OrderCard.svelte`** — replace all inline Tailwind status pill classes with:
```svelte
<span class="algo-status-pill">{order.status}</span>
```
The `data-status` attribute (already present on the outer `.algo-status-card`) drives the colors automatically.
Map `data-status` values to order statuses: COMPLETE → complete, REJECTED → rejected, CANCELLED → cancelled, TRIGGER_PENDING/AMO_REQ_RECEIVED → running, etc.

**`frontend/src/routes/(algo)/automation/+page.svelte`** — agent status pill (ON/OFF + mode L/P) already uses its own approach. Replace the inline `bg-sky-500/15` / `bg-red-500/15` Tailwind on the ON/OFF toggle pill with `.algo-status-pill` too, or at minimum ensure the border+glow comes from CSS vars not Tailwind.

---

#### K2 — Canonical `.page-grid` layout class

**`frontend/src/app.css`** — add:
```css
/* Canonical responsive grid for card pages */
.page-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(18rem, 1fr));
  gap: 0.5rem;
  align-items: start;
}
.page-grid-2col { grid-template-columns: 1fr 1fr; }
.page-grid-3col { grid-template-columns: 1fr 1fr 1fr; }
@media (max-width: 640px) {
  .page-grid, .page-grid-2col, .page-grid-3col { grid-template-columns: 1fr; }
}
```

Migrate the main card-layout pages to use `.page-grid` instead of ad-hoc Tailwind:
- `automation/+page.svelte` → replace `.agent-group-grid` with `page-grid`
- `orders/+page.svelte` → replace `grid grid-cols-5 gap-2` status counter strip with `page-grid`
- Any other page with a `grid grid-cols-* gap-*` card layout

---

#### K3 — Visual hierarchy enforcement (CSS-only, no new components)

The NavStrip → PageHeader → CardHeader → Card family already shares:
- Amber (`--c-action`) for titles and primary labels
- Muted slate (`--text-muted`) for secondary text and timestamps
- `var(--card-bg-gradient)` / `var(--card-bg-elevated)` for card backgrounds
- `CardHeader.svelte` as the shared card header ✓

No new components needed. The only CSS gap is: some cards use `.algo-card` (no box-shadow), some use `.bucket-card` (elevated, shadow), some use `.algo-status-card` (with status chrome). Add a note to `app.css` clarifying intended usage:
- `.algo-card` → data-only panels (no interactivity, no status)
- `.bucket-card` → primary work surface (order entry, chart, main content)
- `.algo-status-card` → any card that has a running/error/inactive state (orders, agents, connections)

This guidance (as a comment in `app.css`) prevents future drift without requiring a refactor.

---

## Agents

- frontend (Pass A — button reorder + download fix + NavStrip cleanup + row alternation + chase move):
  `frontend/src/lib/CardControls.svelte` (button order),
  `frontend/src/lib/DefaultSizeButton.svelte` (remove dsb-label span),
  `frontend/src/lib/LogPanel.svelte` (button reorder + download fix + JS-applied row stripes),
  `frontend/src/lib/PositionStrip.svelte` (remove ⓘ icons, update label texts),
  `frontend/src/app.css` (remove fs restore-text CSS rules),
  `frontend/src/routes/(algo)/orders/+page.svelte` (move chase L/M/H from header to symbol row, dashboard defaultTab fix),
  `frontend/src/routes/(algo)/dashboard/+page.svelte` (defaultTab="news" → "order")

- frontend (Pass A3 — status card SSOT + page-grid layout):
  `frontend/src/app.css` (data-status CSS vars + .algo-status-pill + .page-grid classes + usage comments),
  `frontend/src/lib/order/OrderCard.svelte` (replace Tailwind status pills with algo-status-pill),
  `frontend/src/routes/(algo)/automation/+page.svelte` (agent ON/OFF pill → algo-status-pill),
  pages using ad-hoc grid-cols-* → replace with page-grid

- frontend (Pass A2 — exp close grid alternation + ALL hand-rolled table consistency + activity SSOT):
  `frontend/src/routes/(algo)/admin/derivatives/+page.svelte` (replace nth-child with index classes),
  `frontend/src/routes/(algo)/admin/derivatives/CandidateLegRow.svelte` (accept stripe prop),
  `frontend/src/app.css` (add `.algo-table`, `.algo-table-wrap`, `.algo-table-num` SSOT classes),
  `frontend/src/routes/(algo)/admin/tokens/+page.svelte`,
  `frontend/src/routes/(algo)/admin/brokers/+page.svelte`,
  `frontend/src/routes/(algo)/admin/audit/+page.svelte`,
  `frontend/src/routes/(algo)/admin/statements/+page.svelte`,
  `frontend/src/routes/(algo)/admin/history/+page.svelte`,
  `frontend/src/routes/(algo)/admin/metrics/+page.svelte`,
  `frontend/src/routes/(algo)/admin/settings/+page.svelte`,
  `frontend/src/routes/(algo)/admin/alerts/+page.svelte`,
  `frontend/src/routes/(algo)/admin/research/+page.svelte`,
  `frontend/src/routes/(algo)/admin/perf/+page.svelte`,
  `frontend/src/routes/(algo)/admin/health/+page.svelte`,
  `frontend/src/routes/(algo)/admin/+page.svelte`,
  `frontend/src/routes/(algo)/admin/simulator/iterations/+page.svelte`,
  `frontend/src/routes/(algo)/strategies/+page.svelte`,
  `frontend/src/routes/(algo)/strategies/[id]/+page.svelte`,
  `frontend/src/lib/NavBreakdown.svelte`,
  `frontend/src/routes/(algo)/automation/+page.svelte` (agent card long_name text fix),
  `frontend/src/routes/(algo)/automation/activity/+page.svelte` (UnifiedLog → ActivityLogSurface)

- frontend (Pass B — AlgoTimestamp component + stores cleanup):
  create `frontend/src/lib/AlgoTimestamp.svelte`,
  `frontend/src/lib/stores.js` (remove formatIstOnly),
  `frontend/src/routes/(algo)/+layout.svelte` (algo-ts CSS cleanup)

- frontend (Pass C — 54 page files timestamp replacement):
  grep all pages with `algo-ts-group`, replace with `<AlgoTimestamp />`, remove associated state/imports

- playwright (smoke): update `frontend/e2e/polish-round6.spec.js` — assert dual-TZ format,
  desktop shows two timestamps, refresh is amber, button order correct (search before download),
  download button active on Orders/Agents tabs in activity, chase L/M/H appears after symbol in order entry

## Tests
- pytest: no
- svelte-check: yes
- playwright: yes

## Commit message
fix(ui): algo-table SSOT, activity SSOT, button order, chase to symbol row, AlgoTimestamp dual-TZ, NavStrip hints, row alternation fixed

## Done when
- CardControls order: Search → Download → Collapse → Fullscreen (default); X last (fullscreen)
- No "Restore" text in fullscreen mode
- LogPanel lp-card-btns same order
- Dashboard activity `defaultTab="order"` — download button active on all non-news tabs
- Chase L/M/H appears after symbol in order entry form row (not in card header)
- PositionStrip: no ⓘ icons; P/M/C/H labels explain all slots on click
- Desktop: current (sky) + refresh (amber) side-by-side on all pages
- Mobile: current by default, click toggles to refresh (amber)
- 54 pages use `<AlgoTimestamp />`
- Exp close grid and LogPanel non-Orders tabs: correct alternating rows via JS index classes
- ALL 15+ hand-rolled tables use `.algo-table`: 0.72rem body, 0.6rem header, alternating rows, hover, border rgba(126,151,184,0.10)
- Automation activity panel uses `ActivityLogSurface` (same as console/orders/dashboard)
- Automation agent card long_name text legible (min text-xs)
- `.algo-status-pill` class used for ALL status pills (orders + agents) — driven by `data-status` CSS vars
- OrderCard status pill uses `data-status` CSS tokens (not inline Tailwind)
- `.page-grid` canonical layout class in `app.css`; card-layout pages migrated away from ad-hoc Tailwind grid
- `.algo-card` / `.bucket-card` / `.algo-status-card` usage documented in `app.css`
- svelte-check 0 errors, playwright passing
