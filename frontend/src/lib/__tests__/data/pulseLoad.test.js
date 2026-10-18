import { describe, it, expect } from 'vitest';
import { collectUnderlyings } from '$lib/data/pulseLoad.js';
import { parseSymbol } from '$lib/data/pulseUnified.js';

/**
 * Regression guard for the 2026-10-18 fix: `collectUnderlyings`'s
 * `addUnderlying()` previously resolved an option's underlying via the
 * regex-only `parseDerivSymbol` exclusively, never consulting the
 * instruments cache — while the REAL option position rows get their
 * `underlying` field from `pulseUnified.js:parseSymbol()`, which prefers
 * `getInst(sym)?.u`. When Kite's instrument-master `u` value for a
 * commodity doesn't collapse to the exact same string the regex parses
 * (a documented class of mismatch — see `_MCX_LOT_OVERRIDES` in
 * `backend/api/routes/instruments.py`, e.g. "GOLD M" vs "GOLDM"), the
 * anchor row and its own CE/PE legs silently landed in TWO DIFFERENT
 * underlying groups — the anchor's own one-row "block" then anchored
 * wherever it happened to land in the pre-sort array, instead of
 * leading its options, even though the account+pair block-order sort
 * fix (929a772c) was otherwise working correctly.
 */
describe('collectUnderlyings — anchor underlying must match the real option rows\' own resolution', () => {
  it('prefers the instruments-cache underlying over the regex parse, matching parseSymbol()', () => {
    // Simulate a commodity whose instrument-cache `u` value differs from
    // what the bare regex parse of the option symbol would produce.
    const getInst = (sym) => {
      if (sym === 'GOLD26OCT149000CE') {
        return { u: 'GOLD', t: 'CE', k: 149000, x: '2026-10-27' };
      }
      return null;
    };
    const { underlyingInfos } = collectUnderlyings({
      pRows: [{ symbol: 'GOLD26OCT149000CE', exchange: 'MCX' }],
      hRows: [],
      activeLists: [],
      findNearestFut: () => null,
      listFuts: (root) => (root === 'GOLD' ? [{ s: 'GOLD26OCTFUT', e: 'MCX', x: '2026-10-19' }] : []),
      getInst,
    });

    // The real option row resolves its own `underlying` via parseSymbol,
    // using the exact same getInst — this is the ground truth the
    // anchor must agree with.
    const realRowUnderlying = parseSymbol('GOLD26OCT149000CE', getInst).underlying;
    expect(realRowUnderlying).toBe('GOLD');

    const anchor = [...underlyingInfos.values()][0];
    expect(anchor).toBeTruthy();
    expect(anchor.displayUnderlying).toBe(realRowUnderlying);
  });

  it('falls back to the regex-parsed underlying when the instruments cache is cold (no getInst)', () => {
    const { underlyingInfos } = collectUnderlyings({
      pRows: [{ symbol: 'CRUDEOIL26JUNFUT', exchange: 'MCX' }],
      hRows: [],
      activeLists: [],
      findNearestFut: (root) => ({ s: `${root}26JUNFUT`, e: 'MCX', x: '2026-06-17' }),
      listFuts: () => [],
      // getInst omitted entirely — cold-start path.
    });
    const anchor = [...underlyingInfos.values()][0];
    expect(anchor).toBeTruthy();
    expect(String(anchor.displayUnderlying || anchor.underlying_group).toUpperCase()).toBe('CRUDEOIL');
  });

  it('an equity-index option whose instrument-cache underlying differs from the regex parse resolves via the cache value', () => {
    // NIFTY weekly options sometimes carry a distinct instrument-master
    // name from the bare index root the regex would guess; using a
    // deliberately different (but still realistic-shaped) cache value
    // proves the cache wins outright, not just when it happens to agree.
    const getInst = (sym) =>
      sym === 'BANKNIFTY26JUN50000CE' ? { u: 'NIFTY BANK', t: 'CE', k: 50000, x: '2026-06-24' } : null;
    const { underlyingInfos } = collectUnderlyings({
      pRows: [{ symbol: 'BANKNIFTY26JUN50000CE', exchange: 'NFO' }],
      hRows: [],
      activeLists: [],
      findNearestFut: () => null,
      listFuts: () => [],
      getInst,
    });
    const realRowUnderlying = parseSymbol('BANKNIFTY26JUN50000CE', getInst).underlying;
    expect(realRowUnderlying).toBe('NIFTY BANK');

    const anchor = [...underlyingInfos.values()][0];
    expect(anchor).toBeTruthy();
    expect(String(anchor.displayUnderlying || anchor.underlying_group).toUpperCase()).toBe(realRowUnderlying);
  });
});
