# Plan: Round 12 — demo sandbox fix + CSS SSOT sweep + grid/layout polish

## Context

Four concerns combined into one round:
1. **Demo sandbox redirect bug** — `/admin/execution` (Sandbox / Lab) redirects anonymous demo visitors to `/signin` instead of showing an empty screen with a "not available" notice. The guard at line 57 fires because `$authStore.user` is null for all demo sessions, regardless of role.
2. **18 weak borders** still using `rgba(255,255,255,0.05)` instead of canonical `rgba(126,151,184,0.10)`.
3. **20 hardcoded `#0a1020`** values that should use `var(--algo-bg-elev1)`.
4. **Grid/layout polish**: agent-templates notify/condition rows inconsistent with algo-table standard; tokens page grids have font sizes too small; automation page agent status cards should fill available width with 2 per row on desktop.

## Task

### Agent A — Demo sandbox fix (`admin/execution/+page.svelte`)

**File:** `frontend/src/routes/(algo)/admin/execution/+page.svelte`

Current guard (line 57):
```js
if (!$authStore.user || (r !== 'admin' && r !== 'designated')) { goto('/signin'); return; }
```

**Fix:**
1. Import `getContext` from `'svelte'` and read `algoStatus` from layout context (same pattern as other pages: `const algoStatus = getContext('algoStatus')`).
2. Derive `const isDemo = $derived(algoStatus.isDemo)`.
3. Replace the single guard with two separate checks in `onMount`:
   - If `isDemo`: set a `_demoBlocked = true` state flag and return (no redirect, no panel load).
   - Else if `!$authStore.user || (r !== 'admin' && r !== 'designated')`: redirect to `/signin` as before.
4. In the template, before rendering tabs/panels, add:
   ```svelte
   {#if _demoBlocked}
     <div class="lab-demo-notice">
       <span>Sandbox is not available in demo mode.</span>
     </div>
   {:else}
     <!-- existing tabs + panel content -->
   {/if}
   ```
5. Style `.lab-demo-notice` to match the existing demo restriction strip pattern: amber/muted colour, small padding, centered text, no border — reference the demo banner in `+layout.svelte` or the "Demo mode — feature unavailable." style from `api.js`. Keep it minimal.

The page header (title + timestamp + refresh button) should still render above the notice — only the tab area and panel content are replaced by the notice strip. This gives the operator an empty but coherent screen rather than a signin redirect.

---

### Agent B — CSS SSOT sweep + grid/layout polish (parallel with Agent A)

**B1 — Weak border cleanup (18 instances → `rgba(126,151,184,0.10)`)**

Replace every `rgba(255,255,255,0.05)` / `border-white/5` / `rgba(255, 255, 255, 0.05)` with `rgba(126,151,184,0.10)` in:
- `frontend/src/app.css` (~line 1258)
- `lib/MultiPriceChart.svelte` (~line 371)
- `lib/ShortcutCheatsheet.svelte` (~lines 180, 208)
- `lib/SymbolSearchInput.svelte` (~lines 317, 325)
- `lib/order/ChaseCard.svelte` (~line 416)
- `lib/order/OptionChainTab.svelte` (~lines 1184, 1414)
- `lib/execution/RecordingsPanel.svelte` (~lines 268, 322)
- `lib/execution/SimulatorPanel.svelte` (~lines 1912, 2084)
- `routes/(algo)/+layout.svelte` (~line 2353)
- `routes/(algo)/admin/derivatives/+page.svelte` (~lines 6263, 6322)
- `routes/(algo)/automation/+page.svelte` (~line 1628)
- `routes/(public)/+layout.svelte` (~line 536, value 0.055 → use 0.10)

**B2 — Color token: `#0a1020` → `var(--algo-bg-elev1)` (20 instances)**

Replace bare `#0a1020` literals (NOT the `:root` definition line in app.css) with `var(--algo-bg-elev1)`. For gradient contexts, replace `rgba(10,16,32,x)` with appropriate form. Files:
- `lib/ConfirmModal.svelte` — `rgba(10,16,32,0.6)` → `rgba(var(--algo-bg-elev1-rgb, 10,16,32),0.6)` OR leave as-is if no RGB var exists (just replace the solid `#0a1020` hits)
- `lib/PnlPanel.svelte` — `background: #0a1020`
- `lib/NavTab.svelte` — icon stroke `#0a1020`
- `lib/PositionStrip.svelte` — 3x gradient backgrounds
- `lib/PnlAnalysis.svelte` — gradient
- `lib/order/OrderTimelineDrawer.svelte` — `background: #0a1020`
- `lib/execution/SimulatorPanel.svelte` — gradient
- `routes/(algo)/+layout.svelte` — 3x
- `routes/(algo)/strategies/[id]/+page.svelte` — icon stroke
- `routes/(algo)/admin/alerts/+page.svelte` — background
- `routes/(algo)/admin/tokens/+page.svelte` — Tailwind `bg-[#0a1020]` → `bg-[var(--algo-bg-elev1)]`
- `routes/(algo)/automation/+page.svelte` — gradient
- `routes/(algo)/automation/agent-templates/+page.svelte` — gradient

**Important:** Only replace `#0a1020` in CSS/style rules, not the `:root` var definition in `app.css` (that IS the source). Check that `--algo-bg-elev1` is defined as `#0a1020` in app.css `:root` before doing the sweep.

**B3 — Agent-templates notify/condition grid formatting**

File: `routes/(algo)/automation/agent-templates/+page.svelte`

The `.frag-list` / `.frag-row` / `.frag-head` structure renders notify and condition fragments as an accordion list. Issues:
- Font size/weight inconsistent with the rest of the page
- Border between rows not using the canonical `rgba(126,151,184,0.10)` (may be missing entirely)
- Alignment of columns within frag-row may differ from algo-table cell padding

Fix (read the file first to confirm):
- Add `border-bottom: 1px solid rgba(126,151,184,0.10)` to `.frag-row` if missing
- Ensure `.frag-head` font-size is `var(--fs-md)` (not smaller) and `font-weight: 500`
- Ensure `.frag-desc` / `.frag-name` padding matches `4px` cell padding from algo-table
- Do NOT restructure the accordion into a table — just tighten the existing spacing/border/font to match visual standard

**B4 — Tokens page grid font size**

File: `routes/(algo)/admin/tokens/+page.svelte`

The tokens table uses `.algo-table` which inherits `thead th { font-size: 0.6rem }` from app.css — that's too small. Options:
1. If the tokens page has its own scoped thead rule, bump it to `var(--fs-sm)` or `0.68rem`
2. If it's purely inheriting from `.algo-table` in app.css, check whether bumping `algo-table thead th` globally would break other tables, and if not, change it there

Read the tokens page and app.css algo-table rules. Apply the minimal fix that improves readability without breaking other algo-table instances. Target: thead `0.68rem`, body already `0.72rem` (fine).

**B5 — Automation page agent status card layout (2 per row on desktop)**

File: `routes/(algo)/automation/+page.svelte`

Currently: `.page-grid.agent-group-grid` with `auto-fill minmax(18rem, 1fr)`. Agent cards use `algo-status-card-2x` (span 2). On wide desktops (5+ auto-fill columns) the span-2 cards may not cleanly divide into 2 per row.

Fix: Add a scoped override on `.agent-group-grid` (already defined locally):
```css
.agent-group-grid {
  grid-template-columns: repeat(2, 1fr);  /* exactly 2 per row on desktop */
}
@media (max-width: 768px) {
  .agent-group-grid { grid-template-columns: 1fr; }
}
```
Remove `algo-status-card-2x` from agent card divs (they no longer need span-2 if the grid is always 2-column). Order status cards stay as-is — they will also be 1fr (half width) which is fine since they're narrower by design.

Read the file first to confirm the current structure before making changes.

---

## Agents

- frontend-A: Agent A task only (execution/+page.svelte demo fix)
- frontend-B: Agent B tasks B1–B5 (CSS sweep + grid polish)
- Dispatch both in parallel — they touch no overlapping files
- backend: skip
- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Tests

- pytest: no
- svelte-check: yes
- playwright: no

### Agent B6 — Signals button right border fix (`ChartWorkspace.svelte`)

**File:** `frontend/src/lib/ChartWorkspace.svelte`

**Bug:** The Signals button has both `cw-range-btn` and `cw-signals-btn` classes. `.cw-range-btn:last-child { border-right: 0; }` has specificity (0,2,0) which beats `.cw-signals-btn { border: 1px solid var(--algo-cyan-border); }` at (0,1,0), so the right border is stripped when the Signals button is the last sibling.

**Fix** — change one line (~line 2498):
```css
/* before */
.cw-range-btn:last-child { border-right: 0; }

/* after */
.cw-range-btn:last-child:not(.cw-signals-btn) { border-right: 0; }
```

Also check the responsive version (~line 3036) for the same pattern and apply the same `:not(.cw-signals-btn)` exclusion there if present.

---

## Commit message

fix(demo): sandbox shows unavailable notice instead of signin redirect; refactor(ui): weak border + color token sweep + grid/layout polish

## Done when

- Demo visitors on `/admin/execution` see empty page + notice strip (no `/signin` redirect)
- 0 remaining `rgba(255,255,255,0.05)` borders in frontend
- 0 remaining bare `#0a1020` literals outside `:root` definition
- Agent-templates rows have canonical border + readable font
- Tokens thead font size ≥ 0.68rem
- Automation page: exactly 2 agent status cards per row on desktop
- svelte-check 0 errors
