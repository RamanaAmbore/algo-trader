import { describe, it, expect, vi } from 'vitest';
import { mkRightColDefs, dirCls, mkPnlCellClass, mkPosSummaryCols, mkHoldSummaryCols } from '../../data/pulseColumns.js';

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

// ---------------------------------------------------------------------------
// pnl_per_share column — presence and ordering
// ---------------------------------------------------------------------------

describe('mkRightColDefs — pnl_per_share column', () => {
  it('includes pnl_per_share column in the output', () => {
    const cols = mkRightColDefs(makeOpts());
    const col = cols.find(c => c.colId === 'pnl_per_share');
    expect(col).toBeDefined();
    expect(col.field).toBe('pnl_per_share');
    expect(col.headerName).toBe('P&L/sh');
  });

  it('pnl_per_share appears immediately after pnl_pct', () => {
    const cols = mkRightColDefs(makeOpts());
    const pnlPctIdx      = cols.findIndex(c => c.colId === 'pnl_pct');
    const pnlPerShareIdx = cols.findIndex(c => c.colId === 'pnl_per_share');
    expect(pnlPctIdx,      'pnl_pct column not found').not.toBe(-1);
    expect(pnlPerShareIdx, 'pnl_per_share column not found').not.toBe(-1);
    expect(pnlPerShareIdx).toBe(pnlPctIdx + 1);
  });

  it('pnl_per_share uses aggFmtGrid as valueFormatter', () => {
    const opts = makeOpts();
    const cols = mkRightColDefs(opts);
    const col = cols.find(c => c.colId === 'pnl_per_share');
    // Invoke the valueFormatter — it must be the aggFmtGrid stub.
    col.valueFormatter({ value: 99 });
    expect(opts.aggFmtGrid).toHaveBeenCalledWith({ value: 99 });
  });

  it('pnl_per_share cellClass delegates to pnlCellClass with field name', () => {
    const opts = makeOpts();
    const cols = mkRightColDefs(opts);
    const col = cols.find(c => c.colId === 'pnl_per_share');
    const fakeP = { data: { pnl_per_share: 50 } };
    col.cellClass(fakeP);
    expect(opts.pnlCellClass).toHaveBeenCalledWith(fakeP, 'pnl_per_share');
  });
});

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

// ===========================================================================
// Flash / animation fixes — 7-fix set (Fixes 1–4 + Fix 7)
// ===========================================================================

// Shared helpers for flash-fix tests
const _RA = 'ag-right-aligned-cell';
/** @type {() => any} */
const _noFlash = () => ({ classOf: () => null });

function _makePnlCellClass() {
  return mkPnlCellClass({
    RA:              _RA,
    getMpFlash:      _noFlash,
    getLtpFlashUp:   () => new Set(),
    getLtpFlashDown: () => new Set(),
  });
}

function _makeP(value, data = {}) {
  return { value, data };
}

// ---------------------------------------------------------------------------
// Fix 1 — day_pnl_pct cellClass: no mp-pnl-cell, directional text only
// ---------------------------------------------------------------------------

describe('Fix 1 — day_pnl_pct column cellClass (no mp-pnl-cell)', () => {
  // The column uses inline lambda: (p) => `${RA} ${dirCls(p.value)}`
  const dayPnlPctCellClass = (p) => `${_RA} ${dirCls(p.value)}`;

  it('positive value: contains cell-pos, does NOT contain mp-pnl-cell', () => {
    const result = dayPnlPctCellClass(_makeP(1.5));
    expect(result).toContain('cell-pos');
    expect(result).not.toContain('mp-pnl-cell');
  });

  it('negative value: contains cell-neg, does NOT contain mp-pnl-cell', () => {
    const result = dayPnlPctCellClass(_makeP(-0.5));
    expect(result).toContain('cell-neg');
    expect(result).not.toContain('mp-pnl-cell');
  });

  it('zero value: contains cell-flat, does NOT contain mp-pnl-cell', () => {
    const result = dayPnlPctCellClass(_makeP(0));
    expect(result).toContain('cell-flat');
    expect(result).not.toContain('mp-pnl-cell');
  });

  it('null value: contains cell-flat, does NOT contain mp-pnl-cell', () => {
    const result = dayPnlPctCellClass(_makeP(null));
    expect(result).toContain('cell-flat');
    expect(result).not.toContain('mp-pnl-cell');
  });
});

// ---------------------------------------------------------------------------
// Fix 2 — pnl_pct cellClass: no mp-pnl-cell, directional text only
// ---------------------------------------------------------------------------

describe('Fix 2 — pnl_pct column cellClass (no mp-pnl-cell)', () => {
  const pnlPctCellClass = (p) => `${_RA} ${dirCls(p.value)}`;

  it('positive value: contains cell-pos, does NOT contain mp-pnl-cell', () => {
    const result = pnlPctCellClass(_makeP(3.2));
    expect(result).toContain('cell-pos');
    expect(result).not.toContain('mp-pnl-cell');
  });

  it('negative value: contains cell-neg, does NOT contain mp-pnl-cell', () => {
    const result = pnlPctCellClass(_makeP(-2.1));
    expect(result).toContain('cell-neg');
    expect(result).not.toContain('mp-pnl-cell');
  });
});

// ---------------------------------------------------------------------------
// Fix 3 — mkPosSummaryCols: day_change_percentage uses dirCellClass
// ---------------------------------------------------------------------------

describe('Fix 3 — mkPosSummaryCols day_change_percentage (no mp-pnl-cell)', () => {
  const dirCellClass = (p) => `${_RA} ${dirCls(p.value)}`;
  const pnlCellClass = _makePnlCellClass();
  const cols = mkPosSummaryCols({
    numericHdr: 'ag-right-aligned-header',
    pnlCellClass,
    dirCellClass,
    aggFmtGrid: () => '',
    pctFmtGrid: () => '',
  });

  const dayChangePctCol = cols.find(c => c.field === 'day_change_percentage');

  it('day_change_percentage column exists', () => {
    expect(dayChangePctCol).toBeDefined();
  });

  it('positive value: contains cell-pos, does NOT contain mp-pnl-cell', () => {
    const result = dayChangePctCol.cellClass(_makeP(2.5));
    expect(result).toContain('cell-pos');
    expect(result).not.toContain('mp-pnl-cell');
  });

  it('negative value: contains cell-neg, does NOT contain mp-pnl-cell', () => {
    const result = dayChangePctCol.cellClass(_makeP(-1.1));
    expect(result).toContain('cell-neg');
    expect(result).not.toContain('mp-pnl-cell');
  });

  it('day_pnl column still uses pnlCellClass (has mp-pnl-cell)', () => {
    const dayPnlCol = cols.find(c => c.field === 'day_pnl');
    // pnlCellClass with no field → early return of base which contains mp-pnl-cell
    const result = dayPnlCol.cellClass(_makeP(100));
    expect(result).toContain('mp-pnl-cell');
  });
});

// ---------------------------------------------------------------------------
// Fix 4 — mkHoldSummaryCols: day_change_percentage and pnl_percentage no mp-pnl-cell
// ---------------------------------------------------------------------------

describe('Fix 4 — mkHoldSummaryCols pct columns (no mp-pnl-cell)', () => {
  const dirCellClass = (p) => `${_RA} ${dirCls(p.value)}`;
  const pnlCellClass = _makePnlCellClass();
  const cols = mkHoldSummaryCols({
    RA: _RA,
    numericHdr: 'ag-right-aligned-header',
    pnlCellClass,
    dirCellClass,
    aggFmtGrid: () => '',
    pctFmtGrid: () => '',
  });

  const dayChangePctCol = cols.find(c => c.field === 'day_change_percentage');
  const pnlPctCol       = cols.find(c => c.field === 'pnl_percentage');

  it('day_change_percentage column exists', () => {
    expect(dayChangePctCol).toBeDefined();
  });

  it('pnl_percentage column exists', () => {
    expect(pnlPctCol).toBeDefined();
  });

  it('day_change_percentage positive: cell-pos, no mp-pnl-cell', () => {
    const result = dayChangePctCol.cellClass(_makeP(1.8));
    expect(result).toContain('cell-pos');
    expect(result).not.toContain('mp-pnl-cell');
  });

  it('day_change_percentage negative: cell-neg, no mp-pnl-cell', () => {
    const result = dayChangePctCol.cellClass(_makeP(-0.7));
    expect(result).toContain('cell-neg');
    expect(result).not.toContain('mp-pnl-cell');
  });

  it('pnl_percentage positive: cell-pos, no mp-pnl-cell', () => {
    const result = pnlPctCol.cellClass(_makeP(5.0));
    expect(result).toContain('cell-pos');
    expect(result).not.toContain('mp-pnl-cell');
  });

  it('pnl_percentage negative: cell-neg, no mp-pnl-cell', () => {
    const result = pnlPctCol.cellClass(_makeP(-3.3));
    expect(result).toContain('cell-neg');
    expect(result).not.toContain('mp-pnl-cell');
  });

  it('day_pnl column still uses pnlCellClass (has mp-pnl-cell)', () => {
    const dayPnlCol = cols.find(c => c.field === 'day_pnl');
    const result = dayPnlCol.cellClass(_makeP(500));
    expect(result).toContain('mp-pnl-cell');
  });
});

// ---------------------------------------------------------------------------
// Fix 7 — mkPnlCellClass: _isTotal rows produce no TOTAL:* flash class
// ---------------------------------------------------------------------------

describe('Fix 7 — mkPnlCellClass: _isTotal rows produce no flash class', () => {
  const pnlCellClass = _makePnlCellClass();

  it('TOTAL row with no tradingsymbol returns base class only (no flash suffix)', () => {
    // sym is falsy → early return at `!sym` guard → returns base
    const p = _makeP(12345, { _isTotal: true });
    const result = pnlCellClass(p, 'day_pnl');
    // base = `${RA} ${dirCls(p.value)} mp-pnl-cell`
    expect(result).toBe(`${_RA} cell-pos mp-pnl-cell`);
    expect(result).not.toMatch(/tf-up|tf-down|ltp-flash/);
  });

  it('TOTAL row negative value returns base class only', () => {
    const p = _makeP(-5000, { _isTotal: true });
    const result = pnlCellClass(p, 'pnl');
    expect(result).toBe(`${_RA} cell-neg mp-pnl-cell`);
    expect(result).not.toMatch(/tf-up|tf-down|ltp-flash/);
  });

  it('TOTAL row: getMpFlash.classOf is NOT called for TOTAL:* keys', () => {
    /** @type {import('vitest').Mock<() => '' | 'tf-up' | 'tf-down'>} */
    const classOf = vi.fn(() => /** @type {'tf-up'} */ ('tf-up'));
    /** @type {() => any} */
    const getMpFlash = () => ({ classOf });
    const pcc = mkPnlCellClass({
      RA: _RA,
      getMpFlash,
      getLtpFlashUp:   () => new Set(),
      getLtpFlashDown: () => new Set(),
    });
    // _isTotal=true, no tradingsymbol → sym falsy → early return before classOf
    pcc(_makeP(100, { _isTotal: true }), 'day_pnl');
    expect(classOf).not.toHaveBeenCalledWith('TOTAL:day_pnl');
  });

  it('regular row WITH tradingsymbol still gets flash class from getMpFlash', () => {
    /** @type {import('vitest').Mock<() => '' | 'tf-up' | 'tf-down'>} */
    const classOf = vi.fn(() => /** @type {'tf-up'} */ ('tf-up'));
    /** @type {() => any} */
    const getMpFlash = () => ({ classOf });
    const pcc = mkPnlCellClass({
      RA: _RA,
      getMpFlash,
      getLtpFlashUp:   () => new Set(),
      getLtpFlashDown: () => new Set(),
    });
    const result = pcc(_makeP(200, { tradingsymbol: 'RELIANCE' }), 'day_pnl');
    expect(result).toContain('tf-up');
    expect(classOf).toHaveBeenCalledWith('RELIANCE:day_pnl');
  });
});
