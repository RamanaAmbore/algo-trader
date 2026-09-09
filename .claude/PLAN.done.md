# Plan: Fix candidatesDayPnl — revert to _dayPnlForLeg + stale-while-revalidating

## Context

After commit 202ecd93, `candidatesDayPnl` was changed to read from
`positionsDayPnlStore.byKey[sym]` / `holdingsDayPnlStore.byKey[sym]`.
This introduced a regression: CRUDEOIL and GOLDM show ₹0 in the overlay.

**Root cause**: `positionsDayPnlStore.byKey` returns `_pulseByKey ?? _store.byKey`.
`_pulseByKey` is set by MarketPulse from the positions page. If the user has visited
the positions page, `_pulseByKey` is a non-null object — but it may exclude CRUDEOIL/GOLDM
(closed positions, filtered rows, MCX-only positions not in Pulse's scope). Since
`_pulseByKey != null`, the fallback `_store.byKey` is never used, and
`byKey['CRUDEOIL26SEPFUT']` returns `undefined → 0`.

A secondary issue: the store's `livePositionDayPnl` third branch uses
`(r.last_price - r.close_price) × qty`. For MCX after close, `r.close_price = 0`
(Kite BHAV lag), so this branch fails and falls to `baseDayPnlForPosition(r)`.
`baseDayPnlForPosition` on raw `positionsStore.value` reads `r.day_change_val`, but
`splitClosedReopened` in `buildPositionRowFromBroker` may compute different
`day_change_val` values for candidates vs raw rows.

The original problem (transient zeros) was caused by the `{#if dayPnl !== 0}` guard in
OptionsPayoff being triggered when `candidatePositions` transiently empties during poll
refresh. That guard is already fixed to `{#if dayPnl != null}`.

## Task

Revert `candidatesDayPnl` to use `_dayPnlForLeg(c)` (direct candidate computation,
which is correct and already proven), but add stale-while-revalidating so the value
doesn't transiently return `null` when `candidatePositions` briefly empties during the
5-second poll refresh cycle.

Remove the store imports that are no longer needed for `candidatesDayPnl`.
Keep the `{#if dayPnl != null}` guard in OptionsPayoff (correct, already shipped).

## Agents

- frontend: In `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`:
  1. Remove imports of `positionsDayPnlStore` and `holdingsDayPnlStore` (lines ~61-62,
     added in commit 202ecd93). Only remove if they are not used elsewhere in the file —
     grep for all usages first.
  2. Rewrite `candidatesDayPnl` (lines ~2001-2023) to use `_dayPnlForLeg(c)` with a
     stale-while-revalidating cache:

  ```javascript
  let _lastCandidatesDayPnl = $state(/** @type {number|null} */ (null));
  const candidatesDayPnl = $derived.by(() => {
    void _throttledTick;
    let s = 0;
    let hasLegs = false;
    for (const c of candidatePositions) {
      if (!_isLegEnabled(c)) continue;
      if (!_includeHoldings && c.kind === 'eq') continue;
      hasLegs = true;
      s += _dayPnlForLeg(c, null);
    }
    if (hasLegs) {
      _lastCandidatesDayPnl = s;
      return s;
    }
    // candidatePositions briefly empty during 5s poll refresh — return stale value
    // so OptionsPayoff doesn't flash to null then back.
    return _lastCandidatesDayPnl;
  });
  ```

  Note: `_dayPnlForLeg(c, null)` — pass `null` for `spot` (same as before the SSOT refactor).
  The spot arg is only used for expiry P&L, not day P&L.
  Actually check the `_dayPnlForLeg` signature: it is `function _dayPnlForLeg(c, spot)`.
  For `candidatesDayPnl` the existing callers pass `liveSpot ?? null`. Use `liveSpot ?? null`
  to match the existing pattern, OR check whether `spot` is even used inside `_dayPnlForLeg` —
  if not, pass `null`.

  3. No other changes to OptionsPayoff or the test file.

- backend-test: Update `frontend/e2e/derivatives_pulse_day_pnl_ssot.spec.js` to match the
  reverted implementation:
  - Remove the two tests added in 202ecd93 (`SSOT: candidatesDayPnl reads positionsDayPnlStore`
    and `SSOT: candidatesDayPnl reads holdingsDayPnlStore`)
  - Replace with a test that asserts:
    1. `candidatesDayPnl` uses `_dayPnlForLeg` (grep for pattern)
    2. `_lastCandidatesDayPnl` stale-cache variable is present
    3. `{#if dayPnl != null}` guard (not `!== 0`) is in OptionsPayoff (already passing)
  - The `positionsDayPnlStore` / `holdingsDayPnlStore` import assertions should be removed
    if those imports are removed from the derivatives page.

## Tests

- pytest: no
- svelte-check: yes
- playwright: yes (run the updated spec only)

## Commit message

fix(derivatives): revert candidatesDayPnl to _dayPnlForLeg + stale-while-revalidating cache

## Done when

- CRUDEOIL and GOLDM show correct non-zero day P&L in the overlay (same value as before commit 202ecd93)
- No transient flash to null/0 during poll refresh
- `{#if dayPnl != null}` guard in OptionsPayoff unchanged
- svelte-check 0 errors, playwright spec green
