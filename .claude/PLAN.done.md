# Plan: Log Panel Heights + Expand/Contract/Fullscreen Button Rework

## Context
The log panel currently has a uniform tall-by-default behaviour (`_actTall=true`) across all pages, and the expand/contract button grows the panel to ~85vh. The operator wants per-page default heights, a collapse-first expand/contract button, and a new fullscreen button that opens the ActivityLogModal (the same as the activity bell in the page header). The sandbox (SimulatorPanel) log panel is to be removed entirely.

## Task
1. **Per-page default heights** — set CSS-controlled height on dashboard (33vh), orders (25vh), automation (50vh, already correct).
2. **Expand/Contract button** — invert semantics: at default height the button shows "contract" (arrows inward); pressing it collapses the body (hides `.lp-body-wrap`). When body is hidden the button shows "expand" (arrows outward); pressing re-shows the body. Remove the `CollapseButton` from the canonical group (it is now redundant).
3. **Fullscreen button** — add as the LAST button in the canonical group; hidden when `context === 'modal'`; calls `openActivityModal()` (already imported in LogPanel).
4. **Remove `isTall` from the button loop** — prop stays for legacy compat but default changes to `false`. Remove all `_actTall` page-level state and `bind:isTall` props.
5. **Outer card-body `hidden` fix** — remove `hidden={_colActivity}` from the outer `.card-body` wrappers on dashboard and orders so collapsing only hides `.lp-body-wrap` (the log rows), leaving the tab strip + buttons visible.
6. **Sandbox removal** — remove `ActivityLogSurface` from `SimulatorPanel.svelte` plus `logTab` state and any `$effect` that calls `loadSimLog`/`loadSystemLog` via `logTab` (grep first to confirm no other callers before removing those helpers).

## Agents
- frontend: all six changes above
- backend: skip
- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Critical Files

| File | Change |
|---|---|
| `frontend/src/lib/LogPanel.svelte` | Rework canonical button group (lines 1445–1495); change `isTall` default to `false` (line 160) |
| `frontend/src/lib/ActivityLogSurface.svelte` | Change `isTall = $bindable(true)` → `$bindable(false)` (line 68); keep prop for compat |
| `frontend/src/routes/(algo)/dashboard/+page.svelte` | Remove `_actTall` state + `bind:isTall`; remove `class:act-tall` + `hidden={_colActivity}` from `.card-body`; CSS: `max-height: 15rem` → `33vh`, remove `.act-tall` rule |
| `frontend/src/routes/(algo)/orders/+page.svelte` | Same `_actTall` cleanup; CSS: `height: clamp(18rem, 40vh, 500px)` → `clamp(6rem, 25vh, 280px)`, remove `.oc-act-tall` rule; remove `hidden={_colActivity}` from `.oc-act-body` |
| `frontend/src/lib/execution/SimulatorPanel.svelte` | Remove `ActivityLogSurface` block + `logTab` state + logTab-driven `$effect` |

## Detailed Button Group Change (LogPanel.svelte canonical branch)

**Before** (lines 1476–1495):
```
CollapseButton  →  Expand/Contract (toggles isTall)
```

**After**:
```
[Expand/Contract — toggles isCollapsed]  [Fullscreen — calls openActivityModal()]
```

Button logic:
- `isCollapsed=false` (content visible, default) → show arrows-inward SVG, title "Contract panel"
- `isCollapsed=true` (body hidden) → show arrows-outward SVG, title "Expand panel"
- No `lp-card-btn-on` active class needed (not a persistent mode)
- Fullscreen: `{#if context !== 'modal'} ... openActivityModal() ... {/if}` — use the same 4-corner-bracket SVG from the legacy branch (LogPanel.svelte line 1551–1553)

## Height Summary

| Page | Current default | New default | How |
|---|---|---|---|
| Dashboard | `max-height: 15rem` (non-tall) | `max-height: 33vh` | CSS on `.dash-activity > .card-body` |
| Orders | `clamp(18rem, 40vh, 500px)` | `clamp(6rem, 25vh, 280px)` | CSS on `.oc-act-body` |
| Automation | `h-[50vh]` | `h-[50vh]` — no change | `heightClass` prop already set |
| Sandbox | `h-[40vh]` | removed | Remove entire ActivityLogSurface |

## Tests
- pytest: no
- svelte-check: yes
- playwright: no

## Commit message
refactor(log-panel): per-page heights, collapse-first button, fullscreen opens modal, remove from sandbox

## Done when
- Dashboard log panel renders at ≤33vh; expand/contract button collapses body leaving tab strip visible; fullscreen button opens ActivityLogModal
- Orders log panel renders at ≤25vh; same button behaviour
- Automation unchanged at 50vh
- Sandbox (execution page) has no log panel
- svelte-check passes with zero errors
