/**
 * sourceRowClasses.test.js
 *
 * Unit tests for the _sourceRowClasses logic extracted from MarketPulse.svelte.
 * Specifically verifies Fix B: orphan/paired CSS class wiring on position rows.
 *
 * Since _sourceRowClasses is a local function inside the Svelte component,
 * we replicate its exact logic here and keep it in sync.
 */

import { describe, it, expect } from 'vitest';

// Replicated logic from MarketPulse.svelte _sourceRowClasses — keep in sync.
function _sourceRowClasses(r, s) {
  const out = [];
  if (s.p) {
    const q = Number(r.qty_pos) || 0;
    if (q < 0) out.push('pos-short');
    else if (q > 0) out.push('pos-long');
    else out.push('row-pos');
    if (r.is_orphan) {
      out.push('row-pos-orphan');
    } else if (r.pair_group_key) {
      out.push('row-pos-paired');
    }
  } else if (s.h) {
    const pnl = Number(r.pnl);
    if (Number.isFinite(pnl) && pnl > 0) out.push('row-hold-up');
    else if (Number.isFinite(pnl) && pnl < 0) out.push('row-hold-down');
    else out.push('row-hold-flat');
  }
  if (s.w && !s.p && !s.h) out.push('row-watch');
  else if (s.u) out.push('row-und');
  return out;
}

describe('_sourceRowClasses — orphan/paired Fix B', () => {
  it('orphan long position → pos-long + row-pos-orphan', () => {
    const classes = _sourceRowClasses(
      { qty_pos: 10, is_orphan: true, pair_group_key: null },
      { p: true }
    );
    expect(classes).toContain('pos-long');
    expect(classes).toContain('row-pos-orphan');
    expect(classes).not.toContain('row-pos-paired');
  });

  it('paired long position → pos-long + row-pos-paired', () => {
    const classes = _sourceRowClasses(
      { qty_pos: 10, is_orphan: false, pair_group_key: 'GRP-001' },
      { p: true }
    );
    expect(classes).toContain('pos-long');
    expect(classes).toContain('row-pos-paired');
    expect(classes).not.toContain('row-pos-orphan');
  });

  it('orphan short position → pos-short + row-pos-orphan', () => {
    const classes = _sourceRowClasses(
      { qty_pos: -5, is_orphan: true, pair_group_key: null },
      { p: true }
    );
    expect(classes).toContain('pos-short');
    expect(classes).toContain('row-pos-orphan');
  });

  it('paired short position → pos-short + row-pos-paired', () => {
    const classes = _sourceRowClasses(
      { qty_pos: -5, is_orphan: false, pair_group_key: 'GRP-002' },
      { p: true }
    );
    expect(classes).toContain('pos-short');
    expect(classes).toContain('row-pos-paired');
  });

  it('zero-qty position with no orphan/pair → row-pos only, no orphan/paired', () => {
    const classes = _sourceRowClasses(
      { qty_pos: 0, is_orphan: false, pair_group_key: null },
      { p: true }
    );
    expect(classes).toContain('row-pos');
    expect(classes).not.toContain('row-pos-orphan');
    expect(classes).not.toContain('row-pos-paired');
  });

  it('position with is_orphan=true and pair_group_key truthy → orphan wins', () => {
    const classes = _sourceRowClasses(
      { qty_pos: 10, is_orphan: true, pair_group_key: 'GRP-003' },
      { p: true }
    );
    expect(classes).toContain('row-pos-orphan');
    expect(classes).not.toContain('row-pos-paired');
  });

  it('holding row → no orphan/paired classes', () => {
    const classes = _sourceRowClasses(
      { pnl: 500, is_orphan: true, pair_group_key: 'GRP-004' },
      { h: true }
    );
    expect(classes).toContain('row-hold-up');
    expect(classes).not.toContain('row-pos-orphan');
    expect(classes).not.toContain('row-pos-paired');
  });

  it('watchlist row → row-watch, no orphan/paired', () => {
    const classes = _sourceRowClasses(
      { is_orphan: true, pair_group_key: 'GRP-005' },
      { w: true }
    );
    expect(classes).toContain('row-watch');
    expect(classes).not.toContain('row-pos-orphan');
    expect(classes).not.toContain('row-pos-paired');
  });

  it('position with empty string pair_group_key → no paired class', () => {
    const classes = _sourceRowClasses(
      { qty_pos: 5, is_orphan: false, pair_group_key: '' },
      { p: true }
    );
    expect(classes).not.toContain('row-pos-paired');
    expect(classes).not.toContain('row-pos-orphan');
  });
});
