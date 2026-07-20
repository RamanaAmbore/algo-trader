# Plan: UI consistency round 8 — separators, row tint, timestamps, card width, activity

## Context
Nine interconnected UI consistency issues, most rooted in separate implementations where reusable code should be used. Grouped by root cause.

---

## Group A — Header separator SSOT (3 surfaces need it)

**A1. Page header separator** — no separator exists between title label and AlgoTimestamp.
Fix (CSS-only in `frontend/src/routes/(algo)/+layout.svelte` scoped style):
```css
:global(.page-header) .algo-title-group {
  align-self: stretch;
  display: inline-flex;
  align-items: center;
  border-right: 1px solid rgba(255,255,255,0.10);
  padding-right: 0.35rem;
  margin-right: 0.1rem;
}
```
This matches `.ch-sep` exactly. Zero page template changes.

**A2. LogPanel header separator** — `.lp-sep` CSS mirrors `.ch-sep` but the container uses Tailwind `items-stretch` (`align-items: stretch`) while CardHeader uses `align-items: center`. The difference: with `stretch`, ALL flex children expand to full row height, potentially misaligning label text.
Fix in `frontend/src/lib/LogPanel.svelte`:
- In the `log-tab-row` scoped CSS rule (line 1776: `.log-tab-row { gap: 0; }`), add `align-items: center;` so the row container matches CardHeader's behaviour.
- The `.lp-sep` already has `align-self: stretch` to span full height — this is correct and matches `.ch-sep`.
- Also verify `padding-right` on `.lp-label` is consistent (currently `0 0.5rem`).

---

## Group B — Row tint SSOT (2 surfaces, same color, different classes — unify)

**Root cause**: LogPanel uses `lp-row-odd`/`lp-row-even`, CandidateLegRow uses `cand-row-odd`/`cand-row-even`. Both define the same `rgba(13,22,42,0.30)` separately. `algo-table` uses `tr:nth-child(odd)`. Three systems for one visual.

**Fix**:
1. In `frontend/src/app.css`, after `.algo-table` block, add a global row-tint pair:
   ```css
   /* Shared alternating-row tint — used by LogPanel, exp-close grid, and any hand-rolled list */
   .row-tint-odd  { background: rgba(13,22,42,0.30); }
   .row-tint-even { background: transparent; }
   .row-tint-odd:hover, .row-tint-even:hover { background: rgba(255,255,255,0.02); }
   ```
2. In `frontend/src/lib/LogPanel.svelte`:
   - In `_agentRows`, `_simRows`, `_sysRows`, `_connRows` derived stores, change `'lp-row-even'`/`'lp-row-odd'` → `'row-tint-even'`/`'row-tint-odd'`
   - Also update the `.map()` at line 1298-1300 (Orders tab stripe injection) the same way
   - In the scoped CSS, remove the `lp-row-odd`/`lp-row-even` + `.log-row:hover` rules (replaced by global)
   - The CSS selector changes from `:global(.log-panel.log-rows .log-row.lp-row-odd)` to `:global(.log-panel.log-rows .log-row.row-tint-odd)` — but since the new global class handles backgrounds directly, may not need this context qualifier at all
3. In `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`:
   - Change `stripe={_ci % 2 === 0 ? 'cand-row-odd' : 'cand-row-even'}` → `'row-tint-odd'` / `'row-tint-even'`
   - Remove the `:global(.cand-row.cand-row-odd)` and `:global(.cand-row.cand-row-even)` rules (replaced by global)

---

## Group C — AlgoTimestamp (2 fixes)

**C1. Mobile font** — all timestamp spans use `font-size: inherit` with no mobile override.
Fix in `frontend/src/lib/AlgoTimestamp.svelte`, inside existing `@media (max-width: 640px)` block:
```css
.ats-group { font-size: 0.6rem; }
```

**C2. Mobile toggle not working** — `_toggle()` is gated on `if (_refreshTs)`. On mobile, if no page refresh has happened yet (lastRefreshAt = 0), clicking does nothing AND gives no visual feedback. Also verify that `onclick` on the `<span>` fires on iOS Safari touch (known quirk: non-interactive elements need `cursor: pointer` to receive touch events — already present, but also check that no ancestor has `pointer-events: none`).
Fix: Add `_noRefresh` state feedback and ensure the toggle always fires:
- Remove the `if (_refreshTs)` guard from `_toggle()` — if `_refreshTs` is null, `_showRefresh` would toggle to true but the `{#if _refreshTs}` block is false so nothing bad renders; `ats-now` would get `ats-mobile-hide` and nothing shows — this IS the bug.
- Better fix: in `_toggle()`, only toggle if `_refreshTs` is truthy AND on mobile. The guard should remain. But the click area needs visual feedback.
- Actually best fix: check WHY `lastRefreshAt` stays 0. In `stores.js`, `lastRefreshAt = writable(0)` — it's set when a page refresh completes. If the page auto-refreshes on a timer (via `marketAwareInterval`), `lastRefreshAt` gets updated. Investigate: is `lastRefreshAt` being updated correctly after a data fetch? If it is, the toggle should work once data loads.
- If the toggle guard works but clicks aren't registering: add `cursor: pointer` to `.ats-group` in CSS (it's currently inline style, which Svelte might strip — move it to CSS to be safe).
Frontend agent: investigate the actual toggle failure. Check if `lastRefreshAt` is updating and if the click event fires on mobile.

---

## Group D — Functional fixes (3 items)

**D1. Status card 2x width** — agent cards in `automation/+page.svelte` should span 2 grid columns.
In `frontend/src/app.css`, after `.algo-card-wide`:
```css
.algo-status-card-2x { grid-column: span 2; }
@media (max-width: 640px) { .algo-status-card-2x { grid-column: span 1; } }
```
In `frontend/src/routes/(algo)/automation/+page.svelte`, agent card div (line ~706): add `algo-status-card-2x` to class list. `OrderCard.svelte` untouched.

**D2. Dashboard defaultTab="news"** — Round 7 changed this to "order" to fix download. But "news" was the intended default. Fix both:
- In `frontend/src/routes/(algo)/dashboard/+page.svelte` (~line 2199): change `defaultTab="order"` → `defaultTab="news"`
- In `frontend/src/lib/LogPanel.svelte`, find the download button handler. The `aria-label` at line 1464 says `logTab === 'news' ? 'Download not available for News tab' : 'Download CSV'` — this is a label-only change, not a functional gate. Check the actual download `onclick` — if it's also gated on `logTab !== 'news'`, remove that gate (news tab download should either export headlines or show a graceful message, but should NOT be silently inactive).

**D3. Watchlist tabs investigation** — User reports watchlist tab(s) disappeared from MarketPulse pinned card. MarketPulse was NOT changed in Round 7. `_userLists` (line 1770) is `$derived((lists || []).filter(l => !l.is_pinned).slice(0, 5))` — tabs appear only if user-created watchlists exist. Frontend agent: check if `lists` is loading correctly from `loadLists()` → `/api/watchlists`. If the API returns data, check if `l.is_pinned` filtering is accidentally excluding all watchlists. If code is fine, report to operator that the issue may be data/server-side.

---

## Group E — Button icon color (LogPanel)

**E1. lp-card-btn icon color** — in `frontend/src/lib/LogPanel.svelte`, `.lp-card-btn` (line 2330) has `color: rgba(148, 163, 184, 0.65)` (gray) but `border: 1px solid var(--algo-cyan-border)` (cyan). Icon should match border.
Fix:
- Change `color: rgba(148, 163, 184, 0.65)` → `color: var(--c-info)`
- Hover state (line 2346): keep or strengthen cyan: `border-color: rgba(34,211,238,0.65); color: var(--algo-cyan-text)`
- Remove the gray hover override (`rgba(148,163,184,...)` values)

This matches `GridSearchButton`/`GridDownloadButton` behavior (used in CardControls) which already have `color: var(--c-info)` on their icons.

---

## Group F — Grid format SSOT (3 more surfaces with `.algo-table` drift)

**Root cause**: Each surface hand-rolled its own CSS instead of matching `.algo-table` (0.72rem body, 0.6rem header, `rgba(126,151,184,0.10)` border, `rgba(13,22,42,0.30)` odd-row tint).

**F1. Agent templates notify/condition lists — `frontend/src/routes/(algo)/automation/agent-templates/+page.svelte`**
Structure is `.frag-list` flexbox (not a `<table>`). Specific issues:
- `font-size: var(--fs-lg)` = 0.7rem at line ~426 → change to `0.72rem`
- `border: 1px solid rgba(255,255,255,0.05)` → change to `rgba(126,151,184,0.10)`
- No alternating-row tint → add `row-tint-odd`/`row-tint-even` on list items (the new global classes from Group B), using JS `.map()` index or Svelte `{#each}` with index
- Hover: if present, change to `rgba(34,211,238,0.05)` to match `.algo-table`

**F2. LogPanel row CSS — `frontend/src/lib/LogPanel.svelte`**
The `.log-row` style (line ~1967) has drift:
- `font-size: var(--fs-lg)` = 0.7rem → change to `0.72rem`
- `border-bottom: 1px solid rgba(255,255,255,0.05)` (line ~1963) → change to `rgba(126,151,184,0.10)`
- Hover: `rgba(255,255,255,0.02)` (line ~1984) → change to `rgba(34,211,238,0.05)` matching `.algo-table`
(Alternating rows already implemented via `row-tint-odd`/`row-tint-even` after Group B fix)

**F3. Tokens table — `frontend/src/routes/(algo)/admin/tokens/+page.svelte`**
The `<table class="algo-table">` (line 393) is applied but Tailwind inline classes override it:
- Header `<tr>` (line ~395): `class="bg-[#0a1020] text-[var(--c-action)]"` → remove Tailwind, let `.algo-table thead th` handle it (already styled: background, color, font-size 0.6rem, border-bottom)
- Body `<tr>` (line ~407): `class="border-t border-white/5 hover:bg-white/5 cursor-pointer"` → remove `border-white/5` and `hover:bg-white/5`; `.algo-table tbody tr:hover td` handles hover; keep `cursor-pointer`
- No alternating rows because Tailwind border on `<tr>` overrides: once Tailwind removed, `.algo-table tbody tr:nth-child(odd) td` from app.css will apply automatically
- Cell text with `text-[0.55rem]`/`text-[0.6rem]` on specific columns can stay (semantic override for small label cells is intentional)

---

## Agents

- backend: skip
- frontend: Implement ALL of Groups A, B, C, D above in one pass.
- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Tests
- pytest: no
- svelte-check: yes
- playwright: no

## Commit message
feat(ui): grid SSOT, separator SSOT, row-tint global class, timestamp mobile, card 2x, btn icon color

## Done when
svelte-check 0 errors. Page header shows 1px separator after title label. LogPanel separator matches CardHeader. Agent and exp-close rows alternate using shared row-tint-odd/even classes. AlgoTimestamp smaller on mobile. Mobile toggle fixed or root cause reported. Agent cards span 2 columns. Dashboard activity defaults to news tab. LogPanel search/download icons are cyan to match button border.
