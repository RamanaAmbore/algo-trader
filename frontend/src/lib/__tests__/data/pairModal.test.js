import { describe, it, expect } from 'vitest';
import {
  computePairEnabled,
  computePairPreview,
} from '../../order/pairModalUtils.js';

// ---------------------------------------------------------------------------
// computePairEnabled — pair button state
// ---------------------------------------------------------------------------

describe('computePairEnabled', () => {
  const always = () => true;
  const never  = () => false;

  const mkCand = (account, symbol, enabled = true) => ({ account, symbol, quantity: 1, _en: enabled });

  it('disabled when no candidates', () => {
    const r = computePairEnabled([], always);
    expect(r.pairEnabled).toBe(false);
    expect(r.pairedCandidates).toHaveLength(0);
    expect(r.firstCheckedSymbol).toBe('');
  });

  it('disabled when only 1 enabled candidate', () => {
    const cands = [mkCand('ZG1234', 'NIFTY25JUN22000CE')];
    const r = computePairEnabled(cands, always);
    expect(r.pairEnabled).toBe(false);
    expect(r.pairedCandidates).toHaveLength(1);
  });

  it('enabled when 2 same-account candidates are enabled', () => {
    const cands = [
      mkCand('ZG1234', 'NIFTY25JUN22000CE'),
      mkCand('ZG1234', 'NIFTY25JUN22000PE'),
    ];
    const r = computePairEnabled(cands, always);
    expect(r.pairEnabled).toBe(true);
    expect(r.pairedCandidates).toHaveLength(2);
    expect(r.firstCheckedSymbol).toBe('NIFTY25JUN22000CE');
  });

  it('disabled when 2 candidates with different accounts', () => {
    const cands = [
      mkCand('ZG1234', 'NIFTY25JUN22000CE'),
      mkCand('DH9876', 'NIFTY25JUN22000PE'),
    ];
    const r = computePairEnabled(cands, always);
    // Only 1 candidate shares ZG1234's account
    expect(r.pairEnabled).toBe(false);
    expect(r.pairedCandidates).toHaveLength(1);
  });

  it('disabled when 2 candidates but none enabled', () => {
    const cands = [
      mkCand('ZG1234', 'NIFTY25JUN22000CE'),
      mkCand('ZG1234', 'NIFTY25JUN22000PE'),
    ];
    const r = computePairEnabled(cands, never);
    expect(r.pairEnabled).toBe(false);
    expect(r.pairedCandidates).toHaveLength(0);
  });

  it('enabled when 3 same-account — takes first 2', () => {
    const cands = [
      mkCand('ZG1234', 'NIFTY25JUN22000CE'),
      mkCand('ZG1234', 'NIFTY25JUN22000PE'),
      mkCand('ZG1234', 'NIFTY25JUN21000CE'),
    ];
    const r = computePairEnabled(cands, always);
    expect(r.pairEnabled).toBe(true);
    expect(r.pairedCandidates).toHaveLength(2);
    expect(r.pairedCandidates[0].symbol).toBe('NIFTY25JUN22000CE');
    expect(r.pairedCandidates[1].symbol).toBe('NIFTY25JUN22000PE');
  });

  it('uses tradingsymbol as fallback for firstCheckedSymbol', () => {
    const cands = [
      { account: 'ZG1234', tradingsymbol: 'BANKNIFTY25JUN50000CE', quantity: 1 },
      { account: 'ZG1234', tradingsymbol: 'BANKNIFTY25JUN50000PE', quantity: -1 },
    ];
    const r = computePairEnabled(cands, always);
    expect(r.firstCheckedSymbol).toBe('BANKNIFTY25JUN50000CE');
  });

  it('uses custom isEnabled function to gate candidates', () => {
    const cands = [
      mkCand('ZG1234', 'NIFTY25JUN22000CE'),
      mkCand('ZG1234', 'NIFTY25JUN22000PE'),
      mkCand('ZG1234', 'NIFTY25JUN21000CE'),
    ];
    // Only enable first one
    const isEnabled = (c) => c.symbol === 'NIFTY25JUN22000CE';
    const r = computePairEnabled(cands, isEnabled);
    expect(r.pairEnabled).toBe(false);
    expect(r.pairedCandidates).toHaveLength(1);
  });
});

// ---------------------------------------------------------------------------
// computePairPreview — lot-size math
// ---------------------------------------------------------------------------

describe('computePairPreview', () => {
  it('matched quantities (same size)', () => {
    const cands = [
      { quantity: 50 },
      { quantity: -50 },
    ];
    const r = computePairPreview(cands);
    expect(r.qty_a).toBe(50);
    expect(r.qty_b).toBe(50);
    expect(r.proposedPairedQty).toBe(50);
    expect(r.orphanQty).toBe(0);
  });

  it('mismatched quantities — orphan is the difference', () => {
    const cands = [
      { quantity: 75 },
      { quantity: -50 },
    ];
    const r = computePairPreview(cands);
    expect(r.qty_a).toBe(75);
    expect(r.qty_b).toBe(50);
    expect(r.proposedPairedQty).toBe(50);
    expect(r.orphanQty).toBe(25);
  });

  it('zero quantity candidate', () => {
    const cands = [
      { quantity: 0 },
      { quantity: 25 },
    ];
    const r = computePairPreview(cands);
    expect(r.qty_a).toBe(0);
    expect(r.proposedPairedQty).toBe(0);
    expect(r.orphanQty).toBe(25);
  });

  it('falls back to qty_pos when quantity is absent', () => {
    const cands = [
      { qty_pos: 30 },
      { qty_pos: -20 },
    ];
    const r = computePairPreview(cands);
    expect(r.qty_a).toBe(30);
    expect(r.qty_b).toBe(20);
    expect(r.proposedPairedQty).toBe(20);
    expect(r.orphanQty).toBe(10);
  });

  it('handles empty array gracefully', () => {
    const r = computePairPreview([]);
    expect(r.qty_a).toBe(0);
    expect(r.qty_b).toBe(0);
    expect(r.proposedPairedQty).toBe(0);
    expect(r.orphanQty).toBe(0);
  });

  it('both quantities negative — abs applied correctly', () => {
    const cands = [
      { quantity: -100 },
      { quantity: -40 },
    ];
    const r = computePairPreview(cands);
    expect(r.qty_a).toBe(100);
    expect(r.qty_b).toBe(40);
    expect(r.proposedPairedQty).toBe(40);
    expect(r.orphanQty).toBe(60);
  });
});
