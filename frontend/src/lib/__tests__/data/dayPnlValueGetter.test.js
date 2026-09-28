/**
 * dayPnlValueGetter.test.js
 *
 * Unit tests for the MarketPulse positions and holdings grid day_pnl column valueGetter.
 *
 * The valueGetter handles two cases:
 *   1. Pinned positions rows (totals): returns storeTotalFn() (the account-filtered total)
 *   2. Pinned holdings rows (totals): returns p.data.day_pnl (NOT store total)
 *   3. Regular rows (positions OR holdings): returns p.data.day_pnl directly
 *
 * 2026-09-27 audit fix: regular rows previously preferred
 * positionsDerivedStore.get(sym).day_pnl — a per-SYMBOL, firm-wide,
 * account-filter-unaware aggregate — over the row's own already-correctly-
 * filtered day_pnl. A symbol held in 2+ accounts showed the SAME combined
 * value on every row for that symbol regardless of the operator's account
 * filter. p.data.day_pnl (set via baseDayPnlForPosition from the already
 * account-filtered row set — see pulseUnified.js) is the sole SSOT value;
 * no store lookup is needed or performed for regular rows any more.
 *
 * Five quality dimensions:
 *   1. SSOT   — pinned positions rows use the account-filtered store total;
 *               every other row (pinned holdings, regular positions,
 *               regular holdings) reads p.data.day_pnl directly, no store call
 *   2. Perf   — pure unit test, no DOM / network, sub-millisecond
 *   3. Stale  — pinned positions rows fall back to p.data.day_pnl when the
 *               store total is null
 *   4. Reuse  — cell composition (ag-Grid params object)
 *   5. UX     — pinned-row totals vs per-row values; holdings pinned rows
 *               and ALL regular rows use raw row data
 */

import { describe, it, expect } from 'vitest';

/**
 * Extract the pure logic from MarketPulse.svelte day_pnl valueGetter.
 *
 * @param {Object} p - ag-Grid cell params: { node, data }
 * @param {Function} storeTotalFn - returns positionsDayPnlStore.filteredTotal(positionsAccounts)
 * @returns {number|null} - day P&L value to display
 */
function dayPnlValueGetter(p, storeTotalFn) {
  if (p.node?.rowPinned && p.data?._majorGroup === 'positions')
    return storeTotalFn() ?? p.data?.day_pnl;
  if (p.node?.rowPinned) return p.data?.day_pnl;
  return p.data?.day_pnl;
}

// ── Test 1: Pinned rows (totals) ────────────────────────────────────────────

describe('dayPnlValueGetter — pinned rows', () => {
  it('p.node.rowPinned = "bottom" and _majorGroup = "positions": returns storeTotalFn() result, ignores tradingsymbol', () => {
    // Scenario: totals row pinned at bottom with (account-filtered) store total = 15000
    const p = {
      node: { rowPinned: 'bottom' },
      data: { _majorGroup: 'positions', tradingsymbol: 'RELIANCE', day_pnl: 999 }, // ignored
    };
    const storeTotalFn = () => 15000;

    const result = dayPnlValueGetter(p, storeTotalFn);

    expect(result).toBe(15000);
  });

  it('p.node.rowPinned = "top" and _majorGroup = "positions": returns storeTotalFn() result (same logic as bottom)', () => {
    const p = {
      node: { rowPinned: 'top' },
      data: { _majorGroup: 'positions', tradingsymbol: 'INFY', day_pnl: 500 }, // ignored
    };
    const storeTotalFn = () => 8500;

    const result = dayPnlValueGetter(p, storeTotalFn);

    expect(result).toBe(8500);
  });

  it('p.node.rowPinned = "bottom", _majorGroup = "positions", and storeTotalFn() = null: falls back to p.data.day_pnl', () => {
    const p = {
      node: { rowPinned: 'bottom' },
      data: { _majorGroup: 'positions', tradingsymbol: 'TCS', day_pnl: 1200 },
    };
    const storeTotalFn = () => null;

    const result = dayPnlValueGetter(p, storeTotalFn);

    expect(result).toBe(1200);
  });

  it('p.node.rowPinned = "bottom", _majorGroup = "positions", and storeTotalFn() = 0: returns 0 (not falsy check)', () => {
    const p = {
      node: { rowPinned: 'bottom' },
      data: { _majorGroup: 'positions', tradingsymbol: 'HDFC', day_pnl: 5000 },
    };
    const storeTotalFn = () => 0;

    const result = dayPnlValueGetter(p, storeTotalFn);

    expect(result).toBe(0);
  });

  it('p.node.rowPinned = "bottom", _majorGroup = "positions", with negative total: returns negative value', () => {
    const p = {
      node: { rowPinned: 'bottom' },
      data: { _majorGroup: 'positions', tradingsymbol: 'SBIN', day_pnl: 100 }, // ignored
    };
    const storeTotalFn = () => -2500;

    const result = dayPnlValueGetter(p, storeTotalFn);

    expect(result).toBe(-2500);
  });

  it('p.node.rowPinned = "bottom" and _majorGroup = "holdings": returns p.data.day_pnl (NOT storeTotalFn)', () => {
    const p = {
      node: { rowPinned: 'bottom' },
      data: { _majorGroup: 'holdings', tradingsymbol: 'RELIANCE', day_pnl: 3500 },
    };
    const storeTotalFn = () => 15000; // positions store total (MUST BE IGNORED for holdings)

    const result = dayPnlValueGetter(p, storeTotalFn);

    expect(result).toBe(3500);
  });

  it('p.node.rowPinned = "top" and _majorGroup = "holdings": returns p.data.day_pnl (top pin)', () => {
    const p = {
      node: { rowPinned: 'top' },
      data: { _majorGroup: 'holdings', tradingsymbol: 'INFY', day_pnl: 5200 },
    };
    const storeTotalFn = () => 20000; // IGNORED for holdings

    const result = dayPnlValueGetter(p, storeTotalFn);

    expect(result).toBe(5200);
  });

  it('p.node.rowPinned = "bottom" with no _majorGroup: returns p.data.day_pnl (not storeTotalFn)', () => {
    const p = {
      node: { rowPinned: 'bottom' },
      data: { tradingsymbol: 'TCS', day_pnl: 2100 }, // no _majorGroup key
    };
    const storeTotalFn = () => 18000; // IGNORED because _majorGroup is not 'positions'

    const result = dayPnlValueGetter(p, storeTotalFn);

    expect(result).toBe(2100);
  });

  it('p.node.rowPinned = "bottom", _majorGroup = "holdings", storeTotalFn() = 0: returns p.data.day_pnl', () => {
    const p = {
      node: { rowPinned: 'bottom' },
      data: { _majorGroup: 'holdings', tradingsymbol: 'HDFC', day_pnl: 4700 },
    };
    const storeTotalFn = () => 0; // positions total is zero (IGNORED for holdings)

    const result = dayPnlValueGetter(p, storeTotalFn);

    expect(result).toBe(4700);
  });
});

// ── Test 2: Regular rows (non-pinned) — read p.data.day_pnl directly ───────

describe('dayPnlValueGetter — regular (non-pinned) rows', () => {
  it('p.node.rowPinned = undefined: reads p.data.day_pnl directly, ignores any store', () => {
    const p = {
      node: { rowPinned: undefined },
      data: { tradingsymbol: 'RELIANCE', day_pnl: 6500 },
    };
    const storeTotalFn = () => 99999; // ignored — regular rows never call this

    const result = dayPnlValueGetter(p, storeTotalFn);

    expect(result).toBe(6500);
  });

  it('p.node.rowPinned = null: reads p.data.day_pnl (falsy rowPinned but not "truthy")', () => {
    const p = {
      node: { rowPinned: null },
      data: { tradingsymbol: 'INFY', day_pnl: 3500 },
    };
    const storeTotalFn = () => 77777; // ignored

    const result = dayPnlValueGetter(p, storeTotalFn);

    expect(result).toBe(3500);
  });

  it('p.data.day_pnl is null: returns null (no store fallback for regular rows)', () => {
    const p = {
      node: { rowPinned: undefined },
      data: { tradingsymbol: 'UNKNOWN', day_pnl: null },
    };
    const storeTotalFn = () => 15000; // ignored

    const result = dayPnlValueGetter(p, storeTotalFn);

    expect(result).toBeNull();
  });

  it('p.data.day_pnl = 0: returns 0 (valid flat-position value)', () => {
    const p = {
      node: { rowPinned: undefined },
      data: { tradingsymbol: 'WIPRO', day_pnl: 0 },
    };
    const storeTotalFn = () => 50000; // ignored

    const result = dayPnlValueGetter(p, storeTotalFn);

    expect(result).toBe(0);
  });

  it('p.data.day_pnl is negative: returns negative value', () => {
    const p = {
      node: { rowPinned: undefined },
      data: { tradingsymbol: 'MARUTI', day_pnl: -1500 },
    };
    const storeTotalFn = () => 75000; // ignored

    const result = dayPnlValueGetter(p, storeTotalFn);

    expect(result).toBe(-1500);
  });

  it('same symbol held in two accounts, filtered to one: row shows only that account\'s day_pnl (2026-09-27 audit scenario)', () => {
    // Scenario: NIFTY held in account A (day_pnl 3000) and account B (day_pnl
    // 9000, NOT in the current filter). p.data.day_pnl for this row is
    // already scoped to A only (built from scopedPositions upstream) — the
    // valueGetter must return exactly that, never a cross-account 12000.
    const p = {
      node: { rowPinned: undefined },
      data: { tradingsymbol: 'NIFTY25SEP25000CE', account: 'A', day_pnl: 3000 },
    };
    const storeTotalFn = () => 12000; // firm-wide total — must be ignored here

    const result = dayPnlValueGetter(p, storeTotalFn);

    expect(result).toBe(3000);
  });
});

// ── Test 3: Composition with ag-Grid params object ────────────────────────────

describe('dayPnlValueGetter — ag-Grid params composition', () => {
  it('p.node is undefined: treated as regular row (falsy rowPinned)', () => {
    const p = {
      node: undefined,
      data: { tradingsymbol: 'BHARTIARTL', day_pnl: 2200 },
    };
    const storeTotalFn = () => 45000; // ignored

    const result = dayPnlValueGetter(p, storeTotalFn);

    // p.node?.rowPinned evaluates to undefined (falsy) → regular row path
    expect(result).toBe(2200);
  });

  it('p.data is undefined: falls back gracefully', () => {
    const p = {
      node: { rowPinned: undefined },
      // no data key
    };
    const storeTotalFn = () => 35000; // ignored

    const result = dayPnlValueGetter(p, storeTotalFn);

    // p.data?.day_pnl evaluates to undefined
    expect(result).toBeUndefined();
  });

  it('p.node and p.data both present and non-empty: p.data.day_pnl wins', () => {
    const p = {
      node: { rowPinned: undefined, data: {}, rowIndex: 5, key: 'pos-123' },
      data: { tradingsymbol: 'HDFC', day_pnl: 8750, quantity: 50, average_price: 2500 },
    };
    const storeTotalFn = () => 120000; // ignored

    const result = dayPnlValueGetter(p, storeTotalFn);

    expect(result).toBe(8750);
  });
});

// ── Test 4: Integration with multiple symbols ───────────────────────────────

describe('dayPnlValueGetter — multiple symbols in grid', () => {
  it('every regular row reads its own p.data.day_pnl independently', () => {
    const storeTotalFn = () => 25000;

    const p1 = {
      node: { rowPinned: undefined },
      data: { tradingsymbol: 'RELIANCE', day_pnl: 6000 },
    };
    const p2 = {
      node: { rowPinned: undefined },
      data: { tradingsymbol: 'INFY', day_pnl: 5000 },
    };

    const result1 = dayPnlValueGetter(p1, storeTotalFn);
    const result2 = dayPnlValueGetter(p2, storeTotalFn);

    expect(result1).toBe(6000);
    expect(result2).toBe(5000);
  });

  it('pinned row and two regular rows use correct source for each', () => {
    const storeTotalFn = () => 15500;

    const pTotals = {
      node: { rowPinned: 'bottom' },
      data: { _majorGroup: 'positions', tradingsymbol: 'TOTAL', day_pnl: 999 }, // ignored
    };
    const pReliance = {
      node: { rowPinned: undefined },
      data: { tradingsymbol: 'RELIANCE', day_pnl: 3000 },
    };
    const pTcs = {
      node: { rowPinned: undefined },
      data: { tradingsymbol: 'TCS', day_pnl: 12500 },
    };

    const resultTotals = dayPnlValueGetter(pTotals, storeTotalFn);
    const resultReliance = dayPnlValueGetter(pReliance, storeTotalFn);
    const resultTcs = dayPnlValueGetter(pTcs, storeTotalFn);

    expect(resultTotals).toBe(15500); // from storeTotalFn (positions pinned)
    expect(resultReliance).toBe(3000); // from p.data.day_pnl (regular row)
    expect(resultTcs).toBe(12500); // from p.data.day_pnl (regular row)
  });
});

// ── Test 5: SSOT contract validation ────────────────────────────────────────

describe('dayPnlValueGetter — SSOT contract', () => {
  it('pinned positions rows call storeTotalFn', () => {
    const p = {
      node: { rowPinned: 'bottom' },
      data: { _majorGroup: 'positions', tradingsymbol: 'TEST', day_pnl: 100 },
    };

    let storeTotalFnCalled = false;
    const storeTotalFn = () => {
      storeTotalFnCalled = true;
      return 5000;
    };

    dayPnlValueGetter(p, storeTotalFn);

    expect(storeTotalFnCalled).toBe(true);
  });

  it('pinned holdings rows never call storeTotalFn when _majorGroup = "holdings"', () => {
    const p = {
      node: { rowPinned: 'bottom' },
      data: { _majorGroup: 'holdings', tradingsymbol: 'TEST', day_pnl: 2500 },
    };

    let storeTotalFnCalled = false;
    const storeTotalFn = () => {
      storeTotalFnCalled = true;
      return 5000;
    };

    dayPnlValueGetter(p, storeTotalFn);

    expect(storeTotalFnCalled).toBe(false);
  });

  it('regular rows never call storeTotalFn — read p.data.day_pnl only', () => {
    const p = {
      node: { rowPinned: undefined },
      data: { tradingsymbol: 'TEST', day_pnl: 3000 },
    };

    let storeTotalFnCalled = false;
    const storeTotalFn = () => {
      storeTotalFnCalled = true;
      return 9999;
    };

    const result = dayPnlValueGetter(p, storeTotalFn);

    expect(storeTotalFnCalled).toBe(false);
    expect(result).toBe(3000);
  });
});
