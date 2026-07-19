# Plan: UI Polish Round 5 — minimize label, NavStrip hints, timestamp toggle, collapse fix, fullscreen detection fix + activity gap, button spacing, mobile overflow, grid row shading

## Context

Nine UX issues queued:
1. Fullscreen restore button exists (DefaultSizeButton) but icon alone isn't readable as "Minimize"
2. NavStrip (PositionStrip) P/M/C/H labels have no tap-friendly tooltip — only desktop `title=""`
3. Mobile timestamp: whole group not tappable as one unit (vsep `|` is a dead zone), no color distinction between live and refresh timestamps; desktop should show both without toggle
4. Collapse/expand button: visual state changes but ag-Grid body stays visible — CSS doesn't hide grid content on collapse
5. Fullscreen button missing from Positions/Holdings CardHeaders — `detectOverflow` ResizeObserver watches card container scroll, but ag-Grid manages its own internal scroll so container `scrollHeight` never exceeds `clientHeight`, leaving `_hasOverflow=false` permanently
6. Activity header gap — when System or Conn tab is active, a gap appears between the "All accounts" dropdown and the "All" level dropdown. Root cause: `.act-acct { margin-left: auto }` in `ActivityAccountSelect.svelte` is a stale comment artifact — right-alignment of card buttons is already handled by `lp-tab-strip-wrap: flex: 1 1 0`, not by the auto margin
7. Card button group spacing inconsistent — `.ch-right { gap: 0.25rem }` in CardHeader, but sibling selectors in `DefaultSizeButton.svelte` (`.fs-btn + .default-btn { margin-left: 0.3rem }`) and `CollapseButton.svelte` (`.collapse-btn + :global(.fs-btn) { margin-left: 0.3rem }`) add extra margin for specific adjacent pairs, resulting in 0.25rem between some buttons and 0.55rem between others
8. Mobile viewport overflow — NavStrip (PositionStrip) and page header row content overflows the viewport width on mobile. Pills in the strip may exceed viewport; page header `overflow: visible` lets content bleed past right edge on narrow screens
9. ag-Grid alternating row shading inconsistent — "nav grid" rows (likely positions/holdings ag-Grid near the NavStrip area) not using `--ag-odd-row-background-color` CSS variable; LogPanel/activity tab rows have no alternating row shading at all

**Audit findings — automation/strategies/console page consistency (items 10-14):**
10. Agent chip unreadable — `app.css:1381 .log-panel .log-agent-chip { font-size: 0.5rem }` (8px) — far too small to read; the amber "agent #N" chip inside log rows is invisible at this size
11. Automation filter chip inconsistency — `automation/agent-templates/+page.svelte` uses `.filter-btn` (`var(--fs-md)` = 0.65rem, border-radius `0.25rem`) while `automation/templates/+page.svelte` uses `.tpl-chip` (`var(--fs-lg)` = 0.7rem, border-radius `4px`) — same UI pattern, different styles
12. Automation action button inconsistency — `.action-btn` (agent-templates: border-radius `0.25rem`, padding `0.22rem 0.65rem`, font-weight 500) vs `.tpl-btn` (templates: border-radius `4px`, padding `0.35rem 0.85rem`, font-weight 600) — should share one button style
13. Strategies pill inconsistency — list page (`strategies/+page.svelte:392`) uses `var(--fs-xs)` + `0.05rem 0.4rem` padding; detail page (`strategies/[id]/+page.svelte:586`) uses `var(--fs-sm)` + `0.1rem 0.5rem` — standardize to `var(--fs-sm)` + `0.1rem 0.4rem`
14. Strategies table chrome inconsistency — list page uses plain `border: 1px solid rgba(126,151,184,0.18)` table; detail page uses `.algo-grid-chrome` (gradient bg + shadow + 1.5px border) — apply `.algo-grid-chrome` to list table too

Note: "sandbox" and "build" routes do not exist as separate pages. Closest equivalents: `console` page + automation workspace sub-pages. No separate items needed.

## Task

### Item 1 — Fullscreen minimize text label
**`frontend/src/lib/DefaultSizeButton.svelte`**: Add `<span class="dsb-label">Minimize</span>` after `</svg>` inside the `{#if isFullscreen}` block. Add `.dsb-label { display: none; }` to `<style>`.

**`frontend/src/app.css`** (after the `.fs-card-on` block ~line 1807):
```css
.fs-card-on .default-btn {
  padding: 0 0.4rem;
  width: auto;
  gap: 0.3rem;
}
.fs-card-on .default-btn .dsb-label {
  display: inline;
  font-size: 0.65rem;
  font-weight: 500;
  letter-spacing: 0.02em;
}
```

### Item 2 — NavStrip label-as-tooltip (no new chip)
**`frontend/src/lib/PositionStrip.svelte`**:

Use the label letters (P/M/C/H) themselves as the InfoHint trigger. Instead of adding a separate (i) chip, wrap each `ps-agg-k` span content to show a tooltip on click. Use `title=""` replacement via InfoHint by making the label span carry `tabindex="0"` + `role="button"` and toggling a local popup state, OR: wrap the label letter in `<InfoHint popup text="...">` using the `label` slot/children approach.

Actually the simplest approach: import InfoHint and use its children snippet to replace the plain letter text — so the letter IS the trigger:
```svelte
<span class="ps-agg-k ps-k-p">
  <InfoHint popup label="P" text="<b>P — Positions P&L</b> · ...">P</InfoHint>
</span>
```
But InfoHint renders a separate button. Better: add `title=""` at the individual value level AND replace the container `title` with an InfoHint on the label letter.

**Actual implementation**: Add `<InfoHint popup text="...">` directly inside each `ps-agg-k` span, using `label="P"` prop (InfoHint accepts `label` to override the "(i)" chip text). The InfoHint chip text becomes "P", "M", "C", "H" — so the label IS the chip.

Check InfoHint props: it has `label?: string (default: 'i')`. So `<InfoHint popup label="P" text="...">` renders "P" as a styled chip — the whole label is the button. Remove the plain text "P" and let InfoHint render it as an interactive chip.

Import InfoHint. Add the 4 InfoHint instances replacing each `ps-agg-k` inner text:
- P: `"<b>P — Positions P&L</b> · Three slots: <b>Day P&L</b> (live ticks − prev-close × net qty, all accounts) / <b>Lifetime P&L</b> (cumulative since open) / <b>Expiry P&L</b> (projected F&O value at expiry via lognormal model). Tap the Day P&L value for a per-position breakup."`
- M: `"<b>M — Margin</b> · Available / Total. <b>Available</b> = cash deployable right now for new orders. <b>Total</b> = used margin + available (full collateral picture, all accounts)."`
- C: `"<b>C — Cash</b> · CA (Available) / C (Total). <b>CA</b> = live deployable cash, nets realised P&L + premium debits. <b>Total C</b> = CA + premium tied up in long options (recoverable if closed)."`
- H: `"<b>H — Holdings</b> · Three slots: <b>Today MTM</b> (live LTP − prev-close × qty) / <b>Current value</b> (broker-reported, all holdings) / <b>Lifetime P&L</b> (cumulative since purchase)."`

Add minimal CSS to `.ps-agg-k :global(.ih-btn)` to match the strip's compact scale (override to use monospace bold style matching the existing `ps-agg-k` look). Remove the existing container `title=""` attributes from all 4 `.ps-agg` spans.

### Item 3 — Timestamp toggle fix + remove pulse animation + two colors

**Root cause 1 — toggle broken**: `onclick` is on the inner live-clock span. When `_showLiveTs=true`, that span gets `display:none` on mobile — hidden elements can't be tapped. The vsep `|` separator has NO click handler (dead zone). User taps dead zone, nothing happens.

**Root cause 2 — blinking on launch**: `class:algo-ts-pulse={!$lastRefreshAt}` — `$lastRefreshAt` is `0` at page load and is only ever set by MarketPulse. On every non-Pulse page it stays `0` forever, so the 3-cycle animation plays on EVERY page load. Confusing — remove pulse animation entirely from all pages.

**Fix — 29 page files** (`grep -rl "algo-ts-group" frontend/src/routes/`):
1. Remove `class:algo-ts-pulse={!$lastRefreshAt}` from the live-clock span (eliminates launch blink)
2. Move `onclick` from inner spans to the outer `.algo-ts-group` span:
   - Add `onclick={() => { if ($lastRefreshAt) _showLiveTs = !_showLiveTs; }}` to the outer group span  
   - Add `role="button"` and `tabindex="0"` to the outer group span when `$lastRefreshAt` is set (or always, for simplicity)
   - Remove `onclick` from the inner live-clock span (keep onkeydown for a11y)
   - Remove `onclick` from the `.algo-ts-data` span (handled by outer group now)
3. Desktop: `algo-ts-hidden` only applies at `max-width:480px`. On desktop both timestamps always show. Clicking outer group toggles `_showLiveTs` but has no visible effect on desktop — correct behavior.

**Fix — `frontend/src/app.css`**: Remove the `.algo-ts-pulse` rule and `@keyframes algo-ts-pulse-kf` (no longer needed). Also add two-color distinction:
```css
/* Refresh timestamp — cyan to distinguish from live clock */
.algo-ts-data { color: var(--c-info); }
```
(Live clock keeps its inherited color; refresh ts gets distinct cyan.)

### Item 4 — Collapse/expand fix for ag-Grid cards

**Root cause**: `.is-collapsed` CSS (app.css lines 1928-1934) only changes padding. For ag-Grid cards, grid body is NOT inside `{#if !isCollapsed}` — it's always mounted. So collapse state changes but nothing is visually hidden.

**Fix** (`frontend/src/app.css`): Add a CSS rule that hides ag-Grid content when its card container is collapsed:
```css
/* ag-Grid body hidden when card is collapsed (grid is always mounted) */
.is-collapsed .ag-root-wrapper { display: none !important; }
```
Add this right after the existing `.is-collapsed` block (line 1934).

Also add for non-grid content inside collapsed cards:
```css
/* Non-header children hidden when card collapsed — for any directly nested content */
.is-collapsed .mp-bucket-body { display: none !important; }
```

Also ensure the card container gets `.is-collapsed` class when `isCollapsed=true`. Look at MarketPulse — check if `class:is-collapsed` is applied to the section wrapper or bucket wrap. If not, add it (bind `isCollapsed` from CardHeader back out to the container class binding).

Read `src/lib/MarketPulse.svelte` around the Positions/Holdings card sections to see exactly how `isCollapsed` flows and add `class:is-collapsed` to the outermost card container.

### Item 5 — Fullscreen button: generic overflow-aware detection for all cards

**Redesign goal**: The fullscreen button should auto-detect whether a card's content needs it. Show it when the card or its ag-Grid has overflow; hide it when content fits. Cards can opt out of detection with an explicit `detectOverflow={false}` prop.

**Root cause of existing bug**: `detectOverflow=true` in `CardHeader.svelte` uses `ResizeObserver` on the card CONTAINER (`_overflowAnchorEl.parentElement.parentElement`). ag-Grid manages its own internal `.ag-body-viewport` scroll — the card container's `scrollHeight` never exceeds `clientHeight`, so `_hasOverflow` stays `false` permanently for grid cards.

**Two-part fix in `frontend/src/lib/CardHeader.svelte`**:

**Part A — Change default**: Change `detectOverflow = false` (line 51) → `detectOverflow = true`. This makes overflow-based detection the default for all cards. Cards that should ALWAYS show the fullscreen button (charts, complex workspaces) must now explicitly pass `detectOverflow={false}`.

**Part B — Enhanced overflow check** (replace the `$effect` at lines 60-69):
```javascript
$effect(() => {
  if (!detectOverflow || !_overflowAnchorEl?.parentElement?.parentElement) return;
  const el = _overflowAnchorEl.parentElement.parentElement;

  const checkOverflow = () => {
    // 1. Container-level overflow (regular scrollable content)
    if (el.scrollHeight > el.clientHeight + 4 || el.scrollWidth > el.clientWidth + 4) {
      _hasOverflow = true; return;
    }
    // 2. ag-Grid internal overflow: row content taller than grid viewport
    //    .ag-center-cols-container = actual rendered rows (grows with data)
    //    .ag-body-viewport = fixed-height grid viewport
    const agRows = el.querySelector('.ag-center-cols-container');
    const agViewport = el.querySelector('.ag-body-viewport');
    if (agRows && agViewport && agRows.offsetHeight > agViewport.clientHeight + 4) {
      _hasOverflow = true; return;
    }
    _hasOverflow = false;
  };

  const obs = new ResizeObserver(checkOverflow);
  obs.observe(el);

  // Watch ag-Grid row container directly — grows as rows are added
  const agRows = el.querySelector('.ag-center-cols-container');
  if (agRows) obs.observe(agRows);

  // MutationObserver: wire up ag-Grid observation when grid initialises async
  let mutObs = null;
  if (!agRows) {
    mutObs = new MutationObserver(() => {
      const newAgRows = el.querySelector('.ag-center-cols-container');
      if (newAgRows) {
        obs.observe(newAgRows);
        mutObs?.disconnect(); mutObs = null;
        checkOverflow();
      }
    });
    mutObs.observe(el, { childList: true, subtree: true });
  }

  checkOverflow();  // initial check
  return () => { obs.disconnect(); mutObs?.disconnect(); };
});
```

**Part C — Per-card audit in all callers** (`grep -rn "CardHeader" frontend/src/`):

The agent must read each `<CardHeader>` usage and add `detectOverflow={false}` only for cards where fullscreen is ALWAYS useful regardless of content:
- `SymbolPanel.svelte`: chart workspace → `detectOverflow={false}` (charts always benefit)
- Derivatives page complex calc cards → audit content; if always dense, `detectOverflow={false}`  
- Lab/Console workspace cards → `detectOverflow={false}` (workspace surfaces always useful)
- All ag-Grid cards (Positions, Holdings, orders grids): remove existing `detectOverflow={true}` (now the default `true` handles it correctly with the enhanced check)
- Simple status cards, summary cards: leave at default `true` (auto-detect is correct)

The frontend agent must read each card's content to make the call. When in doubt, leave at default (auto-detect).

### Item 6 — Activity header gap

**`frontend/src/lib/ActivityAccountSelect.svelte`** line 52: remove `margin-left: auto` from `.act-acct`.

The comment "Claims the spacer slot so the close-button / CardControls cluster sits to the right" is incorrect — the right-alignment is handled by `lp-tab-strip-wrap { flex: 1 1 0 }` in LogPanel.svelte. The `margin-left: auto` on `.act-acct` (inside `.act-filters inline-flex` inside `lp-tab-strip-wrap`) creates spurious free space before "All accounts" when `.act-filters` has any free width.

Remove the `margin-left: auto` line and update/remove the stale comment.

### Item 7 — Card button group spacing

**Root cause**: `.ch-right { gap: 0.25rem }` in `CardHeader.svelte` line 155. But:
- `CollapseButton.svelte` line 164: `.collapse-btn + :global(.fs-btn) { margin-left: 0.3rem; }` — adds 0.3rem to the gap between Collapse and Fullscreen buttons
- `DefaultSizeButton.svelte` lines 105-115: `:global(.fs-btn + .default-btn) { margin-left: 0.3rem; }` and `:global(.default-btn + .collapse-btn) { margin-left: 0.3rem; }` and `:global(.fs-btn + .collapse-btn) { margin-left: 0.3rem; }`

These produce 0.55rem spacing between some pairs vs 0.25rem between others.

**Fix**:
1. `frontend/src/lib/CardHeader.svelte` — change `.ch-right { gap: 0.25rem }` → `gap: 0.3rem`
2. `frontend/src/lib/CollapseButton.svelte` — remove lines 164-166 (`.collapse-btn + :global(.fs-btn) { margin-left: 0.3rem; }`)
3. `frontend/src/lib/DefaultSizeButton.svelte` — remove lines 105-115 (all three `:global(...)` sibling margin rules)
4. `frontend/src/app.css` lines 1909-1913 — remove `.ams + .collapse-btn, .ams + .fs-btn, .collapse-btn + .fs-btn { margin-left: 0.3rem !important; }` (same pattern, duplicates the inconsistency globally)

With uniform `gap: 0.3rem` in `.ch-right` and no sibling overrides, all adjacent button pairs get equal spacing.

### Item 8 — Mobile viewport overflow (NavStrip + page header)

**PositionStrip**: `.ps-strip { overflow-x: auto }` on mobile should allow pills to scroll. Check if `.ps-agg` pills are `flex-shrink: 0` — if so, add `max-width: 100%` or reduce padding on pills at `max-width: 380px`. Also verify the InfoHint additions from item 2 don't make the pills wider than their column.

**Page header**: `position: fixed; left: 0; right: 0; overflow: visible`. Content can bleed past right edge. Check what element overflows — likely the `.algo-ts-group` (dual timestamp including both live clock and refresh) or `.page-header-actions` cluster. On mobile:
- `.algo-ts-group`: ensure `min-width: 0; overflow: hidden` or `text-overflow: ellipsis` on the timestamp spans
- `.page-header` at ≤480px: if `flex-wrap: wrap` isn't firing correctly, add `overflow-x: hidden` to `.page-header` (note: this clips tooltips but the dropdown is a portal so it should be fine)
- Check if `algo-ts-group` has `white-space: nowrap` set somewhere that prevents wrapping

Investigate both in context. The goal is that at 360px viewport width, neither strip shows a horizontal scrollbar or bleeds content past the right edge.

### Item 9 — ag-Grid / log row alternating shading

**"Nav grid"** (positions/holdings grids near NavStrip): Ensure all ag-Grid instances in `MarketPulse.svelte` (Positions, Holdings) use `--ag-odd-row-background-color` via the theme. They should already inherit from the global ag-Grid theme in app.css (line 1007: `--ag-odd-row-background-color: rgba(13,22,42,0.30)`). Check if any grid has `rowStyle` or `getRowStyle` overriding the theme — if so, remove the override and let the CSS variable handle it.

**Activity tab (LogPanel) rows**: `.log-row` elements currently have no alternating row background. Add:
```css
/* LogPanel.svelte <style> */
:global(.log-panel.log-rows .log-row:nth-child(odd)) {
  background: var(--ag-odd-row-background-color, rgba(13,22,42,0.30));
}
```
This makes activity log rows visually consistent with ag-Grid rows on the same pages.

Also check other non-ag-Grid table-like surfaces (Snapshot rows, CandidateLegRow already fixed in Round 4 — don't regress those).

### Item 10 — Agent chip font size fix

**`frontend/src/app.css` line 1381**: `.log-panel .log-agent-chip { font-size: 0.5rem }` → `font-size: var(--fs-xs)`. Also `font-weight: 600` → `700`. The amber "agent #N" chip in order log rows is currently 8px — invisible to most users.

### Item 11 — Automation filter chip standardization

**`automation/agent-templates/+page.svelte`** (`.filter-btn`) and **`automation/templates/+page.svelte`** (`.tpl-chip`) implement the same horizontal filter chip pattern. Standardize both to:
- `font-size: var(--fs-sm)`
- `border-radius: 3px`
- `padding: 0.2rem 0.55rem`
- Inactive: `color: rgba(180,200,230,0.70)`, `background: rgba(255,255,255,0.04)`, `border: 1px solid rgba(180,200,230,0.18)`
- Active: `color: var(--c-action)`, `background: rgba(251,191,36,0.12)`, `border: rgba(251,191,36,0.45)`

Keep class names as-is, only update the CSS values.

### Item 12 — Automation action button standardization

**`automation/agent-templates/+page.svelte`** (`.action-btn`, `.primary-btn`) and **`automation/templates/+page.svelte`** (`.tpl-btn`, `.tpl-btn-primary`, `.tpl-btn-danger`) — standardize to:
- `font-size: var(--fs-sm)`, `font-weight: 600`, `border-radius: 3px`, `padding: 0.25rem 0.7rem`
- Default: `color: rgba(200,216,240,0.85)`, `background: rgba(255,255,255,0.04)`, `border: 1px solid rgba(180,200,230,0.2)`
- Hover: `color: var(--c-action)`, `background: rgba(251,191,36,0.10)`, `border: rgba(251,191,36,0.40)`
- Danger hover: `color: var(--c-short)`, `background: var(--c-short-10)`, `border: rgba(248,113,113,0.45)`
- Primary: `color: var(--c-action)`, `background: rgba(251,191,36,0.15)`, `border: rgba(251,191,36,0.5)`, `font-weight: 700`

### Item 13 — Strategies pill consistency

**`strategies/+page.svelte` lines 392-401**: `.pill-active`/`.pill-inactive` — change `font-size: var(--fs-xs)` → `var(--fs-sm)` and `padding: 0.05rem 0.4rem` → `0.1rem 0.4rem`. Matches detail page styling.

### Item 14 — Strategies list table chrome

**`strategies/+page.svelte`**: Update `.strat-table-wrap` to match detail page:
```css
.strat-table-wrap {
  border: 1.5px solid rgba(255,255,255,0.10);
  box-shadow: 0 2px 8px rgba(0,0,0,0.45), inset 0 1px 0 rgba(255,255,255,0.08);
  background: linear-gradient(180deg, #273552 0%, #1d2a44 100%);
  border-radius: 6px;
  overflow: hidden;
}
```

## Agents

- frontend (pass A — items 1-9, core polish): Files:
  - `frontend/src/lib/DefaultSizeButton.svelte` (item 1: dsb-label span; item 7: remove sibling margin overrides lines 105-115)
  - `frontend/src/app.css` (item 1: fs-card-on rules; item 3: .algo-ts colors; item 4: .is-collapsed ag-Grid rule; item 7: remove .collapse-btn + .fs-btn margin override lines 1909-1913; item 10: .log-agent-chip font-size → var(--fs-xs))
  - `frontend/src/lib/PositionStrip.svelte` (item 2: InfoHint with label="P/M/C/H"; item 8: mobile overflow check)
  - All 29 page files with `algo-ts-group` pattern (item 3: move onclick to outer group span)
  - `frontend/src/lib/MarketPulse.svelte` (item 4: is-collapsed class; item 5: remove explicit detectOverflow={true} — now handled by enhanced default)
  - `frontend/src/lib/ActivityAccountSelect.svelte` (item 6: remove margin-left: auto from .act-acct)
  - `frontend/src/lib/CardHeader.svelte` (item 5: enhanced overflow detection + default change; item 7: .ch-right gap: 0.3rem)
  - `frontend/src/lib/CollapseButton.svelte` (item 7: remove .collapse-btn + :global(.fs-btn) margin rule)
  - `frontend/src/lib/LogPanel.svelte` (item 9: add nth-child(odd) alternating background to .log-row)
  - `frontend/src/routes/(algo)/+layout.svelte` (item 8: page header mobile overflow fix)
  - All other `<CardHeader>` callers: `frontend/src/lib/SymbolPanel.svelte`, `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`, `frontend/src/routes/(algo)/dashboard/+page.svelte`, `frontend/src/routes/(algo)/automation/*.svelte`, etc. — add `detectOverflow={false}` where always-show is appropriate (item 5 Part C)
  
  For item 3 (29 files), use `grep -rl "algo-ts-group" frontend/src/routes/` to get the full list, then edit each. The pattern is consistent — the Svelte template for the timestamp group is nearly identical across all pages (auto-generated pattern).
  
  For item 9 (nav grid), check if Positions/Holdings ag-Grids in MarketPulse.svelte have any `rowStyle`/`getRowStyle` overriding the theme CSS variable — if so, remove. The global theme in app.css already sets `--ag-odd-row-background-color: rgba(13,22,42,0.30)` (line 1007) so properly-themed grids should already show alternating rows.

- frontend (pass B — items 11-14, consistency): Files:
  - `frontend/src/routes/(algo)/automation/agent-templates/+page.svelte` (item 11: standardize .filter-btn; item 12: standardize .action-btn/.primary-btn)
  - `frontend/src/routes/(algo)/automation/templates/+page.svelte` (item 11: standardize .tpl-chip; item 12: standardize .tpl-btn)
  - `frontend/src/routes/(algo)/strategies/+page.svelte` (item 13: .pill-active/inactive → var(--fs-sm) + 0.1rem 0.4rem; item 14: upgrade .strat-table-wrap to algo-grid-chrome style)
  
  Pass B can run in parallel with Pass A since they touch different files.

- backend: skip
- broker: skip
- doc: skip
- backend-test: skip
- playwright: Add tests for:
  1. Fullscreen minimize label: enter fullscreen on a card, verify `.default-btn` text contains "Minimize"
  2. NavStrip P label: tap P label, verify a popup/tooltip opens with "Day P&L" text
  3. Collapse fix: collapse a card, verify `.ag-root-wrapper` inside it is not visible (or has display:none)
  4. Fullscreen button visibility: on `/pulse` or page with MarketPulse, verify Positions card has a visible fullscreen button (`.fs-btn` or `.default-btn` depending on state)
  5. Activity header gap: navigate to /activity or /orders, switch to System tab, verify `.act-acct` has no `margin-left: auto` computed style (or gap between account dropdown and level dropdown is ≤ 0.5rem)
  6. Card button spacing: verify all adjacent button pairs in `.ch-right` have equal computed gap (within 1px tolerance)
  7. Mobile overflow: at 375px viewport width, verify `.ps-strip` and `.page-header` have no horizontal scroll (scrollWidth ≤ clientWidth)
  Add to existing spec files.

## Tests

- pytest: no
- svelte-check: yes
- playwright: yes

## Commit message

```
fix(ui): Polish Round 5 — minimize label, NavStrip hints, timestamp, collapse, fullscreen, gaps, mobile, row shading, chip/button consistency

- DefaultSizeButton: show "Minimize" text label inside .fs-card-on context
- PositionStrip: P/M/C/H labels become InfoHint triggers (label prop); popup tooltip
  on tap; remove desktop-only title="" from containers
- Timestamp: move onclick to .algo-ts-group so vsep dead-zone is covered; two-color
  treatment (.algo-ts-data cyan); remove pulse animation entirely
- Collapse: add .is-collapsed .ag-root-wrapper { display: none } to app.css; wire
  class:is-collapsed to MarketPulse card containers
- Fullscreen: CardHeader detectOverflow default→true (auto-detect); enhanced check
  covers both container scroll AND ag-Grid .ag-center-cols-container overflow;
  MutationObserver wires up ag-Grid watch when grid initialises async; cards that
  always want fullscreen (charts, workspaces) get explicit detectOverflow={false}
- Activity header: remove margin-left:auto from .act-acct; stale comment cleanup
- Card buttons: standardize .ch-right gap to 0.3rem; remove sibling margin-left
  overrides from DefaultSizeButton/CollapseButton/app.css for uniform spacing
- Mobile: fix NavStrip + page header overflow at narrow viewports (≤375px)
- Row shading: LogPanel .log-row nth-child(odd) alternating bg; verify ag-Grid
  theme inherits --ag-odd-row-background-color correctly across all cards
- Agent chip: .log-agent-chip font-size 0.5rem → var(--fs-xs) (was invisible at 8px)
- Automation: standardize filter chip + action button styles across agent-templates
  and templates sub-pages (same font-size/border-radius/padding)
- Strategies: unify pill sizing (--fs-sm) + apply algo-grid-chrome to list table
```

## Done when

- Fullscreen card shows "⊡ Minimize" text; clicking exits fullscreen
- P/M/C/H labels in PositionStrip are tappable and open popup with rich explanation
- Mobile: tapping anywhere in timestamp group (incl. vsep) toggles between timestamps
- Live clock and refresh timestamp have visually distinct colors
- Collapsing a card with ag-Grid hides the grid body
- Cards with ag-Grid overflow (more rows than fit): fullscreen button visible
- Cards with content that fits without scrolling: fullscreen button absent (auto-detected)
- Charts / workspace cards with detectOverflow={false}: fullscreen always visible
- Activity header: no gap before "All accounts" dropdown when System/Conn tab active
- Card buttons in .ch-right all have equal spacing (0.3rem)
- At 375px viewport: NavStrip and page header fit within viewport width (no horizontal scroll)
- LogPanel activity rows show alternating background on odd rows matching ag-Grid theme
- `.log-agent-chip` text is readable (≥0.55rem / var(--fs-xs), not 0.5rem)
- Automation agent-templates + templates filter chips: same font-size, border-radius, padding
- Automation action buttons: same border-radius, padding, font-weight across both sub-pages
- Strategies list + detail pills: same font-size (var(--fs-sm)) and padding
- Strategies list table has same chrome (gradient bg + shadow) as detail table
- svelte-check: 0 errors
- Playwright: all 7 smoke tests pass
