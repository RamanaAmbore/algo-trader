# Plan: Draft-position UX round 2 — long-press select, OrderBook/LogPanel draft relabel, scoped payoff draft-toggle

## Task

Four follow-ups to the draft-positions feature shipped in commit `6bf3dfd7`:

1. **Positions grid (MarketPulse) + Legs grid (derivatives)**: replace the per-row checkbox
   (`.draft-select-cb` / `.cand-draft-select`) with a long-press (touch) / right-click (desktop)
   gesture that enters a per-grid "selecting" mode and toggles that row into the selection. The
   header "Clear Selected Drafts (N)" button is hidden entirely while nothing is selected, and
   appears only once selection starts.
2. **OrderBook.svelte + LogPanel.svelte**: a draft `AlgoOrder` row (`status="OPEN", mode="draft"`)
   currently renders and counts exactly like a genuine resting broker order under "Open" — relabel,
   don't hide: exclude `mode==='draft'` from every status-chip count/classification, but keep the
   row visible in the merged list with a DRAFT pill instead of a status badge.
3. **Chase visibility for drafts**: confirmed already correct (`list_active_chases` excludes
   `mode='draft'`; OrderBook's chase chip requires `attempts>0`, which a draft never has). No code
   change — add a regression test only.
4. **Derivatives payoff page**: reinstate a draft-inclusion toggle, but scoped per-underlying (a
   `Map`, not the old single global boolean that was deleted in `6bf3dfd7` for needing too much
   special-casing), folded into the ONE existing `_isLegEnabled(c)` predicate every aggregate
   already reads (not re-scattered `!showDraft &&` checks at ~8 sites — that was the original
   design's mistake). Button visible only when the currently-viewed underlying has at least one
   draft leg. Defaults to ON (include) per operator confirmation — the workflow is "compare
   with-draft vs. without-draft payoff/Greeks without deleting the draft." Scope: affects payoff
   curve, the overlay/"layover" stat card, Greeks, Snapshot, and the derived aggregate totals
   (candidatesDayPnl, Exp P&L totals, extrinsic totals) — does NOT hide the draft row from the
   Legs/Positions grids, which keep showing it unconditionally (that's what long-press Clear is for).

Council findings folded in:
- **Architect**: per-underlying `Map` is required (Snapshot/EV render rows for underlyings other
  than the currently-charted one); fold the gate into `_isLegEnabled` alone; reuse the SAME
  underlying-canonicalization helper already backing `selectedUnderlying` comparison (not a fresh
  `rootOf()`), to avoid MCX virtual-root-aliasing drift between button visibility and gate
  application.
- **Risk**: fix must land in BOTH `OrderBook.svelte` AND `LogPanel.svelte` (near-duplicate
  consumers of the same unguarded `open` predicate) in the same commit, or it's a partial,
  CLAUDE.md-flagged fix. Recommend relabel-not-hide — hiding risks an operator forgetting a draft
  exists; a false "OPEN" risks the operator mistaking it for a real resting order, which is worse.
  Navbar chase chip (`/events/recent?status=open`) is a dormant residual (safe today only because
  `create_draft` never writes an `AlgoOrderEvent`) — note as a watch-item, no fix needed now.
- **UX**: `longPress.js` already drives a context-menu gesture on draft rows in the Legs grid
  (`CandidateLegRow.svelte`, derivatives `+page.svelte`) — the new "select for clear" gesture must
  not collide with it. Resolution: for DRAFT rows specifically, repurpose that same long-press/
  right-click handler to mean "select for clear" instead of opening the real-position context menu
  (verify first whether that menu offers anything meaningful for a draft — pin/GTT entries don't
  apply to a non-broker row, so this is likely a clean behavioral swap, not a loss). Real rows'
  context-menu is untouched. Add an explicit Cancel/✕ control next to the Clear button so selection
  mode can be exited without deleting (nothing in the current checkbox-only code has this — it's a
  genuine gap, not optional polish). Discoverability: keep the existing pink `row-draft` tint as the
  passive cue, add a hover tooltip ("Right-click or long-press to select for removal") on draft rows
  — the header button itself stays hidden until selection starts, per the operator's explicit ask.
- **Perf**: event-delegate the long-press/select logic on the grid container (existing
  `target.closest('.draft-select-cb')`-style pattern), not per-row listeners — ag-Grid's
  cellRenderer returns an HTML string and tears down nodes on virtualization, so per-row timers
  would leak/orphan. Handle `pointercancel` in addition to `pointerup`/`pointermove`. Use
  `SvelteMap` (from `svelte/reactivity`) for the per-underlying toggle map, not a plain `Map` in
  `$state` (plain `Map.set()` doesn't trigger Svelte 5 reactivity).

## Agents

- **frontend**: Implement all four items.
  - *MarketPulse.svelte*: remove `.draft-select-cb` input; reuse `longPress.js` on draft rows to
    enter/toggle selection (repurposing its existing contextmenu+touch dual-binding as the desktop/
    mobile gesture pair — do not build a separate mousedown-timer from scratch); while selection
    mode is active for this grid, plain clicks on OTHER draft rows toggle membership instead of
    opening the promote ticket; `.mp-clear-drafts-btn` renders only when `_selectedDraftIds.size >
    0` (was: always rendered, `disabled` at 0) with an adjacent small Cancel/✕ that empties the
    selection without deleting; replace the removed checkbox's visual with a selected-row CSS state
    (border/tint on top of existing `row-draft`).
  - *CandidateLegRow.svelte + derivatives `+page.svelte`*: mirror the same pattern for
    `.cand-draft-select` / `_selectedDraftIds`; resolve the long-press collision as described above
    (draft rows: long-press/right-click = select-for-clear; real rows: unchanged).
  - *OrderBook.svelte*: add `mode !== 'draft'` to every status-chip classification predicate
    (`open`, `complete`, `rejected_cancelled`, `held`, the chase check already excludes drafts via
    `attempts>0`); render a DRAFT pill (reuse the `mode-pill-draft` token) on a draft row in place
    of its status badge; drafts stay visible in the merged grid, just uncounted in every chip.
  - *LogPanel.svelte*: identical fix — same unguarded `open` predicate at a near-identical call
    site, must not be left out (risk council flagged this explicitly).
  - *derivatives `+page.svelte`*: add `SvelteMap`-backed `_draftIncludeByUnderlying` (default
    missing-key = include/true); extend `_isLegEnabled(c)` to
    `checkbox-state && (!c.isDraft || _draftIncludeByUnderlying.get(underlyingKey(c)) !== false)`
    using the SAME underlying-key helper that already backs `selectedUnderlying` comparison; add
    the DRAFT toggle button near the payoff chart, visible only when
    `candidatePositions` filtered to the current underlying contains a `source==='draft'` leg; no
    localStorage persistence (session-only state, resets to ON on reload — simpler than the deleted
    global toggle's persisted key, and avoids unbounded per-underlying key growth).
- **playwright**: Update `draft_positions_pulse.spec.js` test C (checkbox → long-press/right-click
  select + Cancel + Clear); mirror for the Legs grid equivalent spec; new assertions that a draft
  row in OrderBook never counts toward the "Open" chip and renders a DRAFT pill; new derivatives
  spec for the payoff toggle (button hidden with no draft for that underlying, appears once one
  exists, toggling excludes/includes the draft leg from payoff/Greeks/Snapshot while the Legs grid
  row stays put regardless).
- **doc**: Update `docs/specs/PULSE_SPEC.md` (draft row selection UX) and the derivatives feature
  notes in CLAUDE.md's Critical-math-guards-adjacent draft section (new entry documenting the
  scoped-per-underlying toggle and why it differs from the deleted global one).

## Tests

- pytest: no (no backend changes)
- svelte-check: yes
- playwright: yes
- vitest: yes (new unit test for `_isLegEnabled`'s draft-map branch, and OrderBook/LogPanel
  draft-exclusion predicate)

## Commit message

```
feat(draft-ux): long-press select for drafts, OrderBook/LogPanel draft relabel, scoped per-underlying payoff draft-toggle
```

## Done when

- Positions and Legs grids: checkbox UI gone; long-press (touch) / right-click (desktop) on a draft
  row enters selection mode and toggles that row; header Clear button hidden at 0 selected, visible
  with count + Cancel once selection starts; Clear removes exactly the selected drafts.
- OrderBook and LogPanel: a draft row never counts toward any status chip, shows a DRAFT pill, and
  remains visible in the merged order list.
- Chase: regression test confirms drafts never appear in the active-chases panel or the OrderBook
  chase chip (no code change needed).
- Derivatives payoff page: DRAFT toggle button appears only when the current underlying has a draft
  leg; defaults ON; toggling OFF excludes that underlying's draft legs from payoff curve/overlay
  stat/Greeks/Snapshot/aggregate totals while the Legs/Positions grid rows remain visible and
  selectable/clearable regardless of toggle state.
- svelte-check 0 errors, vitest + playwright green.
