# Plan: Fix holdings day P&L — apply positions pattern + broker cross-check suite

## Context

**Root bug — holdings SSOT override inflates per-symbol day_pnl when account filter active:**
`buildUnified` in `MarketPulse.svelte:2979` overrides per-symbol `day_pnl` with
`holdingsDayPnlStore.byKey[sym]` — the combined ALL-ACCOUNTS value. When filter is ZG0790,
`mergeHoldingRows` correctly computes `(liveHold−holdClose) × ZG0790_qty`, then the override
replaces it with the combined multi-account value. ZG0790 alone=2.18L, ZJ6294=1.72L, total=2.24L
→ 3.90L >> 2.24L.

Positions has NO such override and works correctly. The fix is to apply the same pattern.

**Positions pattern (reference — already correct):**
1. `mergePositionRows` computes per-symbol day_pnl from `scopedPositions` (filtered)
2. No SSOT override
3. `$effect` reads `unifiedRows.filter('positions')`, calls `positionsDayPnlStore.setFromPulse(byKey, total)`
4. `positionsDayPnlStore.total` = `_pulseTotal ?? _store.total`

**Holdings pattern (to match):**
1. `mergeHoldingRows` computes per-symbol day_pnl from `scopedHoldings` (filtered) — already correct
2. **Remove** the SSOT override at lines 2979-2982 entirely
3. Add `$effect` reading `unifiedRows.filter('holdings')` → `holdingsDayPnlStore.setFromPulse(byKey, total)`
4. `holdingsDayPnlStore` gains `setFromPulse` + `_pulseTotal`/`_pulseByKey`; `.total` = `_pulseTotal ?? _store.total`
5. `_store` derivation also computes `byAccount` (per-account sums from ALL pulseHoldingsStore rows)
6. Summary grid `holdingsSummaryData.day_pnl` reads `holdingsDayPnlStore.byAccount[r.account]`

**Other fixes:**
- `pulseUnified.js:526`: remove `opening_quantity` fallback (CLAUDE.md violation)
- Summary / H slot inconsistency: resolved by byAccount being sourced from the same
  `pulseHoldingsStore.value` as NavStrip H (via `_store`)

**Test gap — root cause of all escapes:**
All tests use mocked data and test internal logic only. No test cross-checks computed values
against broker API responses. `test_holdings_day_pnl_crosscheck.py` is the right pattern but
covers only holdings. No equivalent for positions, funds, margins.

## Task
Apply positions pattern to holdings (fixes inflation + NavStrip H consistency).
Add `backend/tests/broker/test_book_crosscheck.py` covering all book surfaces.

## Agents
- frontend:
  **Change 1 — MarketPulse.svelte: remove SSOT override, add holdings $effect**

  DELETE lines 2976-2982 (the byKey SSOT override block):
  ```javascript
  // DELETE THIS ENTIRE BLOCK:
  // SSOT override: holdingsDayPnlStore wins for day_pnl on every holding
  // row. holdingsDayPnlStore.byKey is keyed by plain uppercase tradingsymbol;
  // pulseUnified byKey is keyed "SYMBOL__hold" — append the suffix directly.
  for (const [sym, val] of Object.entries(holdingsDayPnlStore.byKey)) {
    const row = byKey[`${sym}__hold`];
    if (row) row.day_pnl = val;
  }
  ```

  ADD a new `$effect` mirroring the positions one (insert after the positions $effect at ~line 2947):
  ```javascript
  // Holdings day P&L — mirror positions pattern. Pulse is authoritative;
  // setFromPulse writes to holdingsDayPnlStore so NavStrip H reads the
  // same cq-accurate value as the main grid displays.
  let _lastHoldTotal = null;
  $effect(() => {
    const holdRows = unifiedRows.filter(r => r._majorGroup === 'holdings');
    const holdByKey = {};
    let holdTotal = 0;
    for (const r of holdRows) {
      const sym = String(r?.tradingsymbol || r?.symbol || '').toUpperCase();
      if (!sym) continue;
      const v = Number(r.day_pnl) || 0;
      holdByKey[sym] = (holdByKey[sym] ?? 0) + v;
      holdTotal += v;
    }
    if (Math.round(holdTotal * 100) !== Math.round((_lastHoldTotal ?? NaN) * 100)) {
      _lastHoldTotal = holdTotal;
      holdingsDayPnlStore.setFromPulse(holdByKey, holdTotal);
    }
  });
  ```

  ALSO update holdingsSummaryData line 2383:
  ```javascript
  day_pnl: holdingsDayPnlStore.byAccount[r.account] ?? Number(r.day_change_val) || 0,
  ```

  **Change 2 — holdingsDayPnlStore.svelte.js: add setFromPulse + byAccount**

  Add module-level pulse override state (mirrors positionsDayPnlStore):
  ```javascript
  let _pulseTotal = $state(/** @type {number|null} */ (null));
  let _pulseByKey = $state(/** @type {Record<string,number>|null} */ (null));
  ```

  Add `byAccount` to `_store` derivation (alongside existing `total` and `byKey`):
  ```javascript
  const byAccount = {};
  // inside row loop, after computing val:
  const acc = String(h?.account || '').toUpperCase();
  if (acc) byAccount[acc] = (byAccount[acc] ?? 0) + val;
  // after loop:
  byAccount['TOTAL'] = total;
  return { total, byKey, byAccount };
  ```

  Update singleton export to match positionsDayPnlStore shape:
  ```javascript
  export const holdingsDayPnlStore = {
    get total()     { return _pulseTotal ?? _store.total; },
    get byKey()     { return _pulseByKey ?? _store.byKey; },
    get byAccount() { return _store.byAccount; }, // always all-accounts from _store
    setFromPulse(byKey, total) {
      _pulseByKey = byKey;
      _pulseTotal = total;
    },
  };
  ```
  Note: `byAccount` always reads `_store.byAccount` (not pulse-overridden) so the summary
  grid always has per-account breakdown for all accounts regardless of Pulse filter.

  **Change 3 — pulseUnified.js:526:**
  ```javascript
  const heldQty = Number(r.quantity) || 0;
  // opening_quantity is a reference field — CLAUDE.md prohibits use in P&L
  ```

  **Vitest — pulseRowsAndFlash.test.js lines 209-224:**
  Flip `qty_hold=10` → `qty_hold=0` for fully-sold row (change 3 regression test).

  **Vitest — holdingsDayPnlStore.test.js:**
  Add tests:
  - `byAccount["ZG0790"]` = sum of ZG0790 rows' day P&L
  - `byAccount["TOTAL"]` = `total` (from _store, all accounts)
  - Multi-account same symbol: `byKey["SYM"]` before setFromPulse = combined; after
    `setFromPulse({SYM: 100}, 100)`, `byKey["SYM"]` = 100 (pulse overrides)
  - `byAccount` is NOT overridden by setFromPulse (always reflects _store)

- backend-test:
  **New file: `backend/tests/broker/test_book_crosscheck.py`**
  Follow `test_holdings_day_pnl_crosscheck.py` exactly:
  same DEV_BASE/DEV_USER/DEV_PASS, same `_login()` / `_dev_reachable()` helpers,
  same pytest skip guard (`@pytest.mark.skipif` or fixture skip), same `__main__` entry.

  **Class TestHoldingsCrossCheck:**
  - `test_row_vs_summary_per_account`: sum of row-level `day_change_val` per account must
    equal that account's summary row `day_change_val` (tolerance: Rs5 per account)
  - `test_summary_total_equals_sum_of_accounts`: summary TOTAL row `day_change_val` ==
    sum of per-account summary rows (exact, no floating-point tolerance > Rs1)
  - `test_formula_vs_broker_day_change`: `(last_price - previous_close) × quantity` per
    symbol vs row `day_change_val` (tolerance: 5% aggregate per account, Rs5 per symbol)

  **Class TestPositionsCrossCheck** (`GET /api/positions`):
  - `test_row_vs_summary_per_account`: sum of row-level `day_change_val` per account must
    equal that account's summary `day_change_val` (tolerance: Rs10 — overnight decomposition)
  - `test_summary_total_equals_sum_of_accounts`: TOTAL row == sum of per-account rows (Rs1)
  - `test_pnl_sign_check`: when `last_price > average_price` and `quantity > 0`, `pnl` > -Rs100
    (allows for rounding, brokerage; catches systematic sign errors)

  **Class TestFundsCrossCheck** (`GET /api/funds`):
  - `test_cash_nonnegative`: `available_cash` >= -Rs100 for all accounts (small negative ok for
    rounding, large negative is a bug)
  - `test_margin_consistency`: `used_margin` + `avail_margin` approx equals `total_margin`
    within Rs100 per account
  - `test_used_margin_nonnegative`: `used_margin` >= 0 for all accounts

  Shared `@pytest.fixture(scope='module')` fetching data once. All classes skip when unreachable.

- broker: skip
- doc: skip
- playwright: skip

## Tests
- pytest: yes (test_book_crosscheck.py skips when dev.ramboq.com unreachable)
- svelte-check: yes
- playwright: no

## Commit message
fix(pulse): apply positions pattern to holdings day P&L + broker book cross-check suite

## Done when
- Holdings byKey SSOT override removed from buildUnified
- `holdingsDayPnlStore` has `setFromPulse` + `byAccount` (all-accounts from _store)
- NavStrip H reflects Pulse filter state (same as NavStrip P)
- ZG0790 alone + ZJ6294 alone each show their own account's value (no multi-account inflation)
- Pulse summary `day_pnl` reads from `holdingsDayPnlStore.byAccount`
- `pulseUnified.js:526` uses `Number(r.quantity) || 0` only
- `test_book_crosscheck.py` covers holdings + positions + funds cross-checks
- `npx vitest run` passes with 0 failures
- svelte-check 0 errors
