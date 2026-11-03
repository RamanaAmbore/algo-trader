/**
 * liveWingBadge.test.js — unit coverage for `computeLiveWingState()`,
 * the pure resolver behind the Chain tab's read-only "wing fired live"
 * confirmation badge (`OptionChainTab.svelte`'s `_liveWingByKey`).
 *
 * Five quality dimensions:
 *  1. SSOT    — the resolver's wing discriminator is `source ===
 *               'template_wing'` AND `parent_order_id != null`, matching
 *               the ONE place the backend stamps that shape
 *               (`_chase_wing` in template_attach.py). A take-profit
 *               child decoy (same parent_order_id, source='take_profit')
 *               must never produce a badge — tested explicitly.
 *  2. Scope   — a wing symbol must resolve to the SAME instrument the
 *               on-screen underlying+expiry's `findOption` would pick;
 *               a mismatched scope must not badge the cell.
 *  3. Reuse   — `decompose` is injected (the real decomposeSymbol in
 *               production), never reimplemented inline here.
 *  4. Chase   — a parent whose broker_order_id changed (cancel-and-
 *               replace) still resolves via `superseded_broker_order_ids`.
 *  5. Prune   — unmatched entries expire on a TTL; a resolved-but-dead
 *               (rejected/cancelled) wing is dropped immediately, not
 *               on the TTL.
 */
import { describe, it, expect } from 'vitest';
import { computeLiveWingState, WING_TRACK_PRUNE_MS } from '../../data/liveWingBadge.js';
import { decomposeSymbol } from '../../data/decomposeSymbol.js';

const NOW = 1_800_000_000_000;

/** Minimal findOption stub — strike/optType -> tradingsymbol, scoped to
 *  one underlying+expiry pair, mirroring the real instruments-cache
 *  lookup's contract (returns null for anything out of scope). */
function makeFindOption(scopeMap) {
  return (underlying, optType, strike, expiry) => {
    const key = `${underlying}|${expiry}|${strike}:${optType}`;
    const sym = scopeMap[key];
    return sym ? { s: sym, ls: 50 } : null;
  };
}

function makeGetInstrument(lotSizes) {
  return (sym) => (lotSizes[sym] != null ? { ls: lotSizes[sym] } : null);
}

describe('computeLiveWingState', () => {
  const trackedParent = {
    account: 'ZG0790', symbol: 'NIFTY26NOV23000CE',
    brokerOrderId: '2200000000001', templateName: 'Bracket-1',
    submittedAt: NOW, lastSeenAt: NOW,
  };

  const parentRow = {
    id: 501, account: 'ZG0790', symbol: 'NIFTY26NOV23000CE',
    broker_order_id: '2200000000001', superseded_broker_order_ids: [],
    source: null, parent_order_id: null, status: 'OPEN',
  };

  const wingRow = {
    id: 502, account: 'ZG0790', symbol: 'NIFTY26NOV22600PE',
    parent_order_id: 501, source: 'template_wing',
    transaction_type: 'SELL', quantity: 50, status: 'OPEN',
  };

  const findOption = makeFindOption({
    'NIFTY|2026-11-26|22600:PE': 'NIFTY26NOV22600PE',
  });
  const getInstrument = makeGetInstrument({ NIFTY26NOV22600PE: 50 });

  it('matches a chase-routed wing row and builds the badge at the right strike:optType key', () => {
    const { badgeMap, trackedParents } = computeLiveWingState({
      trackedParents: [trackedParent],
      algoRows: [parentRow, wingRow],
      chainUnderlying: 'NIFTY', chainExpiry: '2026-11-26',
      decompose: decomposeSymbol, findOption, getInstrument, nowMs: NOW,
    });
    const badge = badgeMap.get('22600:PE');
    expect(badge).toBeTruthy();
    expect(badge.lots).toBe(1); // quantity 50 / lotSize 50
    expect(badge.side).toBe('SELL');
    expect(badge.templateName).toBe('Bracket-1');
    expect(badge.algoOrderId).toBe(502);
    expect(badge.status).toBe('OPEN');
    // Matched entry survives (not pruned) and is refreshed.
    expect(trackedParents).toHaveLength(1);
    expect(trackedParents[0].lastSeenAt).toBe(NOW);
  });

  it('never badges a take-profit child sharing the same parent_order_id (SSOT discriminator)', () => {
    const tpDecoy = {
      id: 503, account: 'ZG0790', symbol: 'NIFTY26NOV23000CE', // parent's OWN symbol
      parent_order_id: 501, source: 'take_profit',
      transaction_type: 'SELL', quantity: 75, status: 'OPEN',
    };
    const { badgeMap } = computeLiveWingState({
      trackedParents: [trackedParent],
      algoRows: [parentRow, tpDecoy],
      chainUnderlying: 'NIFTY', chainExpiry: '2026-11-26',
      decompose: decomposeSymbol, findOption, getInstrument, nowMs: NOW,
    });
    expect(badgeMap.size).toBe(0);
  });

  it('never badges a wing whose resolved contract is out of scope (wrong underlying/expiry on screen)', () => {
    const { badgeMap } = computeLiveWingState({
      trackedParents: [trackedParent],
      algoRows: [parentRow, wingRow],
      // Looking at a DIFFERENT expiry — findOption returns null for it.
      chainUnderlying: 'NIFTY', chainExpiry: '2026-12-31',
      decompose: decomposeSymbol, findOption, getInstrument, nowMs: NOW,
    });
    expect(badgeMap.size).toBe(0);
  });

  it('resolves the parent via superseded_broker_order_ids after a chase cancel-and-replace', () => {
    const chasedParentRow = {
      ...parentRow,
      broker_order_id: '2200000000999', // re-quoted, new broker id
      superseded_broker_order_ids: ['2200000000001'], // original id, superseded
    };
    const { badgeMap } = computeLiveWingState({
      trackedParents: [trackedParent],
      algoRows: [chasedParentRow, wingRow],
      chainUnderlying: 'NIFTY', chainExpiry: '2026-11-26',
      decompose: decomposeSymbol, findOption, getInstrument, nowMs: NOW,
    });
    expect(badgeMap.get('22600:PE')).toBeTruthy();
  });

  it('drops a tracked parent immediately once its wing resolves to a dead status (rejected/cancelled)', () => {
    const deadWing = { ...wingRow, status: 'REJECTED' };
    const { badgeMap, trackedParents } = computeLiveWingState({
      trackedParents: [trackedParent],
      algoRows: [parentRow, deadWing],
      chainUnderlying: 'NIFTY', chainExpiry: '2026-11-26',
      decompose: decomposeSymbol, findOption, getInstrument, nowMs: NOW,
    });
    expect(badgeMap.size).toBe(0);
    expect(trackedParents).toHaveLength(0);
  });

  it('prunes an unmatched tracked entry once it exceeds WING_TRACK_PRUNE_MS with no activity', () => {
    const stale = { ...trackedParent, submittedAt: NOW - WING_TRACK_PRUNE_MS - 1, lastSeenAt: NOW - WING_TRACK_PRUNE_MS - 1 };
    const { badgeMap, trackedParents } = computeLiveWingState({
      trackedParents: [stale],
      algoRows: [], // parent not even visible in the poll window any more
      chainUnderlying: 'NIFTY', chainExpiry: '2026-11-26',
      decompose: decomposeSymbol, findOption, getInstrument, nowMs: NOW,
    });
    expect(badgeMap.size).toBe(0);
    expect(trackedParents).toHaveLength(0);
  });

  it('keeps a tracked entry alive past the original TTL as long as the wing keeps matching', () => {
    const almostStale = { ...trackedParent, submittedAt: NOW - WING_TRACK_PRUNE_MS - 1, lastSeenAt: NOW - 1000 };
    const { trackedParents } = computeLiveWingState({
      trackedParents: [almostStale],
      algoRows: [parentRow, wingRow],
      chainUnderlying: 'NIFTY', chainExpiry: '2026-11-26',
      decompose: decomposeSymbol, findOption, getInstrument, nowMs: NOW,
    });
    expect(trackedParents).toHaveLength(1);
    expect(trackedParents[0].lastSeenAt).toBe(NOW);
  });

  it('is a no-op (empty badge map, untouched tracked list) with no algoRows at all', () => {
    const { badgeMap, trackedParents } = computeLiveWingState({
      trackedParents: [trackedParent],
      algoRows: [],
      chainUnderlying: 'NIFTY', chainExpiry: '2026-11-26',
      decompose: decomposeSymbol, findOption, getInstrument, nowMs: NOW,
    });
    expect(badgeMap.size).toBe(0);
    expect(trackedParents).toHaveLength(1); // still within TTL, kept
  });
});
