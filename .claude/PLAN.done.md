# Plan: Round 11 — z-index CSS variable scale + PnlAnalysis modal

## Context

Audit found 50+ hardcoded z-index literals across the frontend with no coordination. When
overlays collide (e.g. OrderTicket at 300 vs a future drawer at 200) there is no shared
reference. Adding a CSS variable scale gives a single source of truth for the stacking
hierarchy and makes collisions visible at a glance.

Secondary fix: PnlAnalysis.svelte has inline `.modal-backdrop` / `.modal` CSS at z-49/50
rather than delegating to ModalShell. Agent must read PnlAnalysis around lines 1280-1320
to confirm what the overlay is before deciding whether ModalShell is the right fix.

## Task

### Part A — CSS variable scale in app.css

Add to the `:root` block in `frontend/src/app.css`:

```css
/* z-index scale — use these vars; never hardcode global stacking values */
--z-nav:      50;    /* navbar, primary layout */
--z-dropdown: 60;    /* Select, MultiSelect, SymbolPanel dropdowns */
--z-toast:    80;    /* ToastContainer */
--z-drawer:   200;   /* OrderTimelineDrawer, slide-in panels */
--z-modal:    300;   /* OrderTicket overlay, full-screen modals */
--z-tooltip:  9999;  /* InfoHint, MarketPulse tooltips */
--z-search:   10000; /* SymbolSearchInput (must clear open modals) */
--z-command:  10500; /* CommandBar / order-modal overlay */
```

Then replace the matching literals in these files (mechanical substitution only — do NOT
change values that are local component internals like SVG layer stacking 1-6):

| File | Line(s) | Value → var |
|---|---|---|
| `frontend/src/app.css` | 1746 | `9999` → `var(--z-tooltip)` |
| `frontend/src/app.css` | 354 | `10500` → `var(--z-command)` |
| `lib/ToastContainer.svelte` | 33 | `80` → `var(--z-toast)` |
| `lib/Select.svelte` | 271 | `60` → `var(--z-dropdown)` |
| `lib/MultiSelect.svelte` | 231 | `60` → `var(--z-dropdown)` |
| `lib/InfoHint.svelte` | 283 | `9999` → `var(--z-tooltip)` |
| `lib/MarketPulse.svelte` | 4955 | `9999` → `var(--z-tooltip)` |
| `lib/SymbolSearchInput.svelte` | 296 | `10000` → `var(--z-search)` |
| `lib/order/OrderTicket.svelte` | 2437 | `300` → `var(--z-modal)` |
| `lib/order/OrderTimelineDrawer.svelte` | 207 | `200` → `var(--z-drawer)` |
| `routes/(algo)/+layout.svelte` | 1431 | `50` → `var(--z-nav)` |
| `routes/(algo)/+layout.svelte` | 2315, 2325 | `10600`, `10601` → `calc(var(--z-command) + 100)`, `calc(var(--z-command) + 101)` |
| `routes/(algo)/admin/+page.svelte` | 1373 | `200` → `var(--z-drawer)` |

Leave alone:
- All z-index values 1–10 (internal SVG/chart layer stacking — local, not global)
- `AgentToast` at 9997 (intentionally one below FullscreenButton at 9998) — leave as-is with its existing comment
- `NavigationIndicator` at 9200, `BrokerHealthBadge` at 9990/9991 — these are intentional mid-range values; leave as-is until a nav-overlay sub-scale is defined
- `RefreshButton` at 1000/1001 — leave (local dropdown, no collision risk)

### Part B — PnlAnalysis.svelte

Read `lib/PnlAnalysis.svelte` lines 1280–1330. Determine:
- If z-49 is a backdrop and z-50 is a modal panel → refactor to ModalShell
  (ModalShell props: `open`, `onClose`, `ariaLabel`, `zIndex`, `dim`, `children`)
- If z-49/50 are used for in-context stacking (tooltip, sticky header, side panel) → instead
  replace the literals with the appropriate var from the scale and leave the structure unchanged

Report which branch was taken.

## Agents

- frontend: Part A (mechanical var substitution, ~14 file edits) + Part B (read + assess + fix PnlAnalysis)
- backend: skip
- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Tests

- pytest: no
- svelte-check: yes
- playwright: no

## Commit message

refactor(ui): z-index CSS variable scale + PnlAnalysis stacking fix

## Done when

- 8 `--z-*` vars declared in `:root` in app.css
- All listed literals replaced with vars in the 14 files
- PnlAnalysis z-49/50 assessed and fixed (ModalShell or var substitution)
- svelte-check 0 errors
