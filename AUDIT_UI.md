## UI / Palette / Density Audit — 2026-06-01

### Palette violations

- [`frontend/src/lib/AgentToast.svelte:163`] `tier-high` = `#fbbf24` (amber) but [`agents/+page.svelte:1539`] `tier-pill-high` = `#fb923c` (orange). The two surfaces that describe the same "high" tier use different colours — AgentFireModal and AgentToast both use amber; agents/+page uses orange. One of these is wrong; the agents page diverged.

- [`frontend/src/lib/PnlPanel.svelte:334–335`] introduces `pnl-pos` / `pnl-neg` — a third P&L colouring family alongside the documented `pnl-gain/pnl-loss/pnl-zero` (PerformancePage) and `cell-pos/cell-neg/cell-flat` (MarketPulse). Same hex values (`#4ade80` / `#f87171`) but a distinct class name living in a separate scoped `<style>`. CLAUDE.md notes only two families as "known debt"; PnlPanel adds a third.

- [`frontend/src/routes/(algo)/admin/options/+page.svelte:3408–3414`] `cand-pnl-pos` / `cand-pnl-neg` — a fourth P&L variant (`#4ade80` / `#f87171` again). Same colours, different class names. The documented debt is now four families, not two.

- [`frontend/src/routes/(algo)/+layout.svelte:1663`] `.mode-combo-error { background: #1a0a0a }` — a near-black-red not present in any documented palette token. The canonical error surface on dark backgrounds is `rgba(248,113,113,0.12)` with `#f87171` text (as used throughout). This one-off creates a visually inconsistent error chip in the mode dropdown.

- [`frontend/src/routes/(algo)/orders/+page.svelte:925`] `.ul-card-time` override sets `color: #fde047` (Tailwind yellow-300). The documented secondary text accent in sim/algo is `#fde68a` (amber-200). `#fde047` has higher saturation and visually pops against navy backgrounds differently than the rest of the secondary-label palette — unintentional divergence.

- [`frontend/src/lib/PerformancePage.svelte:1295`] `.perf-dark .perf-ts { color: #fde047 !important }` — same `#fde047` anomaly on the dark (algo dashboard) rendering of PerformancePage. Timestamps on every other algo surface use `#c8d8f0` or `#fde68a`; this breaks the timestamp colour identity on the dashboard's embedded grid.

### Density / layout violations

- [`frontend/src/routes/(algo)/admin/+page.svelte:331–343`] **Users page missing RefreshButton.** The page has `load()` called from `onMount` and a user-visible "Loading users…" spinner, but no `<RefreshButton>` in the page header. CLAUDE.md mandates a RefreshButton on every page that fetches data dynamically. The `+ Create User` action chip is present; RefreshButton is absent.

- [`frontend/src/routes/(algo)/admin/settings/+page.svelte:124–133`] **Settings page missing RefreshButton.** `load()` is called on mount (`settings` array fetched from API), but the page header only has `<PageHeaderActions />`. CLAUDE.md lists `admin/settings` as a "form-only" exception — however this page actively fetches a settings list from the API on mount, so the exception may not apply.

- [`frontend/src/routes/(algo)/agents/activity/+page.svelte:29–38`] **Agent Activity page missing RefreshButton.** Page delegates all loading to `<UnifiedLog>` which self-polls at 3 s. But CLAUDE.md's rule is unconditional: "every page that fetches data dynamically MUST have a page-header RefreshButton." The RefreshButton also drives the `lastRefreshAt` tooltip; without it operators have no manual refresh affordance.

- [`frontend/src/routes/(algo)/admin/options/+page.svelte:2123,2204`] **RefreshButton placed inside card header in non-fullscreen mode.** At lines 2123 (Payoff card) and 2204 (Legs card) the `{#if _fsPayoff}` / `{#if _fsLegs}` guards are correct — RefreshButton only renders in fullscreen. But the page header's RefreshButton at line 1909 wraps all three loaders (`loadPositions + loadSimStatus + loadStrategy`). That's fine for the page header. The card-level placements are canonical per CLAUDE.md, no violation — flagged as double-check only.

### Component drift / one-off implementations

- [`frontend/src/lib/OrderDetail.svelte:47`] uses `window.confirm()` for cancel confirmation. The project has `<ConfirmModal>` specifically replacing `window.confirm()` (it fails silently on iOS PWA). Same issue at [`execution/ReplayPanel.svelte:166`] (`confirm('Delete all replay orders and events?')`), [`admin/brokers/+page.svelte:267`], [`admin/tokens/+page.svelte:165`], [`agents/fragments/+page.svelte:143`], and [`execution/SimulatorPanel.svelte:1026`]. Six callsites still on `window.confirm()` / bare `confirm()`.

- [`frontend/src/routes/(algo)/admin/settings/+page.svelte:274–309`] Hand-rolled `.info-btn` + `.info-popout` duplicates the canonical `<InfoHint popup>` component exactly (same amber `(i)` chip, same flat slate-blue popout, same border colours). CLAUDE.md documents that InfoHint was "implemented across `/admin/brokers`, `/admin/options`, `/admin/execution`, `/admin/settings`" — but the settings page didn't actually adopt the component; it re-implemented it inline per-row.

- [`frontend/src/routes/(algo)/+layout.svelte:721–736`] and [`agents/+page.svelte:1688–1707`] each contain a bespoke live-mode confirmation modal (`live-confirm-modal` / `lc-modal`) with their own overlay/body/button markup. `<ConfirmModal>` (which already handles this exact pattern) is available and used elsewhere. Two separate one-off modal implementations for the same "confirm destructive/live action" UX.

- [`frontend/src/routes/(algo)/admin/+page.svelte:642`] email-history toggle uses raw Unicode triangles `▴ / ▾` in button text rather than `<DisclosureChevron>`. CLAUDE.md documents `DisclosureChevron` as the canonical row-level inline disclosure. The admin page already imports `DisclosureChevron` (line 16) and uses it for the user-row expansion (line 724) — so the component is available; it just wasn't applied to this second toggle.

### Notes

- **Tier-high orange vs amber split** requires a runtime render to confirm which is "correct" — both `#fb923c` (orange, Tailwind 400) and `#fbbf24` (amber) are within the documented algo palette. The inconsistency is visible because AgentToast and AgentFireModal (both pop up to alert the operator about a firing agent) use amber while the agents list page (where the tier filter pill lives) uses orange. They describe the same severity concept.
- **Algo / public theme isolation** is clean — no public `#c8a84b` / `#f0ece3` / `#d4920c` colours found in algo routes, and no algo `#4ade80` / `#fbbf24` / `#0a1020` colours found in public routes (PerformancePage dual-theme is the only shared component, correctly gated by `isDark`).
- **No native `<select>` elements** found in any Svelte file — all dropdowns use the custom `<Select>` / `<MultiSelect>` components as required.
- **tabular-nums** is applied in 92 places across the codebase; spot-checked on number-heavy columns in OrderTicket, MarketPulse, and PriceChart — all present.
- **CollapseButton / DefaultSizeButton / FullscreenButton trio** is consistent across dashboard, options, and orders cards. No rogue expand/collapse implementations beyond the documented two-component pattern.
- **`pnl-gain` / `pnl-loss` / `pnl-zero` and `cell-pos` / `cell-neg` / `cell-flat`** usage confined to their respective themes (PerformancePage/dashboard vs MarketPulse). No cross-contamination of the two families.
