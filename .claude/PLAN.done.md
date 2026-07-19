# Plan: UI Polish Round 3 — Timestamps, Filters, Demo Banner, Payoff SSOT, Fullscreen

## Task

Fix 10 post-deploy issues across the frontend and one backend fix (ntfy priority). Issues span:
(1) mobile timestamp toggle broken, (2) filters in wrong order, (3) bio color swap,
(4) demo banner floating gap, (5) payoff chart SSOT, (6) risk data desktop layout,
(7) ntfy high-priority, (8) agents tab amber separator, (9) ag-Grid row consistency,
(10) smart fullscreen button.

## Agents

### frontend:
**12 targeted fixes across 8 files:**

**F1 — Mobile timestamp toggle (26 algo pages via stores.js pattern)**
Current bug: `_showLiveTs` toggles but `formatDualTz(0)` returns `''` when no
`lastRefreshAt`, making the refresh span empty — user sees bare `|` and perceives it as
everything disappearing. Vsep also must be conditional.

In every algo page that has the `algo-ts-group` pattern
(`frontend/src/routes/(algo)/{activity,agents,automation,chart,console,dashboard,...}/+page.svelte`):
- The vsep `<span class="algo-ts-vsep">|</span>` MUST be wrapped in `{#if $lastRefreshAt}` so it
  only shows when refresh data exists.
- The live ts `onclick` handler MUST be gated: `onclick={() => { if ($lastRefreshAt) _showLiveTs = !_showLiveTs; }}`
- The live ts should ONLY get `algo-ts-hidden` class when `$lastRefreshAt` is truthy AND
  `_showLiveTs` is true: `class:algo-ts-hidden={!!$lastRefreshAt && _showLiveTs}`
- When no `lastRefreshAt`: add CSS animation `.algo-ts-pulse` (keyframe: opacity 0.5→1 @1.5s ease-in-out infinite)
  applied as `class:algo-ts-pulse={!$lastRefreshAt}`

Reference: the pulse page (`frontend/src/routes/(algo)/pulse/+page.svelte`) already has the
correct conditional vsep pattern (`{#if _moversAsOf}` block around vsep + first ts).
Apply the same conditional logic using `$lastRefreshAt` across all 26 pages.

The CSS keyframe should be added at the page level OR in a global style block
(note: `app.css` is a safe place if it's not scoped). If adding per-page is too redundant,
add to app.css as `.algo-ts-pulse { animation: algo-ts-pulse-kf 1.5s ease-in-out infinite; }
@keyframes algo-ts-pulse-kf { 0%,100% { opacity:1; } 50% { opacity:0.4; } }`.

**F2 — ActivityHeaderFilters AFTER tabs (`frontend/src/lib/LogPanel.svelte:1409-1421`)**
Currently `ActivityHeaderFilters` is the FIRST child of `.lp-tab-strip-wrap`, before `AlgoTabs`.
User wants filters AFTER the tabs. Swap order inside the div:
```
<div class="lp-tab-strip-wrap">
  <AlgoTabs ... />   ← tabs FIRST
  <ActivityHeaderFilters ... />   ← filters AFTER
</div>
```

**F3 — Showcase bio color swap (`frontend/src/routes/(algo)/showcase/+page.svelte`)**
Swap the colors between contact button text and bio text:
- `.show-contact-btn { color: rgba(226, 232, 240, 0.88); }` (was `#7dd3fc`)
- `.show-attribution { color: #7dd3fc; }` (was `rgba(226, 232, 240, 0.88)`)

**F4 — Demo banner vertical gap (`frontend/src/routes/(algo)/+layout.svelte`)**
Current bug: demo banner is in normal flow before `<main>`, creating a 2rem phantom gap.
The fixed navbar (z-index 50) and fixed ps-strip cover the banner (z-index 10).
The 2rem of flow space pushes `<main>` down by 2rem, creating a visible gap.

Fix:
1. Change `.demo-banner` to `position: fixed; top: 3rem; left: 0; right: 0; z-index: 48;` (above page-header:45, below navbar:50)
2. Add: `:global(.algo-viewport:has(.ps-strip)) .demo-banner { top: calc(3rem + 1.5rem); }` (shift below ps-strip)
3. Add: `:global(.algo-card:has(.demo-banner) .page-header) { top: calc(3rem + 2rem); }` (page-header clears demo banner)
4. Add: `:global(.algo-viewport:has(.ps-strip):has(.demo-banner) .page-header) { top: calc(3rem + 1.5rem + 2rem); }`
5. Add: `:global(.algo-card:has(.demo-banner)) .algo-content { padding-top: calc(3rem + 2rem + 1.8rem); }`
6. Add: `:global(.algo-viewport:has(.ps-strip):has(.demo-banner)) .algo-content { padding-top: calc(3rem + 1.5rem + 2rem + 1.8rem); }`
Note: remove the `width: 100%` rule that was added since fixed + left:0+right:0 handles full-width.

**F5 — Payoff SSOT (`frontend/src/lib/MarketPulse.svelte:3630-3634`)**
`ctxOpenOptions(row)` navigates to `/admin/derivatives?symbol=${sym}` where `sym = row.tradingsymbol`
(a full contract symbol like "NIFTY24DEC18000CE"). The derivatives page reads `?u=` for underlying.
Fix: use `row.underlying` if present, else strip the contract symbol to the underlying name.
```js
function ctxOpenOptions(row) {
  closeContextMenu();
  const underlying = encodeURIComponent(
    row.underlying || row.tradingsymbol || ''
  );
  window.location.href = `/admin/derivatives?u=${underlying}`;
}
```

**F6 — Risk/reward single row desktop (`frontend/src/routes/(algo)/admin/derivatives/+page.svelte`)**
The `.opt-kv` div at line 4693 (Risk & expected value block) uses `grid-template-columns: 1fr 1fr`
(2-column layout). On desktop (≥1180px) show all items in a single horizontal row, same
technique as `.opt-kv-greeks`:
1. Add class `opt-kv-risk` to the risk section div at line 4693
2. Add at-media override:
```css
@media (min-width: 1180px) {
  .opt-kv-risk {
    grid-template-columns: repeat(auto-fit, minmax(0, max-content));
    column-gap: 1rem;
  }
  .opt-kv-risk .kv-pair {
    display: contents;
  }
  .opt-kv-risk .kv-v {
    margin-left: 0.3rem;
    margin-right: 0.8rem;
    text-align: left;
  }
}
```

**F7 — Agents amber separator (`frontend/src/lib/LogPanel.svelte`)**
Investigate: the "thick amber separator" is most likely from one of:
(a) The `.opt-block-h` amber border-bottom visible in the agents content area
(b) First agent row having `log-agent-triggered` class (color: #fb923c) that looks like a separator  
(c) ActivityHeaderFilters level-select showing with amber border only in agents tab

To fix: read the visual rendering of the agents tab content area. If the first visible row
has orange text creating a separator-like visual, add `border-top: 1px solid rgba(255,255,255,0.07)`
to `.log-panel.log-rows` to create a clear visual separation between header and content.
If it's the level-select amber border (`.act-level-sel { border: 1px solid rgba(251,191,36,0.25) }`),
reduce its opacity or change to match account-filter styling.

**F8 — ag-Grid snapshot vs legs (`frontend/src/lib/MarketPulse.svelte` + derivatives)**
Investigate: the "snapshot" grid in pulse uses `ag-theme-quartz ag-theme-algo` CSS.
The "legs" display in derivatives uses a custom CSS subgrid (NOT ag-Grid).
If the user is comparing pulse positions/holdings grid vs derivatives custom grid, ensure:
- Row height consistency: verify `rowHeight` prop passed to each ag-Grid
- `rowClassRules` consistency: both grids should use same row alternate coloring
Read `MarketPulse.svelte` around gridOptions for positions/holdings vs the custom `.cand-grid`
in `CandidateLegRow.svelte`. If there's a concrete difference in row height or alternating
color rules, align them. If investigating reveals they're different UI elements, document what
the user likely means and ensure visual consistency.

**F9 — Smart fullscreen button (`frontend/src/lib/CardHeader.svelte`)**
Add `detectOverflow` boolean prop (default false). When true:
1. In CardHeader, get a ref to the parent container (`<slot>` host) using `$host()` or `bind:this`
2. Use ResizeObserver inside `$effect()` to watch the container
3. Set `_hasOverflow = el.scrollHeight > el.clientHeight + 4 || el.scrollWidth > el.clientWidth + 4`
4. Show fullscreen button only when `_hasOverflow || isFullscreen`

Implementation approach:
```js
// In CardHeader.svelte
let detectOverflow = false; // new prop
let _hasOverflow = $state(false);
let _containerEl = $state(null);

$effect(() => {
  if (!detectOverflow || !_containerEl?.parentElement) return;
  const el = _containerEl.parentElement;
  const obs = new ResizeObserver(() => {
    _hasOverflow = el.scrollHeight > el.clientHeight + 4 || el.scrollWidth > el.clientWidth + 4;
  });
  obs.observe(el);
  return () => obs.disconnect();
});
```

In the CardHeader template: `<span bind:this={_containerEl} class="ch-overflow-anchor" aria-hidden="true" style="display:none"></span>`
Show fullscreen button: `{#if !detectOverflow || _hasOverflow || isFullscreen}`.

Update cards that should use smart fullscreen to pass `detectOverflow={true}`. Start with
MarketPulse position/holdings cards (which have fixed `--bucket-rows` heights that can overflow)
and LogPanel.

### broker: skip

### doc: skip

### backend-test: skip

### playwright: skip (changes are CSS/layout — existing specs cover modal behavior)

### backend (notify_deploy.py):
Add `"Priority": "high"` to the ntfy request headers in
`webhook/notify_deploy.py` at the existing ntfy block (lines 141-149).
Change:
```python
headers={"Title": event_label, "Tags": "rocket", "Content-Type": "text/plain"}
```
To:
```python
headers={"Title": event_label, "Tags": "rocket", "Priority": "high", "Content-Type": "text/plain"}
```
Dev-deploy suppression is already in place (line 66-68: `if is_non_main: sys.exit(0)`). No other change needed.

## Tests
- pytest: no (no backend logic changes)
- svelte-check: yes (26 pages + 8 files changed)
- playwright: no (layout fixes — existing modal specs still valid)

## Commit message
```
fix(ui): timestamp mobile toggle, demo banner position, filters order, payoff SSOT, risk row layout, ntfy priority
```

## Done when
- Mobile: tapping page timestamp toggles to refresh stamp (vsep hidden when no refresh stamp; live ts pulses when no refresh data)
- Desktop: both stamps visible simultaneously (no change)
- ActivityHeaderFilters (All accounts / Level) appear to the RIGHT of tabs, not left
- Showcase bio text is sky-blue (#7dd3fc), contact button text is off-white
- Demo banner appears as a fixed strip between NavStrip and page-header row with no phantom gap; no gap visible when not in demo mode
- "Open in Options →" context menu in MarketPulse navigates to derivatives with `?u=<underlying>` correctly pre-selecting the underlying
- Risk & expected value block shows all items in a single row on desktop (≥1180px); mobile unchanged
- ntfy fires for prod deployments with Priority: high; dev deploys still suppressed
- Agents tab visual separator (amber) investigated and eliminated
- ag-Grid row formatting consistent across pulse grids
- Fullscreen button on cards with `detectOverflow=true` appears only when content overflows
