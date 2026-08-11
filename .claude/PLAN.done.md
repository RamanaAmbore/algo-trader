# Plan: Fix NavStrip H:1 + P:1 = 0 — positions day P&L mirrors holdings logic

## Context

NavStrip H:1 (holdings day P&L) = 0 while Pulse Holdings TOTAL = −5K.  
NavStrip P:1 (positions day P&L) = 0 after market close; Pulse positions TOTAL also = 0 after close.

**Root cause — H:1:**  
`_liveHoldingsToday` in `PositionStrip.svelte` sums `day_change_val + _delta`. Broker returns `day_change_val = 0` early in session and after close. Pulse Holdings TOTAL (`mergeHoldingRows` in pulseUnified.js line 553) uses `(ltp − close_price) × qty` from symbolStore — no market-open gate.

**Root cause — P:1 / Pulse positions = 0 after close:**  
`mergePositionRows` (pulseUnified) and `_livePositionsToday` (PositionStrip) both gate LTP by `isMarketOpen()` and delegate to `livePositionDayPnl`. After close: `liveLtp = null` → falls back to broker `day_change_val = 0`. Holdings has no such gate — that's why holdings shows −5K but positions shows 0.

**Fix (operator direction):**  
Positions day P&L uses the **identical formula** as holdings: `(ltp − close_price) × qty` from symbolStore, unconditionally. Fallback to `baseDayPnlForPosition(r)` only when no LTP available. This replaces the `livePositionDayPnl` call in the positions day P&L path for both Pulse and NavStrip.

## Holdings formula (SSOT — do not change)

`mergeHoldingRows` in `pulseUnified.js` lines 549-558:
```javascript
const liveHold = (_snapLtp != null && Number(_snapLtp) > 0) ? Number(_snapLtp)
               : (Number(liveQ?.ltp) > 0 ? Number(liveQ.ltp) : null);
const holdClose = Number(r.close_price) || 0;
if (liveHold != null && holdClose > 0 && heldQty !== 0) {
  row.day_pnl = (row.day_pnl ?? 0) + (liveHold - holdClose) * heldQty;
} else {
  row.day_pnl = (row.day_pnl ?? 0) + (Number(r.day_change_val) || 0);
}
```

Positions day P&L must match this pattern exactly.

## Task

Four edits, positions day P&L path replaces `livePositionDayPnl` with direct `(ltp − close) × qty`:

### Edit 1 — `frontend/src/lib/PositionStrip.svelte` lines 446-450 (`_liveHoldingsToday`)

Replace `day_change_val + _delta` with holdings-style formula:

```javascript
const _liveHoldingsToday = $derived.by(() => {
  let s = 0;
  for (const h of holdings) {
    const sym   = String(h?.tradingsymbol || '').toUpperCase();
    const ltp   = getSnapshot(sym)?.ltp;
    const close = Number(h?.close_price || 0);
    const qty   = Number(h?.opening_quantity || h?.quantity || 0);
    if (ltp != null && Number(ltp) > 0 && close > 0 && qty !== 0) {
      s += (Number(ltp) - close) * qty;
    } else {
      s += Number(h?.day_change_val || 0) + _delta(h, 'H');
    }
  }
  return s;
});
```

Notes: no `_throttledTick` / `untrack()` — matches `_liveHoldingsTotal` pattern. `opening_quantity || quantity` for qty matches `mergeHoldingRows` line 522.

### Edit 2 — `frontend/src/lib/PositionStrip.svelte` lines 417-445 (`_livePositionsToday`)

Replace `livePositionDayPnl` call with direct `(ltp − close_price) × qty` — mirror of `_liveHoldingsToday`:

```javascript
const _livePositionsToday = $derived.by(() => {
  void _throttledTick;
  let dayTotal = 0;
  for (const p of positions) {
    const sym   = String(p?.tradingsymbol || '').toUpperCase();
    const ltp   = untrack(() => getSnapshot(sym)?.ltp);
    const close = Number(p?.close_price ?? 0);
    const qty   = Number(p?.quantity    ?? 0);
    if (ltp != null && Number(ltp) > 0 && close > 0 && qty !== 0) {
      dayTotal += (Number(ltp) - close) * qty;
    } else {
      dayTotal += baseDayPnlForPosition(p);
    }
  }
  return dayTotal;
});
```

Notes: `untrack()` preserved (throttle). `baseDayPnlForPosition` as fallback handles the `overnight_quantity=0, pnl≠0` new-position edge case when no LTP available. Remove unused `const _mktOpen` and `livePositionDayPnl` from this block (but keep the `livePositionDayPnl` import if still used elsewhere — check first).

### Edit 3 — `frontend/src/lib/data/pulseUnified.js` lines 453-468 (`mergePositionRows`)

Replace `livePositionDayPnl` call with direct `(ltp − close_price) × qty` — identical pattern to `mergeHoldingRows`:

```javascript
// Day P&L — same formula as mergeHoldingRows: (ltp − close) × qty.
// symbolStore LTP first; no isMarketOpen() gate — last tick persists after close.
const _snapLtp   = snap?.ltp;
const posLiveLtp = (_snapLtp != null && Number(_snapLtp) > 0) ? Number(_snapLtp)
                 : (Number(liveQ?.ltp) > 0 ? Number(liveQ.ltp) : null);
const posCls     = Number(r.close_price) || 0;
if (posLiveLtp != null && posCls > 0 && q !== 0) {
  row.day_pnl = (row.day_pnl ?? 0) + (posLiveLtp - posCls) * q;
} else {
  row.day_pnl = (row.day_pnl ?? 0) + baseDayPnlForPosition(r);
}
```

Remove the old `const _mktOpen = isMarketOpen();` and `livePositionDayPnl(...)` call lines.

### Edit 4 — `frontend/src/routes/(algo)/admin/derivatives/+page.svelte` line 1973-1984 (`_dayPnlForLeg`)

Align derivatives leg day P&L with same pattern — use symbolStore LTP unconditionally:

```javascript
function _dayPnlForLeg(c, spot) {
  const legLiveLtp = untrack(() => getSnapshot(String(c.symbol || '').toUpperCase())?.ltp);
  const close = Number(c.prev_close ?? 0);
  const qty   = Number(c.qty ?? 0);
  if (legLiveLtp != null && Number(legLiveLtp) > 0 && close > 0 && qty !== 0) {
    return (Number(legLiveLtp) - close) * qty;
  }
  return baseDayPnlForPosition(c);
}
```

## Agents
- frontend: Make all 4 edits above. Read each file carefully before editing (use Read tool first).
  - Check whether `livePositionDayPnl` import is still needed in PositionStrip.svelte after Edit 2 — remove if unused.
  - Run `npx svelte-check --output machine 2>&1`. Fix any type errors.
  - Add Vitest tests in `frontend/src/lib/__tests__/data/`:
    - New file `positions_holdings_ssot.test.js`: test `mergePositionRows` with `snap.ltp=1005, close_price=1000, qty=10, isMarketOpen()=false` → `row.day_pnl = 50`. Test `mergePositionRows` with no LTP → falls back to `baseDayPnlForPosition`. Test `mergeHoldingRows` and `mergePositionRows` return same formula result for same inputs (ltp, close, qty).
  - Update `frontend/e2e/holdings_navstrip_ssot.spec.js`: add source-scan assertions that `_liveHoldingsToday` uses `close_price` and `_livePositionsToday` uses `close_price` (same formula in both).
  - Update `frontend/e2e/pnl_positions_closed_hours_ssot.spec.js`: assert neither `_livePositionsToday` nor `mergePositionRows` day_pnl path calls `livePositionDayPnl` (replaced by direct formula).
  
  For every file you change or create, you MUST write or update at least one test covering the changed behaviour. This is mandatory — not optional.
- backend: skip
- broker: skip
- doc: Update `docs/specs/NAVSTRIP_SPEC.md` — H:1 and P:1 both use `(ltp − close_price) × qty` (symbolStore-first, no market-open gate, identical formula for holdings and positions).
- backend-test: skip
- playwright: skip

## Tests
- pytest: no
- svelte-check: yes
- playwright: yes — run `npx playwright test e2e/holdings_navstrip_ssot.spec.js e2e/pnl_positions_closed_hours_ssot.spec.js --reporter=line 2>&1` and print full output (pass/fail counts + any assertion errors)

## Commit message
fix(navstrip): positions day P&L mirrors holdings formula — `(ltp − close) × qty` no market-open gate

## Done when
- NavStrip H:1 = Pulse Holdings TOTAL (e.g. −5K)
- NavStrip P:1 = Pulse Positions TOTAL (non-zero after close for overnight positions)
- Pulse Positions TOTAL non-zero after market close
- svelte-check 0 errors
- Vitest `positions_holdings_ssot.test.js` passes
