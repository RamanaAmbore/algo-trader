# Plan: ats-slot gap + chart button icon + dashboard title rename

## Context
Three follow-up fixes from the previous batch:
1. `.ats-slot` on desktop has no `gap` — `ats-now`, `ats-sep`, `ats-refresh` render flush with zero spacing. The `|` is in the HTML but the items butt up against each other making it invisible. Fix: `gap: 0.3rem` on `.ats-slot`.
2. Chart button CSS in `SymbolPanel.svelte` was aligned (background/radius/hover) but the SVG icon was missed — different glyph (`polyline points="22 12 18 12 15 21 9 3 6 12 2 12"`, viewBox 24×24, stroke-width 1.6) vs the page-header icon (`path d="M2 13h12M3 11l3-4 3 2 4-6"`, viewBox 16×16, stroke-width 1.9). Fix: replace SVG in SymbolPanel with the page-header icon.
3. Dashboard chart title is "Intraday Performance" but operator wants "Performance".

## Agents
- frontend: Three edits:

  **Edit 1 — `frontend/src/lib/AlgoTimestamp.svelte`**
  Change `.ats-slot { display: inline-flex; }` → `.ats-slot { display: inline-flex; gap: 0.3rem; }`

  **Edit 2 — `frontend/src/lib/SymbolPanel.svelte`** (lines 1986–1989)
  Replace the SVG inside `.oes-chart-btn` with the page-header icon:
  ```html
  <svg width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden="true">
    <path d="M2 13h12M3 11l3-4 3 2 4-6" stroke="currentColor" stroke-width="1.9"
          stroke-linecap="round" stroke-linejoin="round"/>
  </svg>
  ```

  **Edit 3 — `frontend/src/routes/(algo)/dashboard/+page.svelte`**
  Change `title="Intraday Performance"` → `title="Performance"`

## Tests
- svelte-check: yes
- pytest: no

## Commit message
fix(ui): add ats-slot gap so separator renders; sync chart btn icon with page-header; rename dashboard chart title
