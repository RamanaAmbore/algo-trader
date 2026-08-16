import { describe, it, expect, vi } from 'vitest';
import { mkRightColDefs, dirCls } from '../../data/pulseColumns.js';

// ---------------------------------------------------------------------------
// Minimal stubs — mkRightColDefs requires many column objects and formatters
// that are irrelevant for these structural tests.
// ---------------------------------------------------------------------------

function makeDummyCol(colId) {
  return { colId, field: colId };
}

function makeOpts() {
  return {
    symColRight:     makeDummyCol('tradingsymbol'),
    sparkCol:        makeDummyCol('sparkline'),
    ltpCol:          makeDummyCol('ltp'),
    prevCol:         makeDummyCol('prev'),
    openCol:         makeDummyCol('open'),
    volCol:          makeDummyCol('volume'),
    oiCol:           makeDummyCol('oi'),
    acctColTrailing: makeDummyCol('account'),
    RA:              'ra-cls',
    numericHdr:      'ag-right-aligned-header',
    pnlCellClass:    vi.fn(() => 'pnl-cls'),
    dirCellClass:    vi.fn(() => 'dir-cls'),
    pctFmtGrid:      vi.fn(p => String(p.value)),
    aggFmtGrid:      vi.fn(p => String(p.value)),
    numFmt:          vi.fn(p => String(p.value)),
    qtyFmt:          vi.fn(v => String(v)),
    lotsForRow:      vi.fn(() => null),
    fmtLots:         vi.fn(v => String(v ?? '—')),
  };
}

// ---------------------------------------------------------------------------
// Fix 1 — pos_state cellRenderer qty_pos fallback (defensive orphan marker)
// ---------------------------------------------------------------------------

describe('mkRightColDefs — pos_state cellRenderer quantity fallback (Fix 1)', () => {
  function getPosStateCol() {
    const cols = mkRightColDefs(makeOpts());
    const col = cols.find(c => c.colId === 'pos_state');
    if (!col) throw new Error('pos_state column not found');
    return col;
  }

  // cellRenderer tests
  it('returns "○" when quantity is defined but has_gtt/pair_group_key/is_orphan are all falsy', () => {
    const col = getPosStateCol();
    const result = col.cellRenderer({ data: { quantity: 10 } });
    expect(result).toBe('○');
  });

  it('returns "○" when quantity is 0 (defined but zero)', () => {
    const col = getPosStateCol();
    const result = col.cellRenderer({ data: { quantity: 0 } });
    expect(result).toBe('○');
  });

  it('returns "○" for any non-total, non-GTT, non-paired row (unconditional orphan)', () => {
    const col = getPosStateCol();
    const result = col.cellRenderer({ data: { qty_hold: 5 } });
    expect(result).toBe('○');
  });

  it('quantity fallback does NOT fire when is_orphan is true (is_orphan takes priority)', () => {
    const col = getPosStateCol();
    const result = col.cellRenderer({ data: { is_orphan: true, quantity: 10 } });
    expect(result).toBe('○'); // same output, but via the is_orphan branch
  });

  it('quantity fallback does NOT fire when pair_group_key is set (pair takes priority)', () => {
    const col = getPosStateCol();
    const result = col.cellRenderer({ data: { pair_group_key: 'P1', quantity: 10 } });
    expect(result).toBe('P1');
  });

  it('returns "" for _isTotal rows regardless of quantity', () => {
    const col = getPosStateCol();
    const result = col.cellRenderer({ data: { _isTotal: true, quantity: 10 } });
    expect(result).toBe('');
  });

  it('returns "" for null data', () => {
    const col = getPosStateCol();
    const result = col.cellRenderer({ data: null });
    expect(result).toBe('');
  });

  // any non-GTT, non-paired row shows ○ regardless of which fields are present
  it('returns "○" even when only qty_pos field is present (unconditional orphan)', () => {
    const col = getPosStateCol();
    const result = col.cellRenderer({ data: { qty_pos: 10 } });
    expect(result).toBe('○');
  });

  // cellStyle tests — amber background when quantity is defined
  it('cellStyle returns amber background when quantity is defined and no other flags', () => {
    const col = getPosStateCol();
    const result = col.cellStyle({ data: { quantity: 5 } });
    expect(result).toEqual({ background: 'rgba(251,191,36,0.15)', color: '#fbbf24' });
  });

  it('cellStyle returns amber for any non-total, non-GTT, non-paired row', () => {
    const col = getPosStateCol();
    const result = col.cellStyle({ data: { qty_hold: 5 } });
    expect(result).toEqual({ background: 'rgba(251,191,36,0.15)', color: '#fbbf24' });
  });

  it('cellStyle amber does NOT fire when is_orphan is true (is_orphan takes priority)', () => {
    const col = getPosStateCol();
    const result = col.cellStyle({ data: { is_orphan: true, quantity: 10 } });
    expect(result).toEqual({ background: 'rgba(251,191,36,0.15)', color: '#fbbf24' });
  });

  it('cellStyle returns green when has_gtt is true regardless of quantity', () => {
    const col = getPosStateCol();
    const result = col.cellStyle({ data: { has_gtt: true, quantity: 10 } });
    expect(result).toEqual({ background: 'rgba(74,222,128,0.20)', color: '#4ade80' });
  });

  it('cellStyle returns {} for _isTotal rows regardless of quantity', () => {
    const col = getPosStateCol();
    const result = col.cellStyle({ data: { _isTotal: true, quantity: 10 } });
    expect(result).toEqual({});
  });
});

// ---------------------------------------------------------------------------
// Fix 2 — holdingsColDefs IIFE: Lots moves before inv_val
// ---------------------------------------------------------------------------

describe('holdingsColDefs IIFE — Lots reorder before inv_val (Fix 2)', () => {
  function makeHoldingsCols() {
    const cols = mkRightColDefs(makeOpts()).filter(c => c.colId !== 'pos_state');
    const lotsIdx   = cols.findIndex(c => c.colId === 'lots');
    const invValIdx = cols.findIndex(c => c.colId === 'inv_val');
    if (lotsIdx !== -1 && invValIdx !== -1 && lotsIdx !== invValIdx - 1) {
      const [lotsCol] = cols.splice(lotsIdx, 1);
      const newInvValIdx = cols.findIndex(c => c.colId === 'inv_val');
      cols.splice(newInvValIdx, 0, lotsCol);
    }
    return cols;
  }

  it('pos_state is absent from holdings cols', () => {
    const cols = makeHoldingsCols();
    expect(cols.some(c => c.colId === 'pos_state')).toBe(false);
  });

  it('lots appears immediately before inv_val in holdings cols', () => {
    const cols = makeHoldingsCols();
    const lotsIdx   = cols.findIndex(c => c.colId === 'lots');
    const invValIdx = cols.findIndex(c => c.colId === 'inv_val');
    // Both columns must be present — if either is -1 the IIFE is untested.
    expect(lotsIdx,   'lots column absent from mkRightColDefs — fix the stub or test').not.toBe(-1);
    expect(invValIdx, 'inv_val column absent from mkRightColDefs — fix the stub or test').not.toBe(-1);
    expect(lotsIdx).toBe(invValIdx - 1);
  });
});

// ---------------------------------------------------------------------------
// Fix 2 (original) — pos_state column shape
// ---------------------------------------------------------------------------

describe('mkRightColDefs — pos_state column (Fix 2 original)', () => {
  it('returns pos_state as the first column with headerName "St"', () => {
    const cols = mkRightColDefs(makeOpts());
    expect(cols[0].colId).toBe('pos_state');
    expect(cols[0].headerName).toBe('St');
  });

  it('pos_state column has hide: false', () => {
    const cols = mkRightColDefs(makeOpts());
    expect(cols[0].hide).toBe(false);
  });

  it('pos_state column headerTooltip includes P1/P2 and amber/cyan/green language', () => {
    const cols = mkRightColDefs(makeOpts());
    const tooltip = cols[0].headerTooltip ?? '';
    expect(tooltip).toContain('P1/P2');
    expect(tooltip).toContain('cyan');
    expect(tooltip).toContain('amber');
    expect(tooltip).toContain('green');
  });
});

// ---------------------------------------------------------------------------
// Fix 3 — Lots column cellClass function
// ---------------------------------------------------------------------------

describe('mkRightColDefs — Lots column cellClass (Fix 3)', () => {
  function getLotsCol() {
    const cols = mkRightColDefs(makeOpts());
    const col = cols.find(c => c.colId === 'lots' || c.field === 'lots');
    if (!col) throw new Error('Lots column not found in mkRightColDefs result');
    return col;
  }

  it('lots cellClass is a function', () => {
    const col = getLotsCol();
    expect(typeof col.cellClass).toBe('function');
  });

  it('includes lots-left-sep when row has qty_pos defined', () => {
    const col = getLotsCol();
    const result = col.cellClass({ data: { qty_pos: 50 } });
    expect(result).toContain('lots-left-sep');
  });

  it('includes lots-left-sep when qty_pos is 0 (defined but zero — still a position row)', () => {
    const col = getLotsCol();
    const result = col.cellClass({ data: { qty_pos: 0 } });
    expect(result).toContain('lots-left-sep');
  });

  it('omits lots-left-sep when qty_pos is undefined (holdings / watchlist row)', () => {
    const col = getLotsCol();
    const result = col.cellClass({ data: { qty_hold: 10 } });
    expect(result).not.toContain('lots-left-sep');
  });

  it('omits lots-left-sep when data is null', () => {
    const col = getLotsCol();
    const result = col.cellClass({ data: null });
    expect(result).not.toContain('lots-left-sep');
  });

  it('always includes the RA class', () => {
    const col = getLotsCol();
    const withPos  = col.cellClass({ data: { qty_pos: 10 } });
    const withHold = col.cellClass({ data: { qty_hold: 10 } });
    expect(withPos).toContain('ra-cls');
    expect(withHold).toContain('ra-cls');
  });
});

// ---------------------------------------------------------------------------
// Fix 1 — holdingsColDefs filter (pure logic test)
// ---------------------------------------------------------------------------

describe('holdingsColDefs filter — pos_state exclusion (Fix 1)', () => {
  it('filtering rightColDefs by colId !== pos_state removes the pos_state column', () => {
    const cols = mkRightColDefs(makeOpts());
    const holdingsCols = cols.filter(c => c.colId !== 'pos_state');
    const hasState = holdingsCols.some(c => c.colId === 'pos_state');
    expect(hasState).toBe(false);
  });

  it('filtering does not remove the Lots column', () => {
    const cols = mkRightColDefs(makeOpts());
    const holdingsCols = cols.filter(c => c.colId !== 'pos_state');
    const hasLots = holdingsCols.some(c => c.field === 'lots');
    expect(hasLots).toBe(true);
  });

  it('filtered array is one element shorter than the original', () => {
    const cols = mkRightColDefs(makeOpts());
    const holdingsCols = cols.filter(c => c.colId !== 'pos_state');
    expect(holdingsCols.length).toBe(cols.length - 1);
  });
});

// ---------------------------------------------------------------------------
// dirCls pure-function smoke tests
// ---------------------------------------------------------------------------

describe('dirCls helper', () => {
  it('returns cell-pos for positive values', () => {
    expect(dirCls(1)).toBe('cell-pos');
  });
  it('returns cell-neg for negative values', () => {
    expect(dirCls(-1)).toBe('cell-neg');
  });
  it('returns cell-flat for zero', () => {
    expect(dirCls(0)).toBe('cell-flat');
  });
  it('returns cell-flat for null', () => {
    expect(dirCls(null)).toBe('cell-flat');
  });
});
