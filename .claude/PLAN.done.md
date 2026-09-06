# Plan: Frontend Audit — P1/P2 Fix Pass

## Context

Exhaustive frontend audit run across all three surface agents (data layer, routes, order surface).
The 6D audit fixes from the previous plan are now committed (08e27763) and pushed to dev.
This plan captures the remaining findings from the exhaustive audit for operator review and decision.

---

## Audit Findings Summary

### P1 — Blocking / Crash Risk

| Finding | File:Line | Description |
|---|---|---|
| Undefined CSS var `--algo-slate-dim` | `SymbolPanel.svelte:4844` | References undefined token; falls back to transparent — visual glitch |
| Undefined CSS var `--fs-base` | `OrderTicket.svelte:3031`, `LogPanel.svelte:2416` | Font-size token undefined; browser falls back to inherited font-size |
| Null crash `.toLocaleString()` | `admin/+page.svelte:781` | `user.contribution` can be null → uncaught TypeError |
| Null crash `.toFixed(4)` | `admin/+page.svelte:1354` | `e.nav_per_unit` can be null → uncaught TypeError |
| AbortError re-throw on internal timeout | `api.js:186-190` | Internal fetch AbortController timeout surfaces to UI as uncaught error |
| `$derived(() => {})` anti-pattern | `admin/+page.svelte` (multiple) | Should be `$derived.by(() => {})` in Svelte 5; current form may stale-cache |

### P2 — Memory Leaks / Data Accuracy

| Finding | File | Description |
|---|---|---|
| Holdings day P&L uses stale `previous_close` | `dashboard/+page.svelte` | Reads broker `previous_close` (BHAV lag) instead of `daily_book.ltp` — same bug as fixed in positions |
| No role guard on perf page | `perf/+page.svelte` | Page renders for non-admin users; should redirect or hide content |
| `_loadOrdersTimer` not cleared in onDestroy | `orders/+page.svelte` | Interval leaks after component unmounts |
| `_marginTimer` never cancelled | `CommandLineTab.svelte` | setInterval leaks on unmount |
| `setQuoteLoadedCallback` no unregister | `SymbolPanel.svelte` | Callback registered but never cleared — stale callback can fire after unmount |
| `_symbolDebounce` + `_stickyResultTimer` | `SymbolPanel.svelte` | clearTimeout not called in onDestroy — minor leak |

### P3 — Dead Code / Off-Palette (informational, no blocking risk)

- Large dead W/L grid code block in `dashboard/+page.svelte`
- Dead context-menu code in `orders/+page.svelte`
- Multiple off-palette hardcoded colors in `dashboard/+page.svelte` and `perf/+page.svelte`
  (e.g. `#1e293b`, `#334155`, `#94a3b8`, `#f1f5f9`)

---

## Recommended Scope for This Fix Pass

Fix only the P1 crash risks + the two most impactful P2 items. Leave P3 dead-code for a
dedicated cleanup plan to avoid scope creep.

**In scope:**
1. `--algo-slate-dim` → define in `app.css` as alias of an existing token (e.g. `rgba(148,163,184,0.4)`)
2. `--fs-base` → define in `app.css` as `0.875rem` (14px, matches current design density)
3. `admin/+page.svelte:781` → guard with `user.contribution?.toLocaleString() ?? '—'`
4. `admin/+page.svelte:1354` → guard with `e.nav_per_unit?.toFixed(4) ?? '—'`
5. `api.js:186-190` → catch AbortError from internal timeout separately; don't re-throw as user-visible error
6. `$derived(() => {})` → change to `$derived.by(() => {})` in affected lines
7. `_loadOrdersTimer` and `_marginTimer` → add clearInterval in onDestroy
8. `setQuoteLoadedCallback` unregister → add cleanup in SymbolPanel onDestroy

**Out of scope (leave for later):**
- Holdings day P&L `previous_close` fix (backend change, separate plan)
- Perf page role guard (non-breaking, separate plan)
- P3 dead code + palette cleanup (separate cleanup plan)

---

## Agents

- frontend: Fix P1 CSS tokens (app.css), admin null guards, api.js AbortError, $derived.by fixes
- frontend: Fix P2 timer leaks — _loadOrdersTimer (orders page), _marginTimer (CommandLineTab), SymbolPanel callback cleanup
- backend-test: skip

## Tests
- pytest: no
- svelte-check: yes
- playwright: no

## Commit message
fix(frontend): P1 null guards + undefined CSS tokens + AbortError isolation + P2 timer cleanup

## Done when
svelte-check 0 errors, vitest green, no new null-crash paths in admin page
