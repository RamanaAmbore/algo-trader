# Plan: Derivatives — dropdown active-default, TOTAL = sum of rows

## Context

Three live bugs found after the reactive-chain deploy. Book has ONLY CRUDEOIL and GOLDM FUT positions (no equity, no COPPER positions at all).

1. **Dropdown defaults to COPPER** (watchlist item, no position) instead of CRUDEOIL/GOLDM (active FUT positions)
2. **TOTAL day P&L (-78k) ≠ sum of per-rows (-16k + -8k = -24k)** — formula divergence, not equity/exchange filter issue
3. **Payoff/legs hanging** — caused by #1: COPPER selected → no active legs → `strategy = null` → spinner forever

---

## Root Causes (code-verified)

### Bug 1 — Dropdown selects COPPER (root cause of payoff + legs issues)

COPPER is in the watchlist (**Tier 5** — `hint: 'watchlist'`). CRUDEOIL/GOLDM with FUT positions are in **Tier 2** (`hint: 'futures'`).

**Cold-load sequence:**
1. `positions = []` (not yet loaded) → Tier 1+2 empty → `opts[0]` = COPPER (Tier 5 watchlist)
2. Auto-select $effect: `cur = ''` → picks `opts[0]` = COPPER
3. `loadPositions()` completes → positions=[CRUDEOIL_FUT, GOLDM_FUT] → Tier 2 = [CRUDEOIL, GOLDM]
4. `underlyingOptionsForPicker` recomputes → COPPER moves to Tier 5, CRUDEOIL is now `opts[0]`
5. Auto-select $effect re-fires: `cur = 'COPPER'`, `curInOpts = {hint:'watchlist'}`, `curIsPopular = false`
6. Current promote condition: `curIsPopular && opts[0]?.hint !== 'popular'` → **false** (not popular) → no promote
7. **COPPER stays selected** ← Bug

COPPER has no active legs → `_hasEnabledLegs = false` → strategy wipe guard fires → `strategy = null` → chart hangs.

### Bug 2 — TOTAL uses different formula than per-rows

`_snapshotTotalDay` (line ~3440) calls `livePositionDayPnl` on raw `positionsStore.value` rows.
Per-row grid (`_dayPnlByRootMap`, line 964) calls `_dayPnlForLeg` via `_perRootReduce` on transformed rows.

**Formula divergence:**
- `_dayPnlForLeg`: `(liveLtp - prev_close) × qty` (mark-to-close from live tick)
- `livePositionDayPnl`: `baseDayPnlForPosition(p) + (live - pollLtp) × qty`
  - When `prev_settlement_pnl` is set: `baseDayPnlForPosition = pnl - prev_settlement_pnl` (unrelated to close price)
  - This diverges from `(live - prev_close) × qty` by `(pnl - prev_settlement_pnl) - (pollLtp - prev_close) × qty`

For MCX FUT positions with `prev_settlement_pnl` set, this divergence can be tens of thousands of rupees.

`_snapshotTotalPnl` and `_snapshotTotalExp` (lines 987-992) are correctly derived as sums of `_pnlByRootMap` / `_expPnlByRootMap`. `_snapshotTotalDay` was rewritten in the reactive-chain deploy to use `livePositionDayPnl` instead of summing `_dayPnlByRootMap` — the old comment "Using _dayPnlByRootMap was wrong" referred to the OLD `_dayPnlByRootMap` (which used `_expiryPnl`). Since that deploy, `_dayPnlByRootMap` uses `_dayPnlForLeg` — the correct formula.

---

## Fixes

### Fix 1 — Add `qtySum` to picker options; one-time promote on positions load

**File: `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`**

**Step A** — Add `let _autoSelectDone = $state(false);` near the other `$state` vars (around line 1030).

**Step B** — In `underlyingOptionsForPicker` ($derived.by, line ~1469), add `qtySum` to each emitted option. The local `_rootQtySum` Map is already built at line 1474:

```js
// Tier 1 (line ~1487)
out.push({ value: u, label: u, hint: 'options', qtySum: _rootQtySum.get(u) || 0 });
// Tier 2 (line ~1495)
out.push({ value: u, label: u, hint: 'futures', qtySum: _rootQtySum.get(u) || 0 });
// Tier 3 (line ~1511)
out.push({ value: u, label: u, hint: 'holdings', qtySum: 0 });
// Tier 4 (line ~1519)
out.push({ value: u, label: u, hint: 'pinned', qtySum: 0 });
// Tier 5 (line ~1526)
out.push({ value: u, label: u, hint: 'watchlist', qtySum: 0 });
// Tier 6 (line ~1538)
out.push({ value: u, label: u, hint: 'popular', qtySum: 0 });
```

**Step C** — Update the auto-select $effect (~line 1550) to:
1. On initial `!cur`: prefer first active option (qtySum > 0), fall back to `opts[0]`
2. Add a one-time promote (guarded by `_autoSelectDone`) that fires when positions first load and current selection has no active qty

```js
$effect(() => {
  void positions; void holdings; void _rootsWithOptions; void _rootsWithFuturesOnly;
  void _positionsLoaded; void _pinnedWatchlistRoots; void _regularWatchlistRoots;
  const opts = underlyingOptionsForPicker;
  const cur  = untrack(() => selectedUnderlying);
  // First option with active positions; fallback to opts[0]
  const firstActive = opts.find(o => (o.qtySum || 0) > 0) ?? opts[0];

  // Standard case: nothing selected yet — pick first active (skip popular if positions exist)
  if (!cur) {
    const first = (firstActive ?? opts[0])?.value;
    if (first) untrack(() => { selectedUnderlying = first; });
    return;
  }

  const curInOpts = opts.find(o => o.value === cur);
  if (!curInOpts && opts[0]?.value) {
    // Stale cache: previously-selected underlying no longer in options — reset to first.
    untrack(() => { selectedUnderlying = opts[0].value; });
    return;
  }

  const curIsPopular = curInOpts?.hint === 'popular';
  const curHasActiveQty = (curInOpts?.qtySum || 0) > 0;
  const bestHasActiveQty = (firstActive?.qtySum || 0) > 0;

  // Promote: popular provisional → any position tier (existing logic, unchanged)
  if (curIsPopular && firstActive?.hint !== 'popular') {
    untrack(() => { selectedUnderlying = firstActive.value; });
    return;
  }

  // One-time promote: fires when positions first load and current is a non-active
  // provisional (watchlist/pinned selected before positions were available).
  // Guard: _autoSelectDone prevents re-firing on every subsequent 5s position refresh
  // so user can manually pick an inactive underlying (e.g. COPPER for analysis)
  // without being immediately bounced back to the active one.
  if (!untrack(() => _autoSelectDone) && _positionsLoaded && !curIsPopular && !curHasActiveQty && bestHasActiveQty) {
    untrack(() => {
      _autoSelectDone = true;
      selectedUnderlying = firstActive.value;
    });
  }
});
```

### Fix 2 — TOTAL = sum of `_dayPnlByRootMap` (same formula as per-rows)

**File: `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`**

Replace `_snapshotTotalDay` body (line ~3440) to sum from `_dayPnlByRootMap` — identical
pattern to `_snapshotTotalPnl` / `_snapshotTotalExp` (lines 987-992):

```js
const _snapshotTotalDay = $derived.by(() => {
  void _throttledTick;
  return Object.values(_dayPnlByRootMap).reduce((s, v) => s + Number(v || 0), 0);
});
```

This guarantees TOTAL = sum of per-row day P&L values BY CONSTRUCTION (same source, same formula).
No `isFOSymbol` filter needed — `_dayPnlByRootMap` reads from `positions` state which already
contains only F&O rows (via `buildPositionRowFromBroker` + `isFOSymbol` in the positions $effect).

Remove the now-wrong `title="Includes all positions (equity intraday + F&O)"` tooltip from the TOTAL row.

---

## Agents

- backend: skip
- frontend: Apply Fix 1 (qtySum in picker options + one-time promote via _autoSelectDone) and Fix 2 (sum _dayPnlByRootMap in _snapshotTotalDay, remove old tooltip title). Both changes in `+page.svelte`.
  For every file you change or create, you MUST write or update at least one test that covers the changed behaviour. This is mandatory — not optional. No change ships without a corresponding test update.
- broker: skip
- doc: skip
- backend-test: skip
- playwright: Update `frontend/e2e/derivatives_reactive_chain.spec.js` — fix Spec 3 (TOTAL now = sum of per-rows) and add Spec 5 (dropdown auto-selects active underlying on first positions load, not watchlist provisional).

## Tests
- pytest: no
- svelte-check: yes
- playwright: yes

## Commit message
fix(derivatives): dropdown one-time promote to active-qty underlying; TOTAL = sum of per-rows

## Done when
- Cold load selects CRUDEOIL or GOLDM (highest active qty), not COPPER (watchlist provisional)
- After first promote, user can manually pick COPPER without being bounced back
- Payoff chart renders on cold load for CRUDEOIL/GOLDM
- Snapshot TOTAL = sum of CRUDEOIL + GOLDM per-row values (formula parity)
- svelte-check: 0 errors
- Playwright specs green
