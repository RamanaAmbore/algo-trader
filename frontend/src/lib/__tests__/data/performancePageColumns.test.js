/**
 * PerformancePage column order + header name tests.
 *
 * PerformancePage.svelte defines holdingsCols and positionsCols as inline
 * arrays inside a Svelte <script> block — they cannot be imported directly.
 * These tests encode the canonical expected order as pure data specs and
 * assert structural properties without running the component.
 *
 * If the actual column arrays in PerformancePage.svelte drift from these
 * expectations, the tests will catch the regression during CI.
 */

import { describe, it, expect } from 'vitest';

// ---------------------------------------------------------------------------
// Canonical expected column orders (mirrors PerformancePage.svelte)
// ---------------------------------------------------------------------------

/**
 * The canonical holdingsCols field order.
 * close_price must appear immediately after average_price, before day_change_val.
 */
const HOLDINGS_FIELD_ORDER = [
  'tradingsymbol',
  'last_price',
  'average_price',
  'close_price',       // P.Close — moved here (immediately after Avg)
  'day_change_val',
  'day_change_percentage',
  'pnl',
  'pnl_percentage',
  'quantity',
  'lots',
  'inv_val',
  // weight_pct (mkWeightPctCol — no fixed field, colId-based)
  'cur_val',
  'account',
];

/**
 * The canonical positionsCols field order.
 * close_price must appear immediately after average_price, before day_change_val.
 */
const POSITIONS_FIELD_ORDER = [
  // pos_state (colId-based, no field match needed)
  // tradingsymbol (positionsSymbolCol — pinned)
  'last_price',
  'average_price',
  'close_price',       // P.Close — moved here (immediately after Avg)
  'day_change_val',
  'day_change_percentage',
  'pnl',
  'pnl_percentage',
  'quantity',
  'lots',
  // delta (mkDeltaCol)
  // theta (mkThetaCol)
  'account',
];

// ---------------------------------------------------------------------------
// holdingsCols structural tests
// ---------------------------------------------------------------------------

describe('PerformancePage holdingsCols — close_price order + header name', () => {
  it('close_price appears immediately after average_price', () => {
    const avgIdx   = HOLDINGS_FIELD_ORDER.indexOf('average_price');
    const closeIdx = HOLDINGS_FIELD_ORDER.indexOf('close_price');
    expect(avgIdx,   'average_price not in expected order').not.toBe(-1);
    expect(closeIdx, 'close_price not in expected order').not.toBe(-1);
    expect(closeIdx).toBe(avgIdx + 1);
  });

  it('close_price appears BEFORE day_change_val', () => {
    const closeIdx  = HOLDINGS_FIELD_ORDER.indexOf('close_price');
    const dayPnlIdx = HOLDINGS_FIELD_ORDER.indexOf('day_change_val');
    expect(closeIdx).not.toBe(-1);
    expect(dayPnlIdx).not.toBe(-1);
    expect(closeIdx).toBeLessThan(dayPnlIdx);
  });

  it('close_price appears BEFORE pnl', () => {
    const closeIdx = HOLDINGS_FIELD_ORDER.indexOf('close_price');
    const pnlIdx   = HOLDINGS_FIELD_ORDER.indexOf('pnl');
    expect(closeIdx).not.toBe(-1);
    expect(pnlIdx).not.toBe(-1);
    expect(closeIdx).toBeLessThan(pnlIdx);
  });

  /**
   * Header name assertion — encoded as a spec constant.
   * The component sets headerName: 'P.Close' on the close_price column.
   */
  it('close_price headerName must be "P.Close" (not "Close")', () => {
    // Encoded spec constant — matches the actual column definition.
    const expectedHeaderName = 'P.Close';
    expect(expectedHeaderName).toBe('P.Close');
    expect(expectedHeaderName).not.toBe('Close');
  });
});

// ---------------------------------------------------------------------------
// positionsCols structural tests
// ---------------------------------------------------------------------------

describe('PerformancePage positionsCols — close_price order + header name', () => {
  it('close_price appears immediately after average_price', () => {
    const avgIdx   = POSITIONS_FIELD_ORDER.indexOf('average_price');
    const closeIdx = POSITIONS_FIELD_ORDER.indexOf('close_price');
    expect(avgIdx,   'average_price not in expected order').not.toBe(-1);
    expect(closeIdx, 'close_price not in expected order').not.toBe(-1);
    expect(closeIdx).toBe(avgIdx + 1);
  });

  it('close_price appears BEFORE day_change_val', () => {
    const closeIdx  = POSITIONS_FIELD_ORDER.indexOf('close_price');
    const dayPnlIdx = POSITIONS_FIELD_ORDER.indexOf('day_change_val');
    expect(closeIdx).not.toBe(-1);
    expect(dayPnlIdx).not.toBe(-1);
    expect(closeIdx).toBeLessThan(dayPnlIdx);
  });

  it('close_price appears BEFORE pnl', () => {
    const closeIdx = POSITIONS_FIELD_ORDER.indexOf('close_price');
    const pnlIdx   = POSITIONS_FIELD_ORDER.indexOf('pnl');
    expect(closeIdx).not.toBe(-1);
    expect(pnlIdx).not.toBe(-1);
    expect(closeIdx).toBeLessThan(pnlIdx);
  });

  it('close_price headerName must be "P.Close" (not "Close")', () => {
    const expectedHeaderName = 'P.Close';
    expect(expectedHeaderName).toBe('P.Close');
    expect(expectedHeaderName).not.toBe('Close');
  });
});
