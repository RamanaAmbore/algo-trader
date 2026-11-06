/**
 * columnSort.test.js — Vitest unit tests for the shared column-sort SSOT
 * (`columnSort.js`), extracted for the derivatives Legs/Candidates grid
 * but intended for reuse by any future hand-rolled grid.
 *
 * Five quality dimensions:
 *  1. SSOT  — `cycleSortState` reads `SORT_CYCLE` from algoGridUtils.js,
 *             the same array every ag-Grid instance's `sortingOrder` uses;
 *             asserted directly, not re-derived.
 *  2. Perf  — `applyColumnSort` must never evaluate an accessor when
 *             `direction == null` — asserted via a spy that throws.
 *  3. Stale — null-handling mirrors ag-Grid's own nulls-as-minimum rule,
 *             not a naive "nulls always last"; ties are stable.
 *  4. Reuse — exercises the real exported functions only.
 *  5. UX    — a different column always restarts at 'asc', matching the
 *             ag-Grid header-click UX the plan mirrors.
 */

import { describe, it, expect } from 'vitest';
import { SORT_CYCLE } from '$lib/data/algoGridUtils.js';
import { cycleSortState, applyColumnSort } from '$lib/data/columnSort.js';

/**
 * Typed helper so `svelte-check` doesn't widen `type: 'text'|'number'`
 * literals to a bare `string` when building a columnSpecs object inline.
 * @param {'text'|'number'} type
 * @param {(row: any) => any} accessor
 * @returns {import('$lib/data/columnSort.js').ColumnSpec}
 */
function spec(type, accessor) {
  return { type, accessor };
}

describe('cycleSortState', () => {
  it('starts a fresh column at asc (SORT_CYCLE[0])', () => {
    const next = cycleSortState({ column: null, direction: null }, 'ltp');
    expect(next).toEqual({ column: 'ltp', direction: SORT_CYCLE[0] });
    expect(next.direction).toBe('asc');
  });

  it('cycles asc → desc → none on the SAME column', () => {
    let state = { column: null, direction: null };
    state = cycleSortState(state, 'pnl');
    expect(state).toEqual({ column: 'pnl', direction: 'asc' });
    state = cycleSortState(state, 'pnl');
    expect(state).toEqual({ column: 'pnl', direction: 'desc' });
    state = cycleSortState(state, 'pnl');
    expect(state).toEqual({ column: null, direction: null });
  });

  it('reaching the terminal null clears `column` too (never a stale column + null direction)', () => {
    const terminal = cycleSortState({ column: 'pnl', direction: 'desc' }, 'pnl');
    expect(terminal.column).toBeNull();
    expect(terminal.direction).toBeNull();
  });

  it('a DIFFERENT column always resets to asc — never resumes mid-cycle', () => {
    // 'pnl' is mid-cycle at desc; clicking a different column ('ltp')
    // must start that column fresh at asc, not continue pnl's cycle.
    /** @type {import('$lib/data/columnSort.js').SortState} */
    const midCycle = { column: 'pnl', direction: 'desc' };
    const switched = cycleSortState(midCycle, 'ltp');
    expect(switched).toEqual({ column: 'ltp', direction: 'asc' });
  });

  it('tolerates a null/undefined current state (defaults to inert)', () => {
    expect(cycleSortState(null, 'ltp')).toEqual({ column: 'ltp', direction: 'asc' });
    expect(cycleSortState(undefined, 'ltp')).toEqual({ column: 'ltp', direction: 'asc' });
  });
});

describe('applyColumnSort — direction == null short-circuit', () => {
  it('returns the SAME array reference and never evaluates the accessor', () => {
    const rows = [{ v: 3 }, { v: 1 }, { v: 2 }];
    const throwingAccessor = () => { throw new Error('accessor must not be called when direction is null'); };
    const specs = { v: spec('number', throwingAccessor) };

    const out = applyColumnSort(rows, { column: 'v', direction: null }, specs);
    expect(out).toBe(rows); // same reference, not just deep-equal
  });

  it('also short-circuits when sortState/column is missing entirely', () => {
    const rows = [{ v: 3 }, { v: 1 }];
    const throwingAccessor = () => { throw new Error('must not be called'); };
    const specs = { v: spec('number', throwingAccessor) };

    expect(applyColumnSort(rows, null, specs)).toBe(rows);
    expect(applyColumnSort(rows, { column: null, direction: null }, specs)).toBe(rows);
    expect(applyColumnSort(rows, { column: 'missing', direction: 'asc' }, {})).toBe(rows);
  });
});

describe('applyColumnSort — numeric sort + null handling (ag-Grid nulls-as-minimum parity)', () => {
  const specs = { v: spec('number', (r) => r.v) };

  it('ascending: nulls sort FIRST (minimum), not last', () => {
    const rows = [{ v: 5 }, { v: null }, { v: 1 }, { v: 3 }];
    const out = applyColumnSort(rows, { column: 'v', direction: 'asc' }, specs);
    expect(out.map((r) => r.v)).toEqual([null, 1, 3, 5]);
  });

  it('descending: nulls sort LAST', () => {
    const rows = [{ v: 5 }, { v: null }, { v: 1 }, { v: 3 }];
    const out = applyColumnSort(rows, { column: 'v', direction: 'desc' }, specs);
    expect(out.map((r) => r.v)).toEqual([5, 3, 1, null]);
  });

  it('undefined is treated the same as null', () => {
    const rows = [{ v: 2 }, { v: undefined }, { v: 1 }];
    const out = applyColumnSort(rows, { column: 'v', direction: 'asc' }, specs);
    expect(out.map((r) => r.v)).toEqual([undefined, 1, 2]);
  });

  it('a NaN numeric key is coerced to null (never breaks comparator transitivity)', () => {
    const rows = [{ v: 2 }, { v: NaN }, { v: 1 }];
    const out = applyColumnSort(rows, { column: 'v', direction: 'asc' }, specs);
    expect(out.map((r) => r.v)).toEqual([NaN, 1, 2]);
    const outDesc = applyColumnSort(rows, { column: 'v', direction: 'desc' }, specs);
    expect(outDesc.map((r) => r.v)).toEqual([2, 1, NaN]);
  });
});

describe('applyColumnSort — text sort', () => {
  const specs = { sym: spec('text', (r) => r.sym) };

  it('sorts lexicographically, ascending and descending', () => {
    const rows = [{ sym: 'NIFTY' }, { sym: 'BANKNIFTY' }, { sym: 'GOLD' }];
    expect(applyColumnSort(rows, { column: 'sym', direction: 'asc' }, specs).map((r) => r.sym))
      .toEqual(['BANKNIFTY', 'GOLD', 'NIFTY']);
    expect(applyColumnSort(rows, { column: 'sym', direction: 'desc' }, specs).map((r) => r.sym))
      .toEqual(['NIFTY', 'GOLD', 'BANKNIFTY']);
  });
});

describe('applyColumnSort — stability within ties', () => {
  const specs = { v: spec('number', (r) => r.v) };

  it('rows with equal keys preserve their ORIGINAL relative order on asc', () => {
    const rows = [{ id: 'a', v: 1 }, { id: 'b', v: 1 }, { id: 'c', v: 1 }, { id: 'd', v: 0 }];
    const out = applyColumnSort(rows, { column: 'v', direction: 'asc' }, specs);
    expect(out.map((r) => r.id)).toEqual(['d', 'a', 'b', 'c']);
  });

  it('rows with equal keys preserve their ORIGINAL relative order on desc too (tiebreak never reversed)', () => {
    const rows = [{ id: 'a', v: 1 }, { id: 'b', v: 1 }, { id: 'c', v: 1 }, { id: 'd', v: 0 }];
    const out = applyColumnSort(rows, { column: 'v', direction: 'desc' }, specs);
    expect(out.map((r) => r.id)).toEqual(['a', 'b', 'c', 'd']);
  });

  it('does not mutate the input array', () => {
    const rows = [{ v: 3 }, { v: 1 }, { v: 2 }];
    const snapshot = [...rows];
    applyColumnSort(rows, { column: 'v', direction: 'asc' }, specs);
    expect(rows).toEqual(snapshot);
  });
});
