import { describe, it, expect } from 'vitest';
import { splitPairedLots } from '../../data/lotPairSplit.js';

/** @param {Partial<any>} overrides */
function row(overrides) {
  return {
    account: 'ACC1',
    tradingsymbol: 'SYM',
    exchange: 'NFO',
    quantity: 0,
    pnl: 0,
    realised: 0,
    unrealised: 0,
    day_change_val: 0,
    is_orphan: false,
    pair_group_key: null,
    paired_qty: 0,
    orphan_qty: 0,
    ...overrides,
  };
}

describe('splitPairedLots — clean single-group match', () => {
  it('row already carrying the correct pairing fields is returned by the same reference (no wrap)', () => {
    const long = row({
      tradingsymbol: 'NIFTY26JUN24000CE', quantity: 100,
      pair_group_key: 'P1', paired_qty: 100, orphan_qty: 0, is_orphan: false,
    });
    const short = row({
      tradingsymbol: 'NIFTY26JUN24500CE', quantity: -100,
      pair_group_key: 'P1', paired_qty: 100, orphan_qty: 0, is_orphan: false,
    });

    const out = splitPairedLots([long, short]);

    expect(out).toHaveLength(2);
    expect(out[0]).toBe(long);
    expect(out[1]).toBe(short);
    expect(out[0].pair_group_key).toBe('P1');
    expect(out[1].pair_group_key).toBe('P1');
    expect(out[0].is_orphan).toBe(false);
    expect(out[1].is_orphan).toBe(false);
  });

  it('a row carrying STALE/incorrect pairing fields gets corrected via a copy (never silently trusted)', () => {
    // Defaults seed pair_group_key: null / paired_qty: 0 — deliberately
    // stale relative to the freshly-derived P1/100 match.
    const long = row({ tradingsymbol: 'NIFTY26JUN24000CE', quantity: 100 });
    const short = row({ tradingsymbol: 'NIFTY26JUN24500CE', quantity: -100 });

    const out = splitPairedLots([long, short]);

    expect(out).toHaveLength(2);
    expect(out[0]).not.toBe(long);  // corrected copy, not the stale original
    expect(out[1]).not.toBe(short);
    expect(out[0].pair_group_key).toBe('P1');
    expect(out[1].pair_group_key).toBe('P1');
    expect(out[0].paired_qty).toBe(100);
    expect(out[0].is_orphan).toBe(false);
    expect(out[1].is_orphan).toBe(false);
  });

  it('flat (quantity 0) rows pass through completely untouched', () => {
    const flat = row({ tradingsymbol: 'NIFTY26JUN24000CE', quantity: 0, pair_group_key: 'STALE' });
    const out = splitPairedLots([flat]);
    expect(out[0]).toBe(flat);
    expect(out[0].pair_group_key).toBe('STALE'); // untouched, not "corrected"
  });
});

describe('splitPairedLots — the reported bug scenario', () => {
  it('one long (300) matched against two different shorts (200 + 100) splits the long into 2 pieces; both shorts stay correctly paired', () => {
    const longRow = row({ tradingsymbol: 'CRUDEOIL26OCTFUT', quantity: 300, account: 'ACC1' });
    const shortA  = row({ tradingsymbol: 'CRUDEOIL26NOVFUT', quantity: -200, account: 'ACC1' });
    const shortB  = row({ tradingsymbol: 'CRUDEOIL26DECFUT', quantity: -100, account: 'ACC1' });

    const out = splitPairedLots([longRow, shortA, shortB]);

    const longPieces = out.filter(r => r.tradingsymbol === 'CRUDEOIL26OCTFUT');
    const shortAOut  = out.find(r => r.tradingsymbol === 'CRUDEOIL26NOVFUT');
    const shortBOut  = out.find(r => r.tradingsymbol === 'CRUDEOIL26DECFUT');

    // Long row spans 2 groups -> split into 2 pieces (bigger match ranked P1).
    expect(longPieces).toHaveLength(2);
    const byLabel = Object.fromEntries(longPieces.map(p => [p.pair_group_key, p]));
    expect(byLabel['P1'].quantity).toBe(200);
    expect(byLabel['P2'].quantity).toBe(100);
    expect(byLabel['P1']._isLotSplit).toBe(true);
    expect(byLabel['P2']._isLotSplit).toBe(true);

    // Both shorts remain correctly paired (not orphaned), each a clean
    // single-piece match with the SAME label the long's corresponding
    // piece carries.
    expect(shortAOut.pair_group_key).toBe('P1');
    expect(shortAOut.is_orphan).toBe(false);
    expect(shortBOut.pair_group_key).toBe('P2');
    expect(shortBOut.is_orphan).toBe(false);
  });
});

describe('splitPairedLots — partial match with leftover', () => {
  it('a row partially matched leaves a matched piece + an orphan piece', () => {
    const longRow = row({ tradingsymbol: 'NIFTY26JUN24000CE', quantity: 150 });
    const shortRow = row({ tradingsymbol: 'NIFTY26JUN24500CE', quantity: -100 });

    const out = splitPairedLots([longRow, shortRow]);

    const longPieces = out.filter(r => r.tradingsymbol === 'NIFTY26JUN24000CE');
    expect(longPieces).toHaveLength(2);
    const matched = longPieces.find(p => p.pair_group_key === 'P1');
    const orphan  = longPieces.find(p => p.is_orphan);
    expect(matched.quantity).toBe(100);
    expect(matched.is_orphan).toBe(false);
    expect(orphan.quantity).toBe(50);
    expect(orphan.pair_group_key).toBeNull();
    expect(orphan.lot_piece_label).toBe('Unmatched');

    const shortOut = out.find(r => r.tradingsymbol === 'NIFTY26JUN24500CE');
    expect(shortOut.pair_group_key).toBe('P1');
    expect(shortOut.is_orphan).toBe(false);
  });
});

describe('splitPairedLots — pro-rating invariant', () => {
  it('pnl/realised/unrealised/day_change_val sum across pieces equal the original row', () => {
    const longRow = row({
      tradingsymbol: 'CRUDEOIL26OCTFUT', quantity: 300,
      pnl: 1234.56, realised: 500.11, unrealised: 734.45, day_change_val: 999.99,
      prev_settlement_pnl: 222.22, overnight_quantity: 300,
    });
    const shortA = row({ tradingsymbol: 'CRUDEOIL26NOVFUT', quantity: -200 });
    const shortB = row({ tradingsymbol: 'CRUDEOIL26DECFUT', quantity: -100 });

    const out = splitPairedLots([longRow, shortA, shortB]);
    const pieces = out.filter(r => r.tradingsymbol === 'CRUDEOIL26OCTFUT');
    expect(pieces).toHaveLength(2);

    for (const f of ['pnl', 'realised', 'unrealised', 'day_change_val', 'prev_settlement_pnl', 'overnight_quantity']) {
      const sum = pieces.reduce((s, p) => s + Number(p[f]), 0);
      expect(sum).toBeCloseTo(longRow[f], 6);
    }
    // qty sums back exactly too
    expect(pieces.reduce((s, p) => s + p.quantity, 0)).toBe(300);
  });
});

describe('splitPairedLots — CE/PE cross-pairing bug (2026-10 fix, parity with backend test_auto_pair_ce_never_pairs_with_pe_same_root_same_account)', () => {
  // Fixture shared with backend/tests/test_positions_auto_pair.py's
  // CE/PE-aware grouping tests — same account string, same tradingsymbols,
  // same quantities, so a human can compare the two test files side by
  // side. NOTE: these specific symbols (no expiry month token) don't parse
  // via `decomposeSymbol` as real F&O contracts — they exercise this
  // module's fallback root/optType derivation (mirrors the backend's own
  // suffix-only `_root_symbol`/`_option_type`), not the primary
  // `decomposeSymbol` path. See the CE/PE-cross-pair test further below
  // for the same guarantee on a real Kite-format symbol.
  it('Case 1 — long CE + short PE, same root/account, equal magnitude, must NOT pair', () => {
    const ceRow = row({ account: 'ZG0001', tradingsymbol: 'NIFTY24000CE', quantity: 30 });
    const peRow = row({ account: 'ZG0001', tradingsymbol: 'NIFTY27000PE', quantity: -30 });

    const out = splitPairedLots([ceRow, peRow]);

    expect(out).toHaveLength(2);
    const ceOut = out.find(r => r.tradingsymbol === 'NIFTY24000CE');
    const peOut = out.find(r => r.tradingsymbol === 'NIFTY27000PE');
    expect(ceOut.is_orphan).toBe(true);
    expect(peOut.is_orphan).toBe(true);
    expect(ceOut.pair_group_key).toBeNull();
    expect(peOut.pair_group_key).toBeNull();
    expect(ceOut.orphan_qty).toBe(30);
    expect(peOut.orphan_qty).toBe(30);
  });

  it('Case 2 — long CE + short CE, same root/account, same option type, must still pair', () => {
    const longRow = row({ account: 'ZG0001', tradingsymbol: 'NIFTY24000CE', quantity: 100 });
    const shortRow = row({ account: 'ZG0001', tradingsymbol: 'NIFTY24500CE', quantity: -100 });

    const out = splitPairedLots([longRow, shortRow]);

    expect(out).toHaveLength(2);
    const longOut = out.find(r => r.tradingsymbol === 'NIFTY24000CE');
    const shortOut = out.find(r => r.tradingsymbol === 'NIFTY24500CE');
    expect(longOut.pair_group_key).toBe('P1');
    expect(shortOut.pair_group_key).toBe('P1');
    expect(longOut.is_orphan).toBe(false);
    expect(shortOut.is_orphan).toBe(false);
    expect(longOut.paired_qty).toBe(100);
    expect(shortOut.paired_qty).toBe(100);
    expect(longOut.orphan_qty).toBe(0);
    expect(shortOut.orphan_qty).toBe(0);
  });

  it('Case 3 — long PE + short PE, same root/account, same option type, must still pair', () => {
    const longRow = row({ account: 'ZG0001', tradingsymbol: 'NIFTY25000PE', quantity: 50 });
    const shortRow = row({ account: 'ZG0001', tradingsymbol: 'NIFTY25500PE', quantity: -50 });

    const out = splitPairedLots([longRow, shortRow]);

    expect(out).toHaveLength(2);
    const longOut = out.find(r => r.tradingsymbol === 'NIFTY25000PE');
    const shortOut = out.find(r => r.tradingsymbol === 'NIFTY25500PE');
    expect(longOut.pair_group_key).toBe('P1');
    expect(shortOut.pair_group_key).toBe('P1');
    expect(longOut.is_orphan).toBe(false);
    expect(shortOut.is_orphan).toBe(false);
    expect(longOut.paired_qty).toBe(50);
    expect(shortOut.paired_qty).toBe(50);
    expect(longOut.orphan_qty).toBe(0);
    expect(shortOut.orphan_qty).toBe(0);
  });

  it('futures (no CE/PE suffix) are unaffected by the option-type grouping dimension', () => {
    const longRow = row({ account: 'ZG0001', tradingsymbol: 'NIFTY24JUNFUT', quantity: 20 });
    const shortRow = row({ account: 'ZG0001', tradingsymbol: 'NIFTY24JULFUT', quantity: -20 });

    const out = splitPairedLots([longRow, shortRow]);

    expect(out).toHaveLength(2);
    const longOut = out.find(r => r.tradingsymbol === 'NIFTY24JUNFUT');
    const shortOut = out.find(r => r.tradingsymbol === 'NIFTY24JULFUT');
    expect(longOut.pair_group_key).toBe('P1');
    expect(shortOut.pair_group_key).toBe('P1');
    expect(longOut.is_orphan).toBe(false);
    expect(shortOut.is_orphan).toBe(false);
    expect(longOut.paired_qty).toBe(20);
    expect(shortOut.paired_qty).toBe(20);
  });

  it('a real Kite-format CE and PE of the same root/account/magnitude do not cross-pair (primary decomposeSymbol path, not the fallback)', () => {
    const ceRow = row({ account: 'ZG0001', tradingsymbol: 'NIFTY26JUN24000CE', quantity: 30 });
    const peRow = row({ account: 'ZG0001', tradingsymbol: 'NIFTY26JUN27000PE', quantity: -30 });

    const out = splitPairedLots([ceRow, peRow]);

    expect(out).toHaveLength(2);
    const ceOut = out.find(r => r.tradingsymbol === 'NIFTY26JUN24000CE');
    const peOut = out.find(r => r.tradingsymbol === 'NIFTY26JUN27000PE');
    expect(ceOut.is_orphan).toBe(true);
    expect(peOut.is_orphan).toBe(true);
    expect(ceOut.pair_group_key).toBeNull();
    expect(peOut.pair_group_key).toBeNull();
  });
});

describe('splitPairedLots — order independence (stability)', () => {
  it('produces identical labels regardless of input array order', () => {
    const longRow = row({ tradingsymbol: 'CRUDEOIL26OCTFUT', quantity: 300, account: 'ACC1' });
    const shortA  = row({ tradingsymbol: 'CRUDEOIL26NOVFUT', quantity: -200, account: 'ACC1' });
    const shortB  = row({ tradingsymbol: 'CRUDEOIL26DECFUT', quantity: -100, account: 'ACC1' });

    const outA = splitPairedLots([longRow, shortA, shortB]);
    const outB = splitPairedLots([shortB, longRow, shortA]);

    const labelsA = Object.fromEntries(
      outA.map(r => [`${r.tradingsymbol}:${r.quantity}`, r.pair_group_key])
    );
    const labelsB = Object.fromEntries(
      outB.map(r => [`${r.tradingsymbol}:${r.quantity}`, r.pair_group_key])
    );
    expect(labelsB).toEqual(labelsA);
  });

  it('breaks ties deterministically by symbol when two match events have equal qty', () => {
    // longs [100, 100] vs shorts [101, 99] — tie-qty structure exercised
    // via the tie-break-by-symbol rule in both match order and labelling.
    const l1 = row({ tradingsymbol: 'NIFTY26JUNAAA', quantity: 100, account: 'ACC1' });
    const l2 = row({ tradingsymbol: 'NIFTY26JUNBBB', quantity: 100, account: 'ACC1' });
    const s1 = row({ tradingsymbol: 'NIFTY26JUNCCC', quantity: -101, account: 'ACC1' });
    const s2 = row({ tradingsymbol: 'NIFTY26JUNDDD', quantity: -99, account: 'ACC1' });

    const outFwd = splitPairedLots([l1, l2, s1, s2]);
    const outRev = splitPairedLots([s2, s1, l2, l1]);

    const keyFn = (r) => `${r.tradingsymbol}:${r.quantity}`;
    const labelsFwd = Object.fromEntries(outFwd.map(r => [keyFn(r), r.pair_group_key]));
    const labelsRev = Object.fromEntries(outRev.map(r => [keyFn(r), r.pair_group_key]));
    expect(labelsRev).toEqual(labelsFwd);
  });
});
