import { describe, it, expect } from 'vitest';
import { agDirCellText, mkDirCellText } from '../../data/algoGridUtils.js';

// CellClassParams is a large ag-Grid type (column, colDef, data, node, …).
// Tests only exercise the `.value` field the two helpers actually read, so
// cast the minimal stub to `any` rather than fabricating the full shape.
/** @param {any} value */
const p = (value) => /** @type {any} */ ({ value });

describe('agDirCellText', () => {
  it('positive value → dir-gain', () => {
    expect(agDirCellText(p(5))).toBe('ag-right-aligned-cell dir-gain');
  });

  it('negative value → dir-loss (amber, the historical default)', () => {
    expect(agDirCellText(p(-5))).toBe('ag-right-aligned-cell dir-loss');
  });

  it('zero / null / undefined → dir-flat', () => {
    expect(agDirCellText(p(0))).toBe('ag-right-aligned-cell dir-flat');
    expect(agDirCellText(p(null))).toBe('ag-right-aligned-cell dir-flat');
    expect(agDirCellText(p(undefined))).toBe('ag-right-aligned-cell dir-flat');
  });
});

describe('mkDirCellText', () => {
  it('default (no options) is byte-identical to agDirCellText — every existing caller unaffected', () => {
    const fn = mkDirCellText();
    for (const value of [5, -5, 0, null, undefined]) {
      expect(fn(p(value))).toBe(agDirCellText(p(value)));
    }
  });

  it('{ lossRed: true } → negative resolves to dir-loss-red (A6, matches PositionStrip .ps-neg)', () => {
    const fn = mkDirCellText({ lossRed: true });
    expect(fn(p(-1234))).toBe('ag-right-aligned-cell dir-loss-red');
  });

  it('{ lossRed: true } → positive/zero unaffected (still dir-gain / dir-flat)', () => {
    const fn = mkDirCellText({ lossRed: true });
    expect(fn(p(1234))).toBe('ag-right-aligned-cell dir-gain');
    expect(fn(p(0))).toBe('ag-right-aligned-cell dir-flat');
  });

  it('returns a fresh stable function reference per call (caller should memoise once, not per-render)', () => {
    const a = mkDirCellText({ lossRed: true });
    const b = mkDirCellText({ lossRed: true });
    expect(typeof a).toBe('function');
    expect(typeof b).toBe('function');
    // Not the same reference — this is why NavBreakdown.svelte assigns
    // the result to a module-level const instead of calling
    // mkDirCellText() inline inside the column-def array.
    expect(a).not.toBe(b);
  });
});
