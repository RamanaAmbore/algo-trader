# Plan: Fix closed F&O options excluded from exp P&L when instrument not in cache

## Context / Root Cause

When a CRUDEOIL option position is closed (qty=0), its locked-in realized P&L should appear
in the derivatives overlay exp P&L total (`_legsExpPnlTotal`) and NavStrip P3. It doesn't.

`buildCandidatePositions` (pageLoad.js) applies three filters to all positions — open AND closed:

```javascript
if (!matchExpiry(sym)) continue;             // line 311 — expiry filter
const _inst = getInstrument(sym);
if (!_inst) continue;                         // line 314 — instrument lookup
if (_inst.x && _inst.x < todayIST()) continue; // line 316 — expired contract
```

The failure path: deep OTM CRUDEOIL options (illiquid, low open interest) are OMITTED from
Kite's `/api/instruments` master dump. `getInstrument(sym)` returns null for these symbols.
The `if (!_inst) continue` silently drops the closed position. Its `realised` P&L (e.g. 136,174)
never reaches `_legExpPnlDisplay`, so the overlay shows only open positions' intrinsic.

The operator observes: overlay = 269,826 (= open intrinsic only), Kite = ~406,000 (= open MTM
+ closed realized). "The value you are showing may be correct for tomorrow [after positions
disappear from broker API], but not today."

All three guards need a `qty !== 0` gate: closed positions (qty=0) have locked-in P&L that
needs NO instrument lookup — their `realised || pnl` is self-contained.

**NavStrip P3**: `_expPnlByRootMap` iterates `candidatePositions` (via `_perRootReduce`).
Fixing `buildCandidatePositions` fixes NavStrip P3 automatically — no separate change needed.

## Agents

- frontend: In `frontend/src/lib/derivatives/pageLoad.js`, apply three guards in
  `buildCandidatePositions` (lines 311-316):

  **Fix 1 — expiry filter (line 311):**
  Change:
  ```javascript
  if (!matchExpiry(sym)) continue;
  ```
  To:
  ```javascript
  if (Number(p?.qty || 0) !== 0 && !matchExpiry(sym)) continue;
  ```

  **Fix 2 — instrument lookup (lines 313-314) — most important:**
  Change:
  ```javascript
  const _inst = getInstrument(sym);
  if (!_inst) continue;
  ```
  To:
  ```javascript
  const _inst = getInstrument(sym);
  if (!_inst && Number(p?.qty || 0) !== 0) continue;
  ```

  **Fix 3 — expired contract filter (line 316):**
  Change:
  ```javascript
  if (_inst.x && _inst.x < todayIST()) continue;
  ```
  To:
  ```javascript
  if (_inst?.x && _inst.x < todayIST() && Number(p?.qty || 0) !== 0) continue;
  ```
  (Note: `_inst?.x` because `_inst` may now be null for closed positions.)

  No other changes needed.

- backend-test: Add/update test cases in `frontend/src/lib/__tests__/data/pageLoad_expired.test.js`
  (the file already has `makeGetInst`, `vi.mock('$lib/dateFormat.js', ...)` pattern, and
  imports `buildCandidatePositions`).

  **Test A** — closed option where getInstrument returns null (not in cache):
  ```javascript
  it('closed option not in instruments cache still contributes realised P&L', () => {
    // getInstrument returns null for this deep-OTM closed option.
    const getInstrument = () => null;
    const result = buildCandidatePositions({
      positions: [
        {
          symbol: 'CRUDEOIL11SEP26P5800CE', account: 'ZG0790', qty: 0,
          realised: 136174, pnl: 136174, source: 'live',
          overnight_quantity: 0, day_buy_quantity: 0, day_sell_quantity: 0,
          day_buy_value: 0, day_sell_value: 0,
        },
        {
          symbol: 'CRUDEOIL11SEP26P6200CE', account: 'ZG0790', qty: 25,
          realised: 0, pnl: 20000, source: 'live',
          overnight_quantity: 25, day_buy_quantity: 0, day_sell_quantity: 0,
          day_buy_value: 0, day_sell_value: 0,
        },
      ],
      holdings: [], drafts: [],
      target: 'CRUDEOIL',
      selectedExpiries: [],
      selectedAccounts: [],
      simActive: false,
      proxiesForTarget: () => [],
      getInstrument,
      provisionalPositions: [], draftStorePositions: [],
    });
    // Closed position must appear even though instrument is not in cache.
    const syms = result.map(r => r.symbol);
    expect(syms).toContain('CRUDEOIL11SEP26P5800CE');
    const closed = result.find(r => r.symbol === 'CRUDEOIL11SEP26P5800CE');
    expect(Number(closed?.realised)).toBe(136174);
  });
  ```

  **Test B** — closed option in expired contract still included:
  ```javascript
  it('closed option in expired contract still contributes (expired-contract filter)', () => {
    // expiry '2026-09-07' is in the past relative to todayIST mock '2026-09-11'.
    const getInstrument = makeGetInst({ 'CRUDEOIL7SEP26P5800CE': '2026-09-07' });
    const result = buildCandidatePositions({
      positions: [{
        symbol: 'CRUDEOIL7SEP26P5800CE', account: 'ZG0790', qty: 0,
        realised: 136174, pnl: 136174, source: 'live',
        overnight_quantity: 0, day_buy_quantity: 0, day_sell_quantity: 0,
        day_buy_value: 0, day_sell_value: 0,
      }],
      holdings: [], drafts: [],
      target: 'CRUDEOIL', selectedExpiries: [], selectedAccounts: [],
      simActive: false,
      proxiesForTarget: () => [],
      getInstrument,
      provisionalPositions: [], draftStorePositions: [],
    });
    expect(result).toHaveLength(1);
    expect(result[0].symbol).toBe('CRUDEOIL7SEP26P5800CE');
  });
  ```

  Note: `buildCandidatePositions` returns a flat array (`[...real, ...provisional, ...draftStore]`),
  NOT `{ real }`. Check `result.toHaveLength()` and `result.find()`, not `result.real`.

- playwright: skip
- doc: skip

## Tests
- pytest: no
- svelte-check: yes
- playwright: no
- vitest: yes

## Commit message
fix(derivatives): include closed F&O positions in exp P&L when instrument not in instruments cache

## Done when
- `npx vitest run` passes including both new tests
- `npx svelte-check` 0 errors
- Closed CRUDEOIL options with no instrument cache entry appear in `candidatePositions`
  and their `realised || pnl` contributes to `_legsExpPnlTotal` and `_expPnlByRootMap` (NavStrip P3)
- Three guards in `buildCandidatePositions` all use `qty !== 0` condition
