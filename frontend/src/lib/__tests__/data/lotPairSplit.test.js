import { describe, it, expect } from 'vitest';
import { splitPairedLots, consolidateLotSplitPieces, mergedLegPieces } from '../../data/lotPairSplit.js';
import { baseDayPnlForPosition, isBaselineGap } from '../../data/nav.js';

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
    baseline_stale: false,
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

  // 2026-11 6D follow-up (frontend mirror of pnl_math.baseline_
  // gap_day_pnl_substitute): `baseline_stale` is a boolean flag, not in
  // `_PRORATE_FIELDS`, so every lot-split piece inherits it verbatim from
  // the original row (unlike `pnl`, which IS prorated, remainder-based, so
  // Σ piece.pnl === original.pnl exactly). Because `baseDayPnlForPosition`
  // reads each piece's own (prorated) `pnl` as the gap substitute, this
  // already conserves correctly with NO additional fix needed in this
  // module — this test documents and pins that fact.
  it('baseline_stale rows: baseDayPnlForPosition sums across pieces to the original gap-row substitute, and every piece stays flagged stale', () => {
    const longRow = row({
      tradingsymbol: 'CRUDEOIL26OCTFUT', quantity: 30,
      pnl: 900, realised: 0, unrealised: 0,
      baseline_stale: true,
    });
    const shortA = row({ tradingsymbol: 'CRUDEOIL26NOVFUT', quantity: -10 });
    const shortB = row({ tradingsymbol: 'CRUDEOIL26DECFUT', quantity: -5 });

    expect(isBaselineGap(longRow)).toBe(true);
    const originalDayPnl = baseDayPnlForPosition(longRow);
    expect(originalDayPnl).toBe(900); // substitute = row's own pnl, never a hard 0

    const out = splitPairedLots([longRow, shortA, shortB]);
    const pieces = out.filter(r => r.tradingsymbol === 'CRUDEOIL26OCTFUT');
    // 30 qty: 10 matched + 5 matched + 15 leftover/orphan → 3 genuine pieces.
    expect(pieces).toHaveLength(3);

    for (const p of pieces) {
      expect(p.baseline_stale).toBe(true); // verbatim passthrough, never prorated
      expect(isBaselineGap(p)).toBe(true);
    }

    const summed = pieces.reduce((s, p) => s + baseDayPnlForPosition(p), 0);
    expect(summed).toBeCloseTo(originalDayPnl, 6);
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

describe('consolidateLotSplitPieces — the exact inverse of splitPairedLots (Legs-view consolidation)', () => {
  it('round-trips splitPairedLots output back to one row per original position (qty + every _PRORATE_FIELDS entry sums exactly)', () => {
    const longRow = row({
      tradingsymbol: 'CRUDEOIL26OCTFUT', quantity: 300, account: 'ACC1', source: 'live',
      pnl: 1234.56, realised: 500.11, unrealised: 734.45, day_change_val: 999.99,
      prev_settlement_pnl: 222.22, overnight_quantity: 300, day_pnl: 111.11, lots: 3,
    });
    const shortA = row({ tradingsymbol: 'CRUDEOIL26NOVFUT', quantity: -200, account: 'ACC1', source: 'live' });
    const shortB = row({ tradingsymbol: 'CRUDEOIL26DECFUT', quantity: -100, account: 'ACC1', source: 'live' });

    const split = splitPairedLots([longRow, shortA, shortB]);
    const out = consolidateLotSplitPieces(split);

    // The long row produced 2 pieces in `split`; consolidation must
    // collapse them back to exactly 1 — total row count: 1 (merged long)
    // + 2 (unsplit shorts) = 3, down from `split`'s 4.
    expect(split).toHaveLength(4);
    expect(out).toHaveLength(3);

    const merged = out.find((r) => r.tradingsymbol === 'CRUDEOIL26OCTFUT');
    expect(merged).toBeTruthy();
    expect(merged.quantity).toBe(300);
    for (const f of ['pnl', 'realised', 'unrealised', 'day_change_val', 'prev_settlement_pnl', 'overnight_quantity', 'day_pnl', 'lots']) {
      expect(merged[f]).toBeCloseTo(longRow[f], 6);
    }

    // Shorts pass through unmerged (each only ever had ONE piece).
    const shortAOut = out.find((r) => r.tradingsymbol === 'CRUDEOIL26NOVFUT');
    const shortBOut = out.find((r) => r.tradingsymbol === 'CRUDEOIL26DECFUT');
    expect(shortAOut.quantity).toBe(-200);
    expect(shortBOut.quantity).toBe(-100);
  });

  it('a never-split row passes through unchanged (same reference), at its original position', () => {
    const a = row({ tradingsymbol: 'A', quantity: 10 });
    const b = row({ tradingsymbol: 'B', quantity: -10 });
    const out = consolidateLotSplitPieces([a, b]);
    expect(out).toHaveLength(2);
    expect(out[0]).toBe(a);
    expect(out[1]).toBe(b);
  });

  it('a mixed array of split + unsplit rows only merges the split ones', () => {
    const longRow = row({ tradingsymbol: 'NIFTY26JUN24000CE', quantity: 150, account: 'ACC1', source: 'live' });
    const shortRow = row({ tradingsymbol: 'NIFTY26JUN24500CE', quantity: -100, account: 'ACC1', source: 'live' });
    const untouched = row({ tradingsymbol: 'BANKNIFTY26JUN45000PE', quantity: 50, account: 'ACC2', source: 'live' });

    const split = splitPairedLots([longRow, shortRow, untouched]);
    const out = consolidateLotSplitPieces(split);

    // longRow (150) vs shortRow (100) -> matched piece + orphan piece (2
    // pieces for the long); the ACC2 row never participates in matching
    // (different account), so it's merely a lone, unmatched single-piece
    // row — splitPairedLots' OWN single-piece branch still corrects its
    // is_orphan/orphan_qty fields via a copy (it's alone in its group,
    // so it's a 100% orphan), but never sets `_isLotSplit` on it.
    expect(split.filter((r) => r.tradingsymbol === 'NIFTY26JUN24000CE')).toHaveLength(2);
    const mergedLong = out.find((r) => r.tradingsymbol === 'NIFTY26JUN24000CE');
    expect(mergedLong.quantity).toBe(150);
    const shortOut = out.find((r) => r.tradingsymbol === 'NIFTY26JUN24500CE');
    expect(shortOut.quantity).toBe(-100);
    const splitUntouched = split.find((r) => r.tradingsymbol === 'BANKNIFTY26JUN45000PE');
    expect(splitUntouched._isLotSplit).toBeUndefined();
    const untouchedOut = out.find((r) => r.tradingsymbol === 'BANKNIFTY26JUN45000PE');
    expect(untouchedOut).toBe(splitUntouched); // genuinely unmerged — same reference as splitPairedLots' own output
  });

  it('pairing-only fields are absent on the merged row', () => {
    const longRow = row({ tradingsymbol: 'CRUDEOIL26OCTFUT', quantity: 300, account: 'ACC1', source: 'live' });
    const shortA = row({ tradingsymbol: 'CRUDEOIL26NOVFUT', quantity: -200, account: 'ACC1', source: 'live' });
    const shortB = row({ tradingsymbol: 'CRUDEOIL26DECFUT', quantity: -100, account: 'ACC1', source: 'live' });

    const out = consolidateLotSplitPieces(splitPairedLots([longRow, shortA, shortB]));
    const merged = out.find((r) => r.tradingsymbol === 'CRUDEOIL26OCTFUT');

    for (const f of ['pair_group_key', 'paired_qty', 'orphan_qty', 'is_orphan', '_isLotSplit', 'lot_piece_label', '_lotFraction']) {
      expect(f in merged).toBe(false);
    }
    expect(Array.isArray(merged._mergedPieceKeys)).toBe(true);
    expect(merged._mergedPieceKeys.sort()).toEqual(['P1', 'P2']);
  });

  it('restores the pre-split `key` by stripping the `::<label>` suffix', () => {
    const longRow = row({ tradingsymbol: 'CRUDEOIL26OCTFUT', quantity: 300, account: 'ACC1', source: 'live' });
    const shortA = row({ tradingsymbol: 'CRUDEOIL26NOVFUT', quantity: -200, account: 'ACC1', source: 'live' });
    const shortB = row({ tradingsymbol: 'CRUDEOIL26DECFUT', quantity: -100, account: 'ACC1', source: 'live' });

    const split = splitPairedLots([longRow, shortA, shortB]);
    const piece = split.find((r) => r.tradingsymbol === 'CRUDEOIL26OCTFUT' && r.pair_group_key === 'P1');
    expect(piece.key).toBe('CRUDEOIL26OCTFUT::P1'); // sanity on splitPairedLots' own shape

    const merged = consolidateLotSplitPieces(split).find((r) => r.tradingsymbol === 'CRUDEOIL26OCTFUT');
    expect(merged.key).toBe('CRUDEOIL26OCTFUT');
  });

  // The next two tests construct already-split pieces BY HAND (bypassing
  // `splitPairedLots`'s real matching, which groups purely by (account,
  // root, optType) with no `_splitTag`/`_storeKey` dimension and would
  // make a realistic multi-row fixture for these two cases needlessly
  // hard to control) — valid since `consolidateLotSplitPieces` is a
  // standalone pure function whose only input contract is "a row with
  // `_isLotSplit === true`", regardless of how it got that way.
  it('a different _splitTag ("closed" vs "open") on otherwise-identical pieces never merges across them', () => {
    const closedP1 = row({ tradingsymbol: 'SYM1', quantity: 60, account: 'ACC1', source: 'live', _splitTag: 'closed', _isLotSplit: true, lot_piece_label: 'P1', pair_group_key: 'P1' });
    const closedP2 = row({ tradingsymbol: 'SYM1', quantity: 40, account: 'ACC1', source: 'live', _splitTag: 'closed', _isLotSplit: true, lot_piece_label: 'P2', pair_group_key: 'P2' });
    const openP1   = row({ tradingsymbol: 'SYM1', quantity: 60, account: 'ACC1', source: 'live', _splitTag: 'open',   _isLotSplit: true, lot_piece_label: 'P1', pair_group_key: 'P1' });
    const openP2   = row({ tradingsymbol: 'SYM1', quantity: 40, account: 'ACC1', source: 'live', _splitTag: 'open',   _isLotSplit: true, lot_piece_label: 'P2', pair_group_key: 'P2' });

    const out = consolidateLotSplitPieces([closedP1, closedP2, openP1, openP2]);

    const sym1Rows = out.filter((r) => r.tradingsymbol === 'SYM1');
    expect(sym1Rows).toHaveLength(2); // one merged row per _splitTag, never collapsed into one
    expect(sym1Rows.every((r) => r.quantity === 100)).toBe(true);
    expect(new Set(sym1Rows.map((r) => r._splitTag))).toEqual(new Set(['closed', 'open']));
  });

  it('a different _storeKey (e.g. NRML vs MIS on the same account+symbol) never merges across them (deliberate deviation from the literal grouping key — see the module-level docstring)', () => {
    const nrmlP1 = row({ tradingsymbol: 'SYM1', quantity: 60, account: 'ACC1', source: 'live', _storeKey: 'ACC1|NFO|SYM1|NRML', _isLotSplit: true, lot_piece_label: 'P1', pair_group_key: 'P1' });
    const nrmlP2 = row({ tradingsymbol: 'SYM1', quantity: 40, account: 'ACC1', source: 'live', _storeKey: 'ACC1|NFO|SYM1|NRML', _isLotSplit: true, lot_piece_label: 'P2', pair_group_key: 'P2' });
    const misP1  = row({ tradingsymbol: 'SYM1', quantity: 60, account: 'ACC1', source: 'live', _storeKey: 'ACC1|NFO|SYM1|MIS',  _isLotSplit: true, lot_piece_label: 'P3', pair_group_key: 'P3' });
    const misP2  = row({ tradingsymbol: 'SYM1', quantity: 40, account: 'ACC1', source: 'live', _storeKey: 'ACC1|NFO|SYM1|MIS',  _isLotSplit: true, lot_piece_label: 'P4', pair_group_key: 'P4' });

    const out = consolidateLotSplitPieces([nrmlP1, nrmlP2, misP1, misP2]);

    const sym1Rows = out.filter((r) => r.tradingsymbol === 'SYM1');
    expect(sym1Rows).toHaveLength(2); // NRML's merged SYM1 and MIS's merged SYM1 stay distinct rows
    expect(sym1Rows.find((r) => r._storeKey === 'ACC1|NFO|SYM1|NRML').quantity).toBe(100);
    expect(sym1Rows.find((r) => r._storeKey === 'ACC1|NFO|SYM1|MIS').quantity).toBe(100);
  });
});

describe('mergedLegPieces — rebuild constituent pieces for checkbox-key purposes', () => {
  it('returns [] for an unmerged row', () => {
    const plain = row({ tradingsymbol: 'A', quantity: 10 });
    expect(mergedLegPieces(/** @type {any} */ (plain))).toEqual([]);
  });

  it('rebuilds each constituent piece with the merged row\'s own account/symbol/_splitTag, and each original piece\'s own lot_piece_label — tuples matching the pre-merge split pieces', () => {
    const longRow = row({ tradingsymbol: 'CRUDEOIL26OCTFUT', quantity: 300, account: 'ACC1', source: 'live' });
    const shortA = row({ tradingsymbol: 'CRUDEOIL26NOVFUT', quantity: -200, account: 'ACC1', source: 'live' });
    const shortB = row({ tradingsymbol: 'CRUDEOIL26DECFUT', quantity: -100, account: 'ACC1', source: 'live' });

    const split = splitPairedLots([longRow, shortA, shortB]);
    const originalPieces = split.filter((r) => r.tradingsymbol === 'CRUDEOIL26OCTFUT');
    const merged = consolidateLotSplitPieces(split).find((r) => r.tradingsymbol === 'CRUDEOIL26OCTFUT');

    const rebuilt = mergedLegPieces(merged);
    expect(rebuilt).toHaveLength(2);

    const tupleOf = (r) => `${r.account}|${r.symbol ?? r.tradingsymbol}|${r._splitTag ?? ''}|${r.lot_piece_label ?? ''}`;
    const originalTuples = new Set(originalPieces.map(tupleOf));
    const rebuiltTuples = new Set(rebuilt.map(tupleOf));
    expect(rebuiltTuples).toEqual(originalTuples);
  });

  it('rebuilt pieces never carry _mergedPieceKeys (no infinite-recursion risk for a caller that recurses through its own per-piece checked-state function)', () => {
    const longRow = row({ tradingsymbol: 'CRUDEOIL26OCTFUT', quantity: 300, account: 'ACC1', source: 'live' });
    const shortA = row({ tradingsymbol: 'CRUDEOIL26NOVFUT', quantity: -200, account: 'ACC1', source: 'live' });
    const shortB = row({ tradingsymbol: 'CRUDEOIL26DECFUT', quantity: -100, account: 'ACC1', source: 'live' });

    const merged = consolidateLotSplitPieces(splitPairedLots([longRow, shortA, shortB]))
      .find((r) => r.tradingsymbol === 'CRUDEOIL26OCTFUT');

    for (const piece of mergedLegPieces(merged)) {
      expect('_mergedPieceKeys' in piece).toBe(false);
      expect(mergedLegPieces(piece)).toEqual([]); // recursing one level further terminates
    }
  });
});

describe('splitPairedLots — global pair numbering (2026-10-17 fix, parity with backend test_auto_pair_labels_are_globally_sequential_not_reset_per_group)', () => {
  it('two unrelated pairs (different roots) get DIFFERENT labels, not both "P1"', () => {
    // Operator: "every group in legs and positions has p1... each group
    // should have a different value for st like p1, p2, p3".
    const bankLong  = row({ tradingsymbol: 'BANKNIFTY45000CE', quantity: 10, account: 'ZG0001' });
    const bankShort = row({ tradingsymbol: 'BANKNIFTY45500CE', quantity: -10, account: 'ZG0001' });
    const niftyLong  = row({ tradingsymbol: 'NIFTY24000CE', quantity: 5, account: 'ZG0001' });
    const niftyShort = row({ tradingsymbol: 'NIFTY24500CE', quantity: -5, account: 'ZG0001' });

    const out = splitPairedLots([bankLong, bankShort, niftyLong, niftyShort]);
    const byKey = (ts) => out.find(r => r.tradingsymbol === ts);

    expect(byKey('BANKNIFTY45000CE').pair_group_key).toBe(byKey('BANKNIFTY45500CE').pair_group_key);
    expect(byKey('NIFTY24000CE').pair_group_key).toBe(byKey('NIFTY24500CE').pair_group_key);
    expect(byKey('BANKNIFTY45000CE').pair_group_key).not.toBe(byKey('NIFTY24000CE').pair_group_key);
    // Groups sorted by (account, root, optType) — "BANKNIFTY" < "NIFTY"
    // alphabetically, so BANKNIFTY's pair is P1, NIFTY's is P2.
    expect(byKey('BANKNIFTY45000CE').pair_group_key).toBe('P1');
    expect(byKey('NIFTY24000CE').pair_group_key).toBe('P2');
  });

  it('global numbering is order-independent, matching the within-group order-independence guarantee', () => {
    const bankLong  = row({ tradingsymbol: 'BANKNIFTY45000CE', quantity: 10, account: 'ZG0001' });
    const bankShort = row({ tradingsymbol: 'BANKNIFTY45500CE', quantity: -10, account: 'ZG0001' });
    const niftyLong  = row({ tradingsymbol: 'NIFTY24000CE', quantity: 5, account: 'ZG0001' });
    const niftyShort = row({ tradingsymbol: 'NIFTY24500CE', quantity: -5, account: 'ZG0001' });

    const keyFn = (r) => `${r.tradingsymbol}:${r.quantity}`;
    const outFwd = splitPairedLots([bankLong, bankShort, niftyLong, niftyShort]);
    const outRev = splitPairedLots([niftyShort, niftyLong, bankShort, bankLong]);
    const labelsFwd = Object.fromEntries(outFwd.map(r => [keyFn(r), r.pair_group_key]));
    const labelsRev = Object.fromEntries(outRev.map(r => [keyFn(r), r.pair_group_key]));
    expect(labelsRev).toEqual(labelsFwd);
  });
});
