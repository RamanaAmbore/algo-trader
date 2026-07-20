# Plan: Round 10 — SSOT row-tint variable + page-grid adoption

## Context

Reaudit after Round 8+9 found two remaining SSOT gaps:
1. `PnlPanel.svelte` and `BrokerHealthBadge.svelte` still use `:nth-child(odd)` with hardcoded `rgba(13,22,42,0.30)` instead of the global row-tint token.
2. Only 1 of 10 route pages uses `page-grid`; dashboard and pulse have multi-card layouts but define their own ad-hoc grid CSS.
3. `derivatives/+page.svelte:5689` has a lone hardcoded `rgba(13,22,42,0.30)` outside ag-Grid.

## Task

Two-part cleanup:

**Part A — CSS variable for row tint colour (3 files, zero risk)**
Add `--row-tint-odd-bg: rgba(13,22,42,0.30)` to `:root` in `app.css` (alongside the existing `.row-tint-odd` class rule). Then replace the hardcoded literals:
- `frontend/src/lib/PnlPanel.svelte:324` — `.pnl-table tbody tr:nth-child(odd) { background: rgba(13,22,42,0.45); }` → change value to `var(--row-tint-odd-bg)` (the 0.45 alpha was incorrect—match the standard 0.30)
- `frontend/src/lib/BrokerHealthBadge.svelte:300` — `.bh-row:nth-child(odd)` → `var(--row-tint-odd-bg)`
- `frontend/src/routes/(algo)/admin/derivatives/+page.svelte:5689` — inline `background-color: rgba(13,22,42,0.30)` → `var(--row-tint-odd-bg)`

Leave `SimulatorPanel.svelte:1797` (uses 0.55 alpha — different semantic, likely hover/selected).  
Leave `derivatives:5480` ag-Grid fallback — already correct pattern.

**Part B — `page-grid` adoption for dashboard and pulse (2 files)**
Read `dashboard/+page.svelte` and `pulse/+page.svelte` to find their outermost card-grid containers. Replace ad-hoc grid CSS with the `page-grid` class where the layout matches (auto-fill, minmax card columns). Do NOT restructure the card internals — only swap the outer container class if the existing CSS is equivalent to `page-grid`. If a page uses a fundamentally different layout (e.g. single-column, full-bleed), skip it.

## Agents

- frontend: Part A (3 files) + Part B (dashboard + pulse inspection and class swap). Verify svelte-check passes.
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

refactor(ui): row-tint CSS var + page-grid adoption for dashboard/pulse

## Done when

- `rgba(13,22,42,0.30)` literal gone from PnlPanel, BrokerHealthBadge, derivatives (lone instance)
- `--row-tint-odd-bg` declared in `:root` in app.css
- `page-grid` adopted on dashboard + pulse where layout is equivalent
- svelte-check 0 errors
