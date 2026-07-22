# Plan: Migrate 15+ tables to .algo-table global class

## Task
A parallel agent is adding `.algo-table`, `.algo-table-num`, and `.algo-table-wrap` to `app.css`.
This pass migrates every hand-rolled `<table>` element across 15 frontend files to add the
`algo-table` class and strips out the per-file CSS that duplicates what `.algo-table` will
provide globally (font-size, font-family, base color, alternating rows, generic hover).
Semantic overrides (amber chips, status colors, pending rows, sticky headers) are preserved.

## Agents
- frontend: Migrate all 15 files as specified below. All edits are frontend-only.
- backend: skip
- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Per-file migration details

### 1. admin/tokens/+page.svelte
- `<table class="w-full text-[0.65rem]">` → `<table class="algo-table">`
- Remove `text-[0.65rem]` from table element; remove `py-1.5 px-2` from all `<th>` and `<td>` cells
- No style block table rules to remove
- Keep: amber `font-mono text-[var(--c-action)]` on token-value td (inline Tailwind — keep)

### 2. admin/brokers/+page.svelte
- `<table class="brokers-table">` → `<table class="brokers-table algo-table">`
- `<table class="conn-table">` → `<table class="conn-table algo-table">`
- Remove from `.brokers-table` CSS: `font-family: monospace`, `font-size: var(--fs-sm)`, `.brokers-table td { color: var(--algo-slate); border-bottom: ... }`
- Remove from `.conn-table` CSS: same font/color/border, plus `tr:nth-child(odd) td { background: rgba(13,22,42,0.20) }` (algo-table handles alternating rows)
- KEEP: `.brokers-table th` (amber border-bottom + sticky — semantic), `.conn-table th` (same), `.status-pill` variants, `.brokers-hist-pill`, `.priority-chip`, `.conn-ev-*` colors, `.brokers-table .destructive :global()`, column-width rules, `tr.row-inactive td { opacity: 0.5 }`, `.conn-td-time`, `.conn-td-detail`

### 3. admin/audit/+page.svelte
- `<table class="audit-table">` → `<table class="audit-table algo-table">`
- `<div class="audit-table-wrap algo-grid-chrome">` — rename class to `<div class="algo-table-wrap algo-grid-chrome">`
- Remove from `.audit-table` CSS: `font-family`, `font-variant-numeric`, `font-size: var(--fs-md)`, `td { color: #c8d8f0 }`, `td { border-bottom }`, `tbody tr:nth-child(even) { background: rgba(34,47,75,0.30) }`, generic hover rule
- KEEP: `.audit-table th` sticky + amber border-bottom, `.audit-ts`, `.audit-actor`, role/cat/status chips, `.audit-th-narrow`, `.audit-req-id`, `.audit-ip`
- Update CSS: rename `.audit-table-wrap` → `.algo-table-wrap`

### 4. admin/statements/+page.svelte
- `<table class="ms-table content-fade-in">` → `<table class="ms-table algo-table content-fade-in">`
- `<section class="ms-table-wrap">` → `<section class="algo-table-wrap">`
- `.td-num` → `algo-table-num` in both markup (all `<th class="td-num">` and `<td class="td-num">`) and CSS
- Remove from `.ms-table` CSS: `font-size: var(--fs-lg)`, `color: #c8d8f0`, `td { border-bottom }`, generic hover
- KEEP: `.row-pending` amber bg, `.row-failed` red bg, `.ms-lp-name`, `.ms-lp-sub`, `.ms-error-cell`, `.ms-pill-status` chips, header amber border, `.td-mono`, `.td-actions`
- CSS: rename `.ms-table-wrap` → `.algo-table-wrap`, rename `.td-num` → `.algo-table-num`

### 5. admin/history/+page.svelte
- All three `<table class="hist-table">` → `<table class="hist-table algo-table">`
- `<div class="hist-table-wrap">` → `<div class="algo-table-wrap">`
- `.td-num` → `algo-table-num` in both markup and CSS
- Remove from `.hist-table` CSS: `font-size: var(--fs-md)`, `color: #c8d8f0`, `td { border-bottom }`, generic hover
- KEEP: `.hist-side-buy/sell` chips, `.cell-pos/neg`, `.hist-audit-link`, `.td-mono`, amber `th border-bottom`, `th background`

### 6. admin/metrics/+page.svelte
- `<table class="metrics-table">` → `<table class="metrics-table algo-table">`
- `<div class="metrics-table-wrap">` → `<div class="algo-table-wrap">`
- `.num` → `algo-table-num` in both markup (all `<th class="num">` and `<td class="num">`) and CSS
- Remove from `.metrics-table` CSS: `font-size: var(--fs-xl)`, `font-variant-numeric: tabular-nums`, generic `border-bottom`, generic `td`/`th` color rules
- KEEP: `.metrics-tag code`, `.metrics-sha`, `.metrics-ts`, `.metrics-drill`, `.metric-label`
- CSS: rename `.metrics-table-wrap` → `.algo-table-wrap`, rename `.num` → `.algo-table-num`

### 7. admin/settings/+page.svelte
- `<table class="text-[0.65rem] w-full">` → `<table class="algo-table w-full">`
- Remove `text-[0.65rem]` from table element
- No style block table rules

### 8. admin/alerts/+page.svelte
- `<table class="alerts-table">` → `<table class="alerts-table algo-table">`
- `.alerts-table-wrap` has border/box-shadow/border-radius — keep the wrapper class name (do NOT rename, it has semantic chrome beyond just overflow)
- Remove from `.alerts-table` CSS: `font-size: var(--fs-md)`, `font-family`, `tbody tr:nth-child(odd)` alternating rule, generic hover
- KEEP: amber `th border-bottom`, `th color: var(--c-action)`, `tr.row-sim` special bg, `td border-right`, `background: linear-gradient(...)` on the table itself (branded bg — keep), event chip classes, `.td-cond`, `.td-detail`, `.td-channels`, `.td-time`, `.td-agent`

### 9. admin/research/+page.svelte
- `<table class="drafts-table">` → `<table class="drafts-table algo-table">`
- `<table class="audit-table">` → `<table class="research-table algo-table">` (rename local class to avoid name collision with audit page)
- `<table class="tools-table">` → `<table class="tools-table algo-table">`
- Remove from `.drafts-table` CSS: `font-size: var(--fs-lg)`, generic th/td color/border
- Remove from `.audit-table` CSS: same; rename class throughout to `.research-table`
- Remove from `.tools-table` CSS: `font-size: var(--fs-lg)`, generic th/td color/border
- `.mint-grid` is a CSS grid (display: grid), NOT a table — add `font-size: 0.72rem`, `color: var(--algo-slate)`, `border: 1px solid rgba(126,151,184,0.10)` to the `.mint-grid` CSS rule in-place

### 10. admin/perf/+page.svelte
- `<table class="perf-hotspot-table">` → `<table class="perf-hotspot-table algo-table">`
- `.num` → `algo-table-num` in both markup and CSS
- Remove from `.perf-hotspot-table` CSS: `font-size: var(--fs-xl)`, `font-variant-numeric: tabular-nums`, generic `border-bottom`, `th` color
- KEEP: `.perf-fn-name`, `.perf-fn-page`, `.perf-fn-cc`, `.perf-fn-line`, `.metric-label`

### 11. admin/simulator/iterations/+page.svelte
- `<table class="iter-table">` → `<table class="iter-table algo-table">`
- Remove from `.iter-table` CSS: `font-size: var(--fs-sm)`, `font-family: var(--font-numeric)`
- Keep `.numeric` class as-is (semantic name, covers both th and td; algo-table's global `.algo-table-num` would be separate)
- KEEP: `.er-ok/warn/err/pending/other` chips, `.slug` amber, `.iter-row:hover` (amber — semantic), amber th border-bottom

### 12. admin/+page.svelte (Users page — investor portal modal)
- Two `<table class="ip-modal-tbl">` → `<table class="ip-modal-tbl algo-table">`
- Remove from `.ip-modal-tbl` CSS: `font-size: var(--fs-md)` (table-level rule), `td { color: #c8d8f0; border-bottom: ... }` generic td rules
- KEEP: `.ip-modal-tbl-wrap` (has semantic border + border-radius — do NOT rename), `.ip-modal-tbl th` (all semantic — background, color, amber border-bottom, text-transform), `.ip-modal-tbl td.td-mono`, `.ip-modal-tbl td.td-num`, `.ip-modal-tbl td.td-actions`, `.ip-modal-tbl tr.revoked td`, `.ip-modal-note`

### 13. strategies/+page.svelte
- `<table class="strat-table">` → `<table class="strat-table algo-table">`
- `.strat-table-wrap` has overflow-x + border + box-shadow + gradient bg chrome — keep class name but add `algo-table-wrap` alongside OR just keep existing name (wrapper has too much chrome to be a plain algo-table-wrap)
  - Decision: keep `.strat-table-wrap` as-is; it's not a plain overflow wrapper
- `.td-num` → `algo-table-num` in both markup and CSS
- Remove from `.strat-table` CSS: `font-size: var(--fs-lg)`, `color: #c8d8f0`, `td { border-bottom }`, generic hover
- KEEP: amber `th border-bottom`, `background` on header, `.strat-slug`, `.pnl-pos/neg`, `.strat-row-inactive`, `.strat-row-editing`, `.pill-active/inactive`, `.td-slug`, `td.td-num` alignment/tabular-nums (now under `algo-table-num`)

### 14. strategies/[id]/+page.svelte
- `<table class="strat-table">` → `<table class="strat-table algo-table">`
- Same wrapper decision: keep `.strat-table-wrap algo-grid-chrome` as-is
- `.td-num` → `algo-table-num` in both markup and CSS
- KEEP: `side-long/side-short` chips, `.pnl-pos/neg`, `.strat-row-closed`, `.qty-rem`, `.td-mono`

### 15. lib/NavBreakdown.svelte
- `<table class="nav-bd-table">` → `<table class="nav-bd-table algo-table">`
- Remove from `.nav-bd-table` CSS:
  - `font-size: 0.72rem` (exact match — algo-table provides this)
  - `color: var(--algo-slate)` (exact match)
  - `tbody tr:nth-child(odd) td { background: rgba(13,22,42,0.30) }` (algo-table provides)
  - `tbody tr:nth-child(even) td { background: #1d2a44 }` (remove — algo-table's odd rule is enough)
  - Generic hover rule (algo-table provides)
- KEEP: `.nav-bd-total` TOTAL row amber styling, `nav-bd-acct`, `nav-num`, `nav-bd-nav`, `nav-bd-caption`, empty/error/warn/hint state divs, `th` styling (height, amber border-bottom), `tbody td` height/padding/border specifics

### 16. admin/health/+page.svelte (SKIP — no table, and .health-grid has no font-size/color/border to update)
- `.health-grid` CSS only has `display: grid`, `grid-template-columns`, `gap` — nothing to change
- No `<table>` elements present in this file

## Tests
- pytest: no
- svelte-check: yes
- playwright: no

## Commit message
refactor(frontend): migrate 15 tables to global .algo-table class; strip duplicate font/color/border CSS

## Done when
- All 15 tables have `algo-table` in their class list
- Per-file CSS that duplicates `.algo-table` globals (font-size, font-family, base color, alternating rows, generic hover) is removed
- Semantic overrides (amber chips, status colors, sticky headers, pending rows) remain intact
- `svelte-check` passes with no type errors
- Visual spot-check: tables render with correct monospace font, alternating rows, hover highlight
