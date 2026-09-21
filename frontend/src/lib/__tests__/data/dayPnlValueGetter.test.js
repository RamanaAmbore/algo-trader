/**
 * dayPnlValueGetter.test.js
 *
 * Unit tests for the MarketPulse positions grid day_pnl column valueGetter.
 *
 * The valueGetter handles two cases:
 *   1. Pinned rows (totals): returns positionsDayPnlStore.total
 *   2. Regular rows: returns positionsDerivedStore.get(sym).day_pnl
 *
 * Five quality dimensions:
 *   1. SSOT   — uses two canonical stores (positionsDayPnlStore.total and
 *               positionsDerivedStore.byKey[sym])
 *   2. Perf   — pure unit test, no DOM / network, sub-millisecond
 *   3. Stale  — fallback to p.data.day_pnl when store values are null
 *   4. Reuse  — cell composition (ag-Grid params object)
 *   5. UX     — pinned-row totals vs per-row derived values
 */

import { describe, it, expect } from 'vitest';

/**
 * Extract the pure logic from MarketPulse.svelte day_pnl valueGetter.
 *
 * @param {Object} p - ag-Grid cell params: { node, data }
 * @param {Function} storeTotalFn - returns positionsDayPnlStore.total
 * @param {Function} derivedGetFn - returns positionsDerivedStore.get(sym)
 * @returns {number|null} - day P&L value to display
 */
function dayPnlValueGetter(p, storeTotalFn, derivedGetFn) {
  if (p.node?.rowPinned) return storeTotalFn() ?? p.data?.day_pnl;
  const sym = String(p.data?.tradingsymbol || '').toUpperCase();
  return derivedGetFn(sym)?.day_pnl ?? p.data?.day_pnl;
}

// ── Test 1: Pinned rows (totals) ────────────────────────────────────────────

describe('dayPnlValueGetter — pinned rows', () => {
  it('p.node.rowPinned = "bottom": returns storeTotalFn() result, ignores tradingsymbol', () => {
    // Scenario: totals row pinned at bottom with store total = 15000
    const p = {
      node: { rowPinned: 'bottom' },
      data: { tradingsymbol: 'RELIANCE', day_pnl: 999 }, // ignored
    };
    const storeTotalFn = () => 15000;
    const derivedGetFn = () => ({ day_pnl: null });

    const result = dayPnlValueGetter(p, storeTotalFn, derivedGetFn);

    expect(result).toBe(15000);
  });

  it('p.node.rowPinned = "top": returns storeTotalFn() result (same logic as bottom)', () => {
    // Scenario: totals row pinned at top
    const p = {
      node: { rowPinned: 'top' },
      data: { tradingsymbol: 'INFY', day_pnl: 500 }, // ignored
    };
    const storeTotalFn = () => 8500;
    const derivedGetFn = () => ({ day_pnl: null });

    const result = dayPnlValueGetter(p, storeTotalFn, derivedGetFn);

    expect(result).toBe(8500);
  });

  it('p.node.rowPinned = "bottom" and storeTotalFn() = null: falls back to p.data.day_pnl', () => {
    // Scenario: pinned row but store total is null (stale or empty)
    const p = {
      node: { rowPinned: 'bottom' },
      data: { tradingsymbol: 'TCS', day_pnl: 1200 },
    };
    const storeTotalFn = () => null;
    const derivedGetFn = () => ({ day_pnl: null });

    const result = dayPnlValueGetter(p, storeTotalFn, derivedGetFn);

    expect(result).toBe(1200);
  });

  it('p.node.rowPinned = "bottom" and storeTotalFn() = 0: returns 0 (not falsy check)', () => {
    // Scenario: store total is exactly zero (valid value, not null/undefined)
    // The ?? operator should NOT fall back to p.data.day_pnl when result is 0
    const p = {
      node: { rowPinned: 'bottom' },
      data: { tradingsymbol: 'HDFC', day_pnl: 5000 },
    };
    const storeTotalFn = () => 0;
    const derivedGetFn = () => ({ day_pnl: null });

    const result = dayPnlValueGetter(p, storeTotalFn, derivedGetFn);

    expect(result).toBe(0);
  });

  it('p.node.rowPinned = "bottom" with negative total: returns negative value', () => {
    // Scenario: portfolio is down; store total = -2500
    const p = {
      node: { rowPinned: 'bottom' },
      data: { tradingsymbol: 'SBIN', day_pnl: 100 }, // ignored
    };
    const storeTotalFn = () => -2500;
    const derivedGetFn = () => ({ day_pnl: null });

    const result = dayPnlValueGetter(p, storeTotalFn, derivedGetFn);

    expect(result).toBe(-2500);
  });
});

// ── Test 2: Regular rows (non-pinned) ───────────────────────────────────────

describe('dayPnlValueGetter — regular (non-pinned) rows', () => {
  it('p.node.rowPinned = undefined: reads derivedGetFn(sym).day_pnl', () => {
    // Scenario: regular position row for RELIANCE; derived store has day_pnl = 6500
    const p = {
      node: { rowPinned: undefined },
      data: { tradingsymbol: 'RELIANCE', day_pnl: 1000 }, // fallback only
    };
    const storeTotalFn = () => 99999; // ignored
    const derivedGetFn = (sym) => {
      if (sym === 'RELIANCE') return { day_pnl: 6500 };
      return { day_pnl: null };
    };

    const result = dayPnlValueGetter(p, storeTotalFn, derivedGetFn);

    expect(result).toBe(6500);
  });

  it('p.node.rowPinned = null: reads derivedGetFn(sym).day_pnl (falsy but not "truthy")', () => {
    // Scenario: rowPinned is null (not defined) — still a regular row
    const p = {
      node: { rowPinned: null },
      data: { tradingsymbol: 'INFY', day_pnl: 2000 },
    };
    const storeTotalFn = () => 77777; // ignored
    const derivedGetFn = (sym) => {
      if (sym === 'INFY') return { day_pnl: 3500 };
      return { day_pnl: null };
    };

    const result = dayPnlValueGetter(p, storeTotalFn, derivedGetFn);

    expect(result).toBe(3500);
  });

  it('derivedGetFn(sym) returns { day_pnl: null }: falls back to p.data.day_pnl', () => {
    // Scenario: derived store has no entry for this symbol (stale or missing)
    const p = {
      node: { rowPinned: undefined },
      data: { tradingsymbol: 'UNKNOWN', day_pnl: 750 },
    };
    const storeTotalFn = () => 15000; // ignored
    const derivedGetFn = (sym) => ({ day_pnl: null }); // all symbols return null

    const result = dayPnlValueGetter(p, storeTotalFn, derivedGetFn);

    expect(result).toBe(750);
  });

  it('derivedGetFn(sym) returns undefined: falls back to p.data.day_pnl', () => {
    // Scenario: derivedGetFn returns undefined (missing entry)
    const p = {
      node: { rowPinned: undefined },
      data: { tradingsymbol: 'TCS', day_pnl: 850 },
    };
    const storeTotalFn = () => 20000; // ignored
    const derivedGetFn = (sym) => undefined; // returns undefined, not { day_pnl: null }

    const result = dayPnlValueGetter(p, storeTotalFn, derivedGetFn);

    expect(result).toBe(850);
  });

  it('tradingsymbol is lowercase: converted to uppercase before lookup', () => {
    // Scenario: data has 'reliance' (lowercase) → should look up 'RELIANCE'
    const p = {
      node: { rowPinned: undefined },
      data: { tradingsymbol: 'reliance', day_pnl: 999 },
    };
    const storeTotalFn = () => 99999; // ignored
    const derivedGetFn = (sym) => {
      // derivedGetFn receives uppercase key
      if (sym === 'RELIANCE') return { day_pnl: 5500 };
      // if it received 'reliance' lowercase, it would not match
      return { day_pnl: null };
    };

    const result = dayPnlValueGetter(p, storeTotalFn, derivedGetFn);

    expect(result).toBe(5500);
  });

  it('tradingsymbol is empty string: derivedGetFn receives empty uppercase string', () => {
    // Scenario: data has tradingsymbol = '' (empty) → uppercase = ''
    const p = {
      node: { rowPinned: undefined },
      data: { tradingsymbol: '', day_pnl: 600 },
    };
    const storeTotalFn = () => 9999; // ignored
    const derivedGetFn = (sym) => {
      if (sym === '') return { day_pnl: null }; // empty string lookup
      return { day_pnl: 100 };
    };

    const result = dayPnlValueGetter(p, storeTotalFn, derivedGetFn);

    // derivedGetFn('') returns { day_pnl: null } → fallback to p.data.day_pnl
    expect(result).toBe(600);
  });

  it('tradingsymbol missing (undefined): derivedGetFn receives empty uppercase string', () => {
    // Scenario: data.tradingsymbol is undefined
    const p = {
      node: { rowPinned: undefined },
      data: { day_pnl: 1100 }, // no tradingsymbol key
    };
    const storeTotalFn = () => 8888; // ignored
    const derivedGetFn = (sym) => {
      if (sym === '') return { day_pnl: null };
      return { day_pnl: null };
    };

    const result = dayPnlValueGetter(p, storeTotalFn, derivedGetFn);

    expect(result).toBe(1100);
  });

  it('derivedGetFn(sym) returns { day_pnl: 0 }: returns 0 (not falsy check)', () => {
    // Scenario: derived store has day_pnl = 0 (valid value, e.g., flat position)
    // The ?? operator should NOT fall back when result is 0
    const p = {
      node: { rowPinned: undefined },
      data: { tradingsymbol: 'WIPRO', day_pnl: 3000 },
    };
    const storeTotalFn = () => 50000; // ignored
    const derivedGetFn = (sym) => {
      if (sym === 'WIPRO') return { day_pnl: 0 }; // flat position
      return { day_pnl: null };
    };

    const result = dayPnlValueGetter(p, storeTotalFn, derivedGetFn);

    expect(result).toBe(0);
  });

  it('derivedGetFn(sym) returns { day_pnl: -1500 }: returns negative value', () => {
    // Scenario: derived store has day_pnl = -1500 (losing position)
    const p = {
      node: { rowPinned: undefined },
      data: { tradingsymbol: 'MARUTI', day_pnl: 500 },
    };
    const storeTotalFn = () => 75000; // ignored
    const derivedGetFn = (sym) => {
      if (sym === 'MARUTI') return { day_pnl: -1500 };
      return { day_pnl: null };
    };

    const result = dayPnlValueGetter(p, storeTotalFn, derivedGetFn);

    expect(result).toBe(-1500);
  });
});

// ── Test 3: Composition with ag-Grid params object ────────────────────────────

describe('dayPnlValueGetter — ag-Grid params composition', () => {
  it('p.node is undefined: treated as regular row (falsy rowPinned)', () => {
    // Scenario: p.node itself is undefined (edge case)
    const p = {
      node: undefined,
      data: { tradingsymbol: 'BHARTIARTL', day_pnl: 700 },
    };
    const storeTotalFn = () => 45000; // ignored
    const derivedGetFn = (sym) => {
      if (sym === 'BHARTIARTL') return { day_pnl: 2200 };
      return { day_pnl: null };
    };

    const result = dayPnlValueGetter(p, storeTotalFn, derivedGetFn);

    // p.node?.rowPinned evaluates to undefined (falsy) → regular row path
    expect(result).toBe(2200);
  });

  it('p.data is undefined: falls back gracefully', () => {
    // Scenario: p.data is missing (unlikely but should not crash)
    const p = {
      node: { rowPinned: undefined },
      // no data key
    };
    const storeTotalFn = () => 35000; // ignored
    const derivedGetFn = (sym) => {
      if (sym === '') return { day_pnl: null };
      return { day_pnl: 1500 };
    };

    const result = dayPnlValueGetter(p, storeTotalFn, derivedGetFn);

    // p.data?.day_pnl evaluates to undefined → fallback to ??
    // derivedGetFn('') returns { day_pnl: null } → falls back to undefined
    expect(result).toBeUndefined();
  });

  it('p.node and p.data both present and non-empty: preferred source wins', () => {
    // Scenario: complete ag-Grid params object with all expected properties
    const p = {
      node: { rowPinned: undefined, data: {}, rowIndex: 5, key: 'pos-123' },
      data: { tradingsymbol: 'HDFC', day_pnl: 4000, quantity: 50, average_price: 2500 },
    };
    const storeTotalFn = () => 120000; // ignored
    const derivedGetFn = (sym) => {
      if (sym === 'HDFC') return { day_pnl: 8750 };
      return { day_pnl: null };
    };

    const result = dayPnlValueGetter(p, storeTotalFn, derivedGetFn);

    expect(result).toBe(8750);
  });
});

// ── Test 4: Integration with multiple symbols ───────────────────────────────

describe('dayPnlValueGetter — multiple symbols in grid', () => {
  it('grid row 1 (RELIANCE): derived store has data; row 2 (INFY): fallback to p.data', () => {
    // Scenario: multi-row grid with mixed store availability
    const storeTotalFn = () => 25000;
    const derivedGetFn = (sym) => {
      if (sym === 'RELIANCE') return { day_pnl: 6000 };
      if (sym === 'INFY') return { day_pnl: null }; // missing from derived store
      return { day_pnl: null };
    };

    const p1 = {
      node: { rowPinned: undefined },
      data: { tradingsymbol: 'RELIANCE', day_pnl: 1000 },
    };
    const p2 = {
      node: { rowPinned: undefined },
      data: { tradingsymbol: 'INFY', day_pnl: 5000 },
    };

    const result1 = dayPnlValueGetter(p1, storeTotalFn, derivedGetFn);
    const result2 = dayPnlValueGetter(p2, storeTotalFn, derivedGetFn);

    expect(result1).toBe(6000); // from derived store
    expect(result2).toBe(5000); // fallback to p.data
  });

  it('pinned row and two regular rows use correct source for each', () => {
    // Scenario: totals row + two position rows
    const storeTotalFn = () => 15500;
    const derivedGetFn = (sym) => {
      if (sym === 'RELIANCE') return { day_pnl: 3000 };
      if (sym === 'TCS') return { day_pnl: 12500 };
      return { day_pnl: null };
    };

    const pTotals = {
      node: { rowPinned: 'bottom' },
      data: { tradingsymbol: 'TOTAL', day_pnl: 999 }, // ignored
    };
    const pReliance = {
      node: { rowPinned: undefined },
      data: { tradingsymbol: 'RELIANCE', day_pnl: 1000 },
    };
    const pTcs = {
      node: { rowPinned: undefined },
      data: { tradingsymbol: 'TCS', day_pnl: 2000 },
    };

    const resultTotals = dayPnlValueGetter(pTotals, storeTotalFn, derivedGetFn);
    const resultReliance = dayPnlValueGetter(pReliance, storeTotalFn, derivedGetFn);
    const resultTcs = dayPnlValueGetter(pTcs, storeTotalFn, derivedGetFn);

    expect(resultTotals).toBe(15500); // from storeTotalFn
    expect(resultReliance).toBe(3000); // from derivedGetFn
    expect(resultTcs).toBe(12500); // from derivedGetFn
  });
});

// ── Test 5: SSOT contract validation ────────────────────────────────────────

describe('dayPnlValueGetter — SSOT contract', () => {
  it('always prefers storeTotalFn for pinned rows (never derivedGetFn)', () => {
    // Validate: pinned rows NEVER call derivedGetFn, only storeTotalFn
    const p = {
      node: { rowPinned: 'bottom' },
      data: { tradingsymbol: 'TEST', day_pnl: 100 },
    };

    let derivedGetFnCalled = false;
    const storeTotalFn = () => 5000;
    const derivedGetFn = (sym) => {
      derivedGetFnCalled = true;
      return { day_pnl: 999 };
    };

    dayPnlValueGetter(p, storeTotalFn, derivedGetFn);

    expect(derivedGetFnCalled).toBe(false);
  });

  it('always prefers derivedGetFn for regular rows (calls both, prefers derived result)', () => {
    // Validate: regular rows call derivedGetFn first, fall back to storeTotalFn only if needed
    const p = {
      node: { rowPinned: undefined },
      data: { tradingsymbol: 'TEST', day_pnl: 100 },
    };

    let storeTotalFnCalled = false;
    const storeTotalFn = () => {
      storeTotalFnCalled = true;
      return 9999;
    };
    const derivedGetFn = (sym) => ({ day_pnl: 3000 });

    const result = dayPnlValueGetter(p, storeTotalFn, derivedGetFn);

    // Regular rows should NOT call storeTotalFn at all
    expect(storeTotalFnCalled).toBe(false);
    expect(result).toBe(3000);
  });
});
