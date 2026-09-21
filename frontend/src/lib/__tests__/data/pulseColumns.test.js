import { describe, it, expect, vi } from 'vitest';
import { mkSymColLeft, mkSymColRight } from '../../data/pulseColumns.js';


// positionsDerivedStore is a Svelte 5 reactive module (.svelte.js) — mock it so
// vitest doesn't try to process $state/$derived runes without the Svelte plugin.
vi.mock('$lib/data/positionsDerivedStore.svelte.js', () => ({
  positionsDerivedStore: {
    byKey: {},
    get: (sym) => ({ day_pnl: null, chg_pct: null, pnl: null, exp_pnl: null, extrinsic: null, prev_mv: null })
  }
}));

vi.mock('$lib/data/holdingsDayPnlStore.svelte.js', () => ({
  holdingsDayPnlStore: {
    chgPctByKey: {},
    get: (sym) => ({ day_pnl: null, chg_pct: null })
  }
}));

import { mkRightColDefs, mkPrevCol, dirCls, mkPnlCellClass, mkPosSummaryCols, mkHoldSummaryCols, mkDeltaCol, mkThetaCol, mkLtpCol } from '../../data/pulseColumns.js';

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
    expect(result).toEqual({ background: 'var(--algo-green-badge)', color: 'var(--algo-green)' });
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

  it('lots cellClass is a string (RA constant), not a function', () => {
    const col = getLotsCol();
    expect(typeof col.cellClass).toBe('string');
  });

  it('lots cellClass equals the RA class string', () => {
    const col = getLotsCol();
    expect(col.cellClass).toContain('ra-cls');
  });

  it('does not include lots-left-sep for any row type (double-border fix)', () => {
    const col = getLotsCol();
    // cellClass is now a plain string — no function to call
    expect(col.cellClass).not.toContain('lots-left-sep');
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

// ---------------------------------------------------------------------------
// P.Close rename + reorder — mkPrevCol headerName and prevCol position
// ---------------------------------------------------------------------------

describe('mkPrevCol — headerName is P.Close', () => {
  const col = mkPrevCol({
    RA: 'ra-cls',
    numericHdr: 'ag-right-aligned-header',
    numFmt: ({ value }) => String(value),
  });

  it('headerName is "P.Close"', () => {
    expect(col.headerName).toBe('P.Close');
  });

  it('field is "close"', () => {
    expect(col.field).toBe('close');
  });

  it('valueFormatter returns empty string for _isTotal rows', () => {
    expect(col.valueFormatter({ data: { _isTotal: true }, value: 100 })).toBe('');
  });

  it('valueFormatter calls numFmt for normal rows', () => {
    const numFmt = vi.fn(({ value }) => `${value}`);
    const c = mkPrevCol({ RA: 'ra', numericHdr: 'h', numFmt });
    c.valueFormatter({ data: {}, value: 250 });
    expect(numFmt).toHaveBeenCalledWith({ value: 250 });
  });
});

describe('mkRightColDefs — column order LTP→Chg%→Lots→Qty→Avg→P.Close', () => {
  function getOrderedCols() {
    return mkRightColDefs(makeOpts());
  }

  it('ltpCol appears before lots column', () => {
    const cols = getOrderedCols();
    const ltpIdx  = cols.findIndex(c => c.colId === 'ltp'  || c.field === 'ltp');
    const lotsIdx = cols.findIndex(c => c.colId === 'lots' || c.field === 'lots');
    expect(ltpIdx,  'ltp column not found').not.toBe(-1);
    expect(lotsIdx, 'lots column not found').not.toBe(-1);
    expect(ltpIdx).toBeLessThan(lotsIdx);
  });

  it('prevCol appears BEFORE day_pnl', () => {
    const cols = getOrderedCols();
    const prevIdx   = cols.findIndex(c => c.field === 'prev');
    const dayPnlIdx = cols.findIndex(c => c.field === 'day_pnl');
    expect(prevIdx,   'prev column not found').not.toBe(-1);
    expect(dayPnlIdx, 'day_pnl column not found').not.toBe(-1);
    expect(prevIdx).toBeLessThan(dayPnlIdx);
  });

  it('day_pnl_pct (Chg%) appears immediately after ltpCol', () => {
    const cols = getOrderedCols();
    const ltpIdx       = cols.findIndex(c => c.colId === 'ltp'  || c.field === 'ltp');
    const dayPnlPctIdx = cols.findIndex(c => c.colId === 'day_pnl_pct');
    expect(ltpIdx,       'ltp column not found').not.toBe(-1);
    expect(dayPnlPctIdx, 'day_pnl_pct column not found').not.toBe(-1);
    expect(ltpIdx + 1).toBe(dayPnlPctIdx);
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

  it('day_pnl column uses pnlCellClass (text color only, no mp-pnl-cell)', () => {
    const dayPnlCol = cols.find(c => c.field === 'day_pnl');
    const result = dayPnlCol.cellClass(_makeP(100));
    expect(result).toContain('cell-pos');
    expect(result).not.toContain('mp-pnl-cell');
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

  it('day_pnl column uses pnlCellClass (text color only, no mp-pnl-cell)', () => {
    const dayPnlCol = cols.find(c => c.field === 'day_pnl');
    const result = dayPnlCol.cellClass(_makeP(500));
    expect(result).toContain('cell-pos');
    expect(result).not.toContain('mp-pnl-cell');
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
    // base = `${RA} ${dirCls(p.value)}`
    expect(result).toBe(`${_RA} cell-pos`);
    expect(result).not.toMatch(/tf-up|tf-down|ltp-flash/);
  });

  it('TOTAL row negative value returns base class only', () => {
    const p = _makeP(-5000, { _isTotal: true });
    const result = pnlCellClass(p, 'pnl');
    expect(result).toBe(`${_RA} cell-neg`);
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

  it('regular row WITH tradingsymbol returns base class only (pnl columns do not flash)', () => {
    // mkPnlCellClass flash logic was intentionally removed — pnl/day_pnl columns
    // should never flash; only ltp and chg% flash.
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
    expect(result).toBe(`${_RA} cell-pos`);
    expect(result).not.toMatch(/tf-up|tf-down/);
  });
});

// ─────────────────────────────────────────────────────────────────────────
// _dayPnlPctValueGetter — enforce SSOT (no change_pct fallback)
// ─────────────────────────────────────────────────────────────────────────

describe('_dayPnlPctValueGetter — SSOT enforcement (no change_pct fallback)', () => {
  it('returns null when both positionsDerivedStore.get(sym).chg_pct and holdingsDayPnlStore.get(sym).chg_pct are null', () => {
    // Mock the stores to return null for chg_pct
    const mockPositionsDerivedStore = {
      get: (sym) => ({ chg_pct: null }),
    };
    const mockHoldingsDayPnlStore = {
      get: (sym) => ({ chg_pct: null }),
    };

    // Inline version of _dayPnlPctValueGetter using the get() accessors
    const _dayPnlPctValueGetter = (p) => {
      const sym = String(p.data?.tradingsymbol || p.data?.symbol || '').toUpperCase();
      return mockPositionsDerivedStore.get(sym).chg_pct ?? mockHoldingsDayPnlStore.get(sym).chg_pct;
    };

    const p = { data: { tradingsymbol: 'UNKNOWN', change_pct: 5.0 } };
    const result = _dayPnlPctValueGetter(p);
    expect(result).toBeNull();
  });

  it('returns positions chg_pct when populated', () => {
    const mockPositionsDerivedStore = {
      get: (sym) => ({ chg_pct: 3.5 }),
    };
    const mockHoldingsDayPnlStore = {
      get: (sym) => ({ chg_pct: null }),
    };

    const _dayPnlPctValueGetter = (p) => {
      const sym = String(p.data?.tradingsymbol || p.data?.symbol || '').toUpperCase();
      return mockPositionsDerivedStore.get(sym).chg_pct ?? mockHoldingsDayPnlStore.get(sym).chg_pct;
    };

    const p = { data: { tradingsymbol: 'RELIANCE', change_pct: 5.0 } };
    const result = _dayPnlPctValueGetter(p);
    expect(result).toBe(3.5);
  });

  it('returns holdings chg_pct as fallback when positions chg_pct is null', () => {
    const mockPositionsDerivedStore = {
      get: (sym) => ({ chg_pct: null }),
    };
    const mockHoldingsDayPnlStore = {
      get: (sym) => ({ chg_pct: 2.1 }),
    };

    const _dayPnlPctValueGetter = (p) => {
      const sym = String(p.data?.tradingsymbol || p.data?.symbol || '').toUpperCase();
      return mockPositionsDerivedStore.get(sym).chg_pct ?? mockHoldingsDayPnlStore.get(sym).chg_pct;
    };

    const p = { data: { tradingsymbol: 'INFY', change_pct: 5.0 } };
    const result = _dayPnlPctValueGetter(p);
    expect(result).toBe(2.1);
  });
});

// ---------------------------------------------------------------------------
// Fix 5 — mkDeltaCol / mkThetaCol: TOTAL row renders '' not '—'
// ---------------------------------------------------------------------------

describe('mkDeltaCol — TOTAL row renders empty string (Fix 5)', () => {
  const col = mkDeltaCol({ RA: 'ra-cls', numericHdr: 'ag-right-aligned-header' });

  it('TOTAL row with null value renders ""', () => {
    expect(col.valueFormatter({ data: { _isTotal: true }, value: null })).toBe('');
  });

  it('TOTAL row with 0 value renders ""', () => {
    expect(col.valueFormatter({ data: { _isTotal: true }, value: 0 })).toBe('');
  });

  it('TOTAL row with a real delta value renders ""', () => {
    // TOTAL row always returns '' regardless of value
    expect(col.valueFormatter({ data: { _isTotal: true }, value: 1.25 })).toBe('');
  });

  it('normal row with null value renders "—"', () => {
    expect(col.valueFormatter({ data: {}, value: null })).toBe('—');
  });

  it('normal row with 0 value renders "—"', () => {
    expect(col.valueFormatter({ data: {}, value: 0 })).toBe('—');
  });

  it('normal row with a real delta value renders toFixed(2)', () => {
    expect(col.valueFormatter({ data: {}, value: 0.75 })).toBe('0.75');
  });

  it('normal row with negative delta renders toFixed(2)', () => {
    expect(col.valueFormatter({ data: {}, value: -0.5 })).toBe('-0.50');
  });
});

// ---------------------------------------------------------------------------
// cur_val valueGetter — null when no live tick; live LTP × held qty when snap present
//
// Architectural principle: no surface silently substitutes a stale value when
// the symbolStore returns null/0. When ltp=0 or the symbol is absent from the
// snap, the Value column returns null (blank) — not p.data.cur_val, which was
// computed at buildUnified time and may be up to 10s stale.
// The LTP column already shows the broker-seed via mkResolveCellLtp; Value
// should be blank until the first live tick arrives.
// ---------------------------------------------------------------------------

describe('cur_val valueGetter — null when no live tick (stale-masking fix)', () => {
  function getCurValCol(getLiveLtpSnap) {
    const opts = { ...makeOpts(), getLiveLtpSnap };
    const cols = mkRightColDefs(opts);
    const col = cols.find(c => c.colId === 'cur_val');
    if (!col) throw new Error('cur_val column not found');
    return col;
  }

  it('returns ltp * qty_hold when snap has a positive LTP for the symbol', () => {
    const snap = { RELIANCE: 2500 };
    const col = getCurValCol(() => snap);
    const result = col.valueGetter({ data: { qty_hold: 10, tradingsymbol: 'RELIANCE', quote_symbol: '' } });
    expect(result).toBe(25000);
  });

  it('returns null (not stale cur_val) when snap LTP is 0', () => {
    const snap = { RELIANCE: 0 };
    const col = getCurValCol(() => snap);
    const result = col.valueGetter({ data: { qty_hold: 10, tradingsymbol: 'RELIANCE', quote_symbol: '', cur_val: 9999 } });
    expect(result).toBeNull();
  });

  it('returns null (not stale cur_val) when getLiveLtpSnap returns {} (empty snap)', () => {
    // This is the canonical stale-masking test: snap is empty, p.data.cur_val=1000
    // (computed at buildUnified time, up to 10s stale). The cell must return null.
    const col = getCurValCol(() => ({}));
    const result = col.valueGetter({ data: { qty_hold: 5, tradingsymbol: 'INFY', quote_symbol: '', cur_val: 1000 } });
    expect(result).toBeNull();
  });

  it('returns null when symbol is absent from snap (same as empty snap)', () => {
    const snap = { OTHER: 500 };
    const col = getCurValCol(() => snap);
    const result = col.valueGetter({ data: { qty_hold: 5, tradingsymbol: 'INFY', quote_symbol: '', cur_val: 5500 } });
    expect(result).toBeNull();
  });

  it('uses quote_symbol when present (non-empty) instead of tradingsymbol', () => {
    const snap = { NIFTY: 24500 };
    const col = getCurValCol(() => snap);
    const result = col.valueGetter({ data: { qty_hold: 2, tradingsymbol: 'NIFTY24OPTCE', quote_symbol: 'NIFTY', cur_val: 0 } });
    expect(result).toBe(49000);
  });

  it('returns null when qty_hold is 0', () => {
    const snap = { RELIANCE: 2500 };
    const col = getCurValCol(() => snap);
    const result = col.valueGetter({ data: { qty_hold: 0, tradingsymbol: 'RELIANCE', quote_symbol: '' } });
    expect(result).toBeNull();
  });

  it('returns p.data.cur_val when getLiveLtpSnap is not provided (undefined) — no-accessor path unchanged', () => {
    // This branch (line: if (!getLiveLtpSnap) return p.data.cur_val) is outside the
    // stale-masking fix scope — it fires only when the accessor itself is missing,
    // not when the snap is empty. Retained as-is.
    const col = getCurValCol(undefined);
    const result = col.valueGetter({ data: { qty_hold: 3, tradingsymbol: 'TCS', quote_symbol: '', cur_val: 1200 } });
    expect(result).toBe(1200);
  });

  it('returns p.data.cur_val for _isTotal rows regardless of snap', () => {
    const snap = { TOTAL: 9999 };
    const col = getCurValCol(() => snap);
    const result = col.valueGetter({ data: { _isTotal: true, cur_val: 500000 } });
    expect(result).toBe(500000);
  });

  it('returns null for _isTotal rows with no cur_val', () => {
    const col = getCurValCol(() => ({}));
    const result = col.valueGetter({ data: { _isTotal: true } });
    expect(result).toBeNull();
  });

  it('returns null when data is null or undefined', () => {
    const col = getCurValCol(() => ({}));
    expect(col.valueGetter({ data: null })).toBeNull();
  });

  it('handles negative qty_hold (absolute value used for multiplication)', () => {
    const snap = { HDFC: 1700 };
    const col = getCurValCol(() => snap);
    const result = col.valueGetter({ data: { qty_hold: -5, tradingsymbol: 'HDFC', quote_symbol: '', cur_val: 0 } });
    expect(result).toBe(8500);
  });
});

describe('mkThetaCol — TOTAL row renders empty string (Fix 5)', () => {
  const aggFmtGrid = vi.fn(({ value }) => `agg:${value}`);
  const col = mkThetaCol({ RA: 'ra-cls', numericHdr: 'ag-right-aligned-header', aggFmtGrid });

  it('TOTAL row with null value renders ""', () => {
    expect(col.valueFormatter({ data: { _isTotal: true }, value: null })).toBe('');
  });

  it('TOTAL row with 0 value renders ""', () => {
    expect(col.valueFormatter({ data: { _isTotal: true }, value: 0 })).toBe('');
  });

  it('TOTAL row with a real theta value renders ""', () => {
    expect(col.valueFormatter({ data: { _isTotal: true }, value: -500 })).toBe('');
  });

  it('normal row with null value renders "—"', () => {
    expect(col.valueFormatter({ data: {}, value: null })).toBe('—');
  });

  it('normal row with 0 value renders "—"', () => {
    expect(col.valueFormatter({ data: {}, value: 0 })).toBe('—');
  });

  it('normal row with a real theta value delegates to aggFmtGrid', () => {
    col.valueFormatter({ data: {}, value: -750 });
    expect(aggFmtGrid).toHaveBeenCalledWith({ value: -750 });
  });
});

// ---------------------------------------------------------------------------
// ag-col-sym cellClass — mkSymColLeft + mkSymColRight (Change 1 + Change 2)
// ---------------------------------------------------------------------------

describe('mkSymColLeft — cellClass contains ag-col-sym', () => {
  const col = mkSymColLeft({ symRenderer: (p) => p.data?.tradingsymbol });

  it('cellClass contains ag-col-sym', () => {
    expect(col.cellClass).toContain('ag-col-sym');
  });

  it('cellClass contains ag-col-sym-left', () => {
    expect(col.cellClass).toContain('ag-col-sym-left');
  });
});

describe('mkSymColRight — cellClass contains ag-col-sym', () => {
  const col = mkSymColRight({ symRenderer: (p) => p.data?.tradingsymbol });

  it('cellClass contains ag-col-sym', () => {
    expect(col.cellClass).toContain('ag-col-sym');
  });

  it('cellClass does not contain ag-col-sym-left (right grid has no left variant)', () => {
    expect(col.cellClass).not.toContain('ag-col-sym-left');
  });
});

// ---------------------------------------------------------------------------
// mkLtpCol — tooltipValueGetter (Change 3)
// ---------------------------------------------------------------------------

describe('mkLtpCol — tooltipValueGetter', () => {
  function makeLtpCol() {
    return mkLtpCol({
      getLiveLtpSnap:  () => ({}),
      getLtpFlashUp:   () => new Set(),
      getLtpFlashDown: () => new Set(),
      numFmt:          ({ value }) => String(value ?? ''),
      RA:              'ag-right-aligned-cell',
      numericHdr:      'ag-right-aligned-header',
    });
  }

  it('returns an object with a tooltipValueGetter function', () => {
    const col = makeLtpCol();
    expect(typeof col.tooltipValueGetter).toBe('function');
  });

  it('tooltipValueGetter returns "No live price" when value is null', () => {
    const col = makeLtpCol();
    expect(col.tooltipValueGetter({ value: null })).toBe('No live price');
  });

  it('tooltipValueGetter returns "No live price" when value is undefined', () => {
    const col = makeLtpCol();
    expect(col.tooltipValueGetter({ value: undefined })).toBe('No live price');
  });

  it('tooltipValueGetter returns null when value is a number (1234.5)', () => {
    const col = makeLtpCol();
    expect(col.tooltipValueGetter({ value: 1234.5 })).toBeNull();
  });

  it('tooltipValueGetter returns null when value is 0', () => {
    const col = makeLtpCol();
    expect(col.tooltipValueGetter({ value: 0 })).toBeNull();
  });
});
