import { describe, it, expect } from 'vitest';
import { pairGroupSort } from '../../data/pairGroupSort.js';
import { postSortGroups2Level } from '../../data/pulseGridSetup.js';

/**
 * Regression guard for PerformancePage.svelte's Positions grid
 * `postSortRows` composition — the same bug class fixed in MarketPulse's
 * `_pairGroupPostSort` (2026-10-17, "gold is showing below, it should
 * be the first one as an underlying"), found via self-audit while
 * verifying that fix.
 *
 * PerformancePage wired: `(params) => { pairGroupSort(params.nodes);
 * postSortGroups2Level(params); }`. `pairGroupSort` runs FIRST and
 * re-sorts the whole row set by `${account}|${pair_group_key}` with no
 * regard for `underlying`, scrambling whatever block order the data
 * arrived in. `postSortGroups2Level` then anchors each underlying's
 * block position on `nodes.indexOf(byUnderlying.get(u)[0])` — reading
 * the ALREADY-SCRAMBLED order — so the true upstream block order is
 * unrecoverable by the time it runs. `postSortGroups2Level`'s own
 * intra-block sort (FUT/EQ → CE → PE, then expiry/strike) also fully
 * overwrites whatever intra-block order `pairGroupSort` produced, so
 * calling `pairGroupSort` first achieves nothing except corrupting the
 * block anchors — it should not be called at all in this composition.
 */
function makeNode(tradingsymbol, underlying, account, overrides = {}) {
  return { data: { tradingsymbol, underlying, account, ...overrides } };
}

function makeParams(nodes) {
  return /** @type {any} */ ({
    nodes,
    api: { getColumnState: () => [{ sort: null }] },
  });
}

describe('PerformancePage Positions grid postSortRows composition', () => {
  it('BUG (pre-fix composition): an underlying can jump ahead purely because its account/pair key sorts alphabetically earlier', () => {
    // CRUDEOIL arrives first (its block should stay first); NIFTY's
    // account ("AAAA0001") sorts alphabetically before CRUDEOIL's
    // ("ZZZZ9999") — exactly the GOLD-style symptom.
    const crude = makeNode('CRUDEOIL26OCTFUT', 'CRUDEOIL', 'ZZZZ9999');
    const nifty = makeNode('NIFTY26JUNFUT', 'NIFTY', 'AAAA0001');
    const nodes = [crude, nifty];

    const params = makeParams(nodes);
    pairGroupSort(params.nodes);       // the buggy first pass
    postSortGroups2Level(params);      // reads the already-scrambled order

    // This assertion captures the BUG: NIFTY (alphabetically-earlier
    // account) has jumped ahead of CRUDEOIL, which arrived first.
    expect(nodes[0]).toBe(nifty);
    expect(nodes[1]).toBe(crude);
  });

  it('FIX: calling postSortGroups2Level alone (no pairGroupSort pass) preserves true arrival block order', () => {
    const crude = makeNode('CRUDEOIL26OCTFUT', 'CRUDEOIL', 'ZZZZ9999');
    const nifty = makeNode('NIFTY26JUNFUT', 'NIFTY', 'AAAA0001');
    const nodes = [crude, nifty];

    const params = makeParams(nodes);
    postSortGroups2Level(params);      // no pairGroupSort pass first

    expect(nodes[0]).toBe(crude);
    expect(nodes[1]).toBe(nifty);
  });

  it('FIX: intra-block CE/PE/expiry ordering from postSortGroups2Level is preserved untouched (pairGroupSort would have overwritten it anyway, so dropping it loses nothing)', () => {
    const ce = makeNode('NIFTY26JUNFUT', 'NIFTY', 'A1', { opt_type: 'CE', expiry: '26JUN', strike: 25000 });
    const pe = makeNode('NIFTY26JUNFUT', 'NIFTY', 'A1', { opt_type: 'PE', expiry: '26JUN', strike: 25000 });
    const fut = makeNode('NIFTY26JUNFUT', 'NIFTY', 'A1', { opt_type: null, expiry: '26JUN', strike: 0 });
    // Deliberately out of order on input.
    const nodes = [pe, ce, fut];

    const params = makeParams(nodes);
    postSortGroups2Level(params);

    expect(nodes[0]).toBe(fut);
    expect(nodes[1]).toBe(ce);
    expect(nodes[2]).toBe(pe);
  });
});
