# Plan: Card header scroll + alignment + NavStrip fixes

## Context

Two problems to fix:

**A — Card header overflow (mobile):** Tabs, chips, and filter rows in card headers wrap to a second row on narrow screens instead of scrolling. Wastes vertical space, can push the card button group off-screen.

**B — NavStrip: scroll safety + cash/margin contrast:**
1. `.ps-strip` has `overflow-x: auto` only inside `@media (max-width: 640px)`. Landscape phones (667px+) and small tablets outside that breakpoint have no scroll safety — pills shrink/overlap. Base rule needs `overflow-x: auto` too.
2. Both M (margin) and C (cash) pills currently use `ps-cash` = `#7dd3fc` (sky-300). Need visual contrast — margin and cash are different things and should read differently at a glance.

**Rules being enforced:**
- Tabs/chips/info in card headers = scrollable on overflow (never wrap to second row)
- Card title + info = left-aligned; card button group = right-aligned (CardHeader.svelte already enforces structurally)
- Cash vs margin contrast in NavStrip (same principle as positions vs holdings contrast)

**Already correct (no change):** AlgoTabs.svelte ✅ · Gainers/losers (use AlgoTabs) ✅ · Payoff card `.opt-section-chips` ✅ · ChartWorkspace range buttons ✅

---

## Agents

- frontend: Apply ALL fixes below (CSS-only except the NavStrip class addition — one agent, all files).

  ### Fix A — Card headers

  **A1. `frontend/src/lib/CardHeader.svelte` — `.ch-middle` (lines 117-122)**
  Single-point fix covering all CardHeader users:
  ```css
  .ch-middle {
    flex: 1 1 0;
    display: flex;
    align-items: center;
    min-width: 0;
    overflow-x: auto;
    overflow-y: visible;
    scrollbar-width: none;
  }
  .ch-middle::-webkit-scrollbar { display: none; }
  ```

  **A2. `frontend/src/lib/MarketPulse.svelte` — `.mp-head-tabs`**
  Find around line 5034. Change `flex-wrap: wrap` → `flex-wrap: nowrap`. Add `overflow-x: auto; scrollbar-width: none`. Add `.mp-head-tabs::-webkit-scrollbar { display: none; }`. Ensure child tab items have `flex-shrink: 0` (check if already set).

  **A3. `frontend/src/lib/PnlPanel.svelte` — `.filter-bar` / `.pill-group`**
  `.filter-bar` (line ~230): add `flex-wrap: nowrap; overflow-x: auto; scrollbar-width: none`. Add `.filter-bar::-webkit-scrollbar { display: none; }`.
  `.pill-group` (line ~255): change `flex-wrap: wrap` → `flex-wrap: nowrap; flex-shrink: 0`.

  **A4. `frontend/src/lib/PnlAnalysis.svelte` — `.filter-bar` + legend chips container**
  `.filter-bar` (line ~890): `flex-wrap: nowrap; overflow-x: auto; scrollbar-width: none`. Add webkit scrollbar hide.
  Find the wrapper element around the `.legend-chip` spans in the template (line ~670) — check what class it uses, then add `flex-wrap: nowrap; overflow-x: auto; scrollbar-width: none` to that class. Add webkit scrollbar hide.

  **A5. `frontend/src/routes/(algo)/dashboard/+page.svelte` — `.eq-legend`**
  Find around line 2342. Change `flex-wrap: wrap` → `flex-wrap: nowrap; overflow-x: auto; scrollbar-width: none`. Add `flex-shrink: 0` to child legend items if not present. Add `.eq-legend::-webkit-scrollbar { display: none; }`.

  ### Fix B — NavStrip

  **B1. `frontend/src/lib/PositionStrip.svelte` — base `.ps-strip` scroll safety**
  In the base `.ps-strip` rule (line ~903), add:
  ```css
  overflow-x: auto;
  overflow-y: visible;
  -webkit-overflow-scrolling: touch;
  scrollbar-width: none;
  ```
  Also add `.ps-strip::-webkit-scrollbar { display: none; }` in the base styles (NOT inside the media query — already present there).
  Also move `flex-shrink: 0` from the `@media (max-width: 640px)` block to the base `.ps-agg` rule so pills never shrink at any viewport width.

  **B2. `frontend/src/lib/PositionStrip.svelte` — cash vs margin contrast**
  Currently both M and C pill values use `ps-cash` = `#7dd3fc` (sky-300). Add a new `.ps-margin` class and apply it to M pill values:
  ```css
  .ps-margin { color: var(--algo-cyan); }  /* #22d3ee — cyan-400, margin/credit */
  /* .ps-cash stays as sky-300 #7dd3fc — liquid deployable cash */
  ```
  In the template: find M pill values (lines ~862-866, `marginAvail` and `marginTotal`) and replace `ps-cash` class with `ps-margin` class on those two spans. Leave C pill unchanged.

  ### Alignment verification
  For cards with inline headers (not using CardHeader): verify left content has `flex: 1; min-width: 0` and right card-group has `flex-shrink: 0`. Change only if wrong.

- backend: skip
- broker: skip
- backend-test: skip
- doc: skip
- playwright: Add spec `frontend/e2e/card-header-scroll.spec.js`:
  - Login
  - Set viewport to 375px × 812px (iPhone 12)
  - Dashboard: assert `.ps-strip` does NOT exceed viewport width (no horizontal page overflow)
  - Dashboard: assert gainers/losers card header stays single-row height (no wrap) at 375px
  - Dashboard: assert `.eq-legend` is single-row (scrollWidth check or height check)
  - Derivatives page: assert payoff chip row is single-row height at 375px
  - NavStrip: navigate to dashboard, at 375px assert `.ps-agg.M` (margin pill) has a different computed color than `.ps-agg.C` (cash pill) — contrast check

## Tests
- pytest: no
- svelte-check: yes
- playwright: yes

## Commit message
```
fix(ui): card headers scroll on overflow + NavStrip scroll safety + cash/margin contrast

Card headers: CardHeader.svelte ch-middle gets overflow-x:auto (single-point
fix for all CardHeader users). Per-surface fixes: mp-head-tabs, PnlPanel
filter-bar, PnlAnalysis filter-bar/legend, dashboard eq-legend — all changed
from flex-wrap:wrap to nowrap+scroll.

NavStrip: overflow-x:auto promoted to base .ps-strip rule (was only in
@media ≤640px — landscape phones outside breakpoint had no scroll safety).
.ps-agg flex-shrink:0 also promoted to base. New .ps-margin class (cyan-400
#22d3ee) on M pill values; C pill keeps sky-300 #7dd3fc — cash and margin
now visually distinct.
```

## Done when
- 375px viewport: all card header chip/tab/filter rows stay single-row (scroll, not wrap)
- 375px viewport: NavStrip pills don't overflow the page
- M pill values render in cyan-400, C pill values in sky-300 — visually distinct
- svelte-check 0 errors
- Playwright spec green
