/**
 * ordersStore.test.js — coverage for the shared `ordersStore` singleton
 * added to marketDataStores.svelte.js (PLAN.md Part B, item B1).
 *
 * B1 promotes OrderBook.svelte's own pre-B1 `_loadOrders()` merge logic
 * (fetchOrders()+fetchAlgoOrdersRecent() dedup, chase cancel-and-replace
 * supersede tracking, the session-boundary filter) into a shared
 * `createDataStore` singleton, so a reload paints last-good rows instead
 * of "Loading…" while the background poll catches up.
 *
 * marketDataStores.svelte.js has top-level `$state(...)` calls (e.g.
 * `_bookPollerTick`, `_moversSnapshotAt`), so it can't be imported
 * directly in this harness (no svelte-compiler plugin in
 * vitest.config.js) — same constraint documented in
 * marketDataStoresMeta.test.js, mirrored here with the same two-part
 * coverage shape:
 *  1. A pure-function mirror of `_fetchOrdersMerged`'s synchronous merge
 *     logic (dedup, supersede filtering, session-boundary filter),
 *     exercised directly against representative broker/algo row shapes.
 *  2. A source-scan (Vite `?raw` import) confirming the real shipped
 *     file wires the fetcher-must-throw contract (the actual fix this
 *     store exists for — OrderBook.svelte's own pre-B1 code used
 *     `Promise.allSettled` and silently froze by hand on a rejected
 *     broker leg instead of surfacing the failure to createDataStore's
 *     shared keep-last-good path) and the `ordersStore` declaration.
 *
 * Five quality dimensions:
 *  1. SSOT   — source-scan reads the real shipped file, not a stale copy.
 *  2. Perf   — pure functions + one string read, no I/O.
 *  3. Stale  — the broker-leg-must-throw case is tested explicitly as the
 *              actual regression class this store closes.
 *  4. Reuse  — one merge mirror exercised against dedup, supersede, and
 *              session-boundary cases — the same three concerns
 *              OrderBook.svelte's pre-B1 code hand-rolled independently.
 *  5. UX     — a frozen last-good order book on a broker outage must
 *              never silently collapse to [] — the exact bug class B1
 *              exists to fix (mirrors OrderBook.svelte's own pre-existing
 *              "cancelled orders showing as OPEN again" audit-fix intent,
 *              now routed through the shared store instead of duplicated).
 */

import { describe, it, expect } from 'vitest';
import src from '$lib/data/marketDataStores.svelte.js?raw';

// ── Pure mirrors of the three merge concerns _fetchOrdersMerged owns
// (kept in lockstep with the real implementation — see the source-scan
// section below for the fetcher-must-throw contract itself) ────────────

function isCompleteStatus(st) {
  return st === 'COMPLETE' || st === 'FILLED';
}
function isOpenStatus(st) {
  return st === 'OPEN' || st === 'TRIGGER PENDING' || st === 'TRIGGER_PENDING';
}
function isHeldStatus(st) {
  return st === 'HELD' || st === 'CANCEL_FAILED';
}

/**
 * Mirror of `_fetchOrdersMerged`'s synchronous merge body (everything
 * after both `Promise.allSettled` legs have resolved) — dedup, supersede
 * filtering, and the session-boundary filter. `isCurrentSession` is
 * injected (not re-derived from a timestamp) so each test case can state
 * its intent directly instead of constructing real IST-session edge
 * timestamps.
 */
function mergeOrders({ brokerRows, algoRows, supersededIds, isCurrentSession }) {
  const brokerIds = new Set(brokerRows.map(o => String(o?.order_id || '')));
  const algoOnly = algoRows.filter(o => {
    const bid = String(o?.broker_order_id || '');
    return !bid || !brokerIds.has(bid);
  });
  const brokerRowsVisible = brokerRows.filter(o => {
    const oid = String(o?.order_id || '');
    if (!supersededIds.has(oid)) return true;
    return isCompleteStatus((o?.status || '').toUpperCase());
  });
  const merged = [
    ...brokerRowsVisible.map(o => ({ ...o, _rowOrigin: 'broker' })),
    ...algoOnly.map(o => ({ ...o, _rowOrigin: 'algo' })),
  ].filter(o => {
    const st = (o?.status || '').toUpperCase();
    if (isOpenStatus(st)) return true;
    if (isHeldStatus(st)) return true;
    return isCurrentSession(o);
  });
  return merged;
}

describe('ordersStore merge logic (pure mirror of _fetchOrdersMerged)', () => {
  it('a broker row NOT superseded is kept regardless of status', () => {
    const rows = mergeOrders({
      brokerRows: [{ order_id: 'B1', status: 'OPEN' }],
      algoRows: [],
      supersededIds: new Set(),
      isCurrentSession: () => true,
    });
    expect(rows).toHaveLength(1);
    expect(rows[0]._rowOrigin).toBe('broker');
  });

  it('drops a superseded broker row that is still OPEN (the chase cancel-and-replace race)', () => {
    const rows = mergeOrders({
      brokerRows: [{ order_id: 'B1', status: 'OPEN' }],
      algoRows: [],
      supersededIds: new Set(['B1']),
      isCurrentSession: () => true,
    });
    expect(rows).toHaveLength(0);
  });

  it('keeps a superseded broker row if it is already terminal-filled (late fill racing the cancel)', () => {
    const rows = mergeOrders({
      brokerRows: [{ order_id: 'B1', status: 'COMPLETE' }],
      algoRows: [],
      supersededIds: new Set(['B1']),
      isCurrentSession: () => true,
    });
    expect(rows).toHaveLength(1);
  });

  it('keeps an algo-only row with no broker counterpart (paper/sim/shadow, or placement lag)', () => {
    const rows = mergeOrders({
      brokerRows: [],
      algoRows: [{ id: 1, broker_order_id: '', status: 'OPEN' }],
      supersededIds: new Set(),
      isCurrentSession: () => true,
    });
    expect(rows).toHaveLength(1);
    expect(rows[0]._rowOrigin).toBe('algo');
  });

  it('dedups an algo row that already has a broker counterpart — broker row wins', () => {
    const rows = mergeOrders({
      brokerRows: [{ order_id: 'B1', status: 'OPEN' }],
      algoRows: [{ id: 1, broker_order_id: 'B1', status: 'OPEN' }],
      supersededIds: new Set(),
      isCurrentSession: () => true,
    });
    expect(rows).toHaveLength(1);
    expect(rows[0]._rowOrigin).toBe('broker');
  });

  it('always keeps a still-OPEN row regardless of session age', () => {
    const rows = mergeOrders({
      brokerRows: [{ order_id: 'B1', status: 'OPEN' }],
      algoRows: [],
      supersededIds: new Set(),
      isCurrentSession: () => false, // a prior-session row
    });
    expect(rows).toHaveLength(1);
  });

  it('always keeps a HELD/CANCEL_FAILED row regardless of session age (operator-review hold)', () => {
    const rowsHeld = mergeOrders({
      brokerRows: [{ order_id: 'B1', status: 'HELD' }],
      algoRows: [],
      supersededIds: new Set(),
      isCurrentSession: () => false,
    });
    const rowsCancelFailed = mergeOrders({
      brokerRows: [{ order_id: 'B2', status: 'CANCEL_FAILED' }],
      algoRows: [],
      supersededIds: new Set(),
      isCurrentSession: () => false,
    });
    expect(rowsHeld).toHaveLength(1);
    expect(rowsCancelFailed).toHaveLength(1);
  });

  it('drops a terminal row from a prior trading session (08:00 IST rollover)', () => {
    const rows = mergeOrders({
      brokerRows: [{ order_id: 'B1', status: 'COMPLETE' }],
      algoRows: [],
      supersededIds: new Set(),
      isCurrentSession: () => false,
    });
    expect(rows).toHaveLength(0);
  });

  it('keeps a terminal row from the CURRENT trading session', () => {
    const rows = mergeOrders({
      brokerRows: [{ order_id: 'B1', status: 'REJECTED' }],
      algoRows: [],
      supersededIds: new Set(),
      isCurrentSession: () => true,
    });
    expect(rows).toHaveLength(1);
  });
});

// ── Source-scan — ordersStore declaration + fetcher-must-throw contract ──

describe('marketDataStores.svelte.js — ordersStore wiring (source scan)', () => {
  it('declares ordersStore as a real createDataStore keyed md.orders', () => {
    expect(src).toMatch(/export const ordersStore = createDataStore\(\{/);
    expect(src).toMatch(/key:\s*'md\.orders'/);
    expect(src).toMatch(/fetcher:\s*_fetchOrdersMerged/);
  });

  /** Extract an async function's body by brace-matching from its
   *  declaration — same technique as marketDataStoresMeta.test.js's
   *  extractFn, extended for the `async function` keyword. */
  function extractAsyncFn(name) {
    const marker = `async function ${name}(`;
    const start = src.indexOf(marker);
    expect(start, `${name} not found`).toBeGreaterThan(-1);
    const bodyStart = src.indexOf('{', start);
    let depth = 0;
    for (let i = bodyStart; i < src.length; i++) {
      if (src[i] === '{') depth++;
      else if (src[i] === '}') {
        depth--;
        if (depth === 0) return src.slice(bodyStart, i + 1);
      }
    }
    throw new Error(`unterminated function body for ${name}`);
  }

  it('_fetchOrdersMerged THROWS when the broker leg rejects — the fetcher-must-throw contract', () => {
    const body = extractAsyncFn('_fetchOrdersMerged');
    expect(body).toMatch(/if \(brokerResp\.status !== 'fulfilled'\) \{/);
    expect(body).toMatch(/throw \(/);
    // Must NOT silently early-return without throwing — that was
    // OrderBook.svelte's own pre-B1 hand-rolled freeze, the exact
    // second/parallel mechanism this store replaces.
    expect(body).not.toMatch(/if \(brokerResp\.status !== 'fulfilled'\) \{\s*return;/);
  });

  it('_fetchOrdersMerged tolerates a rejected algo leg (degrades to [], does not throw)', () => {
    const body = extractAsyncFn('_fetchOrdersMerged');
    expect(body).toMatch(/algoResp\.status === 'fulfilled' && Array\.isArray\(algoResp\.value\)/);
  });

  it('_fetchOrdersMerged still calls noteOrderPollFills so the shared books-changed bus fires', () => {
    const body = extractAsyncFn('_fetchOrdersMerged');
    expect(body).toMatch(/noteOrderPollFills\(merged\);/);
  });

  it('exports isOrderSuperseded as the merge filter\'s predicate (not currently called outside this module — OrderBook.svelte\'s own Cancel-button guard does its own independent fresh re-fetch instead, since this predicate only ever reflects the last poll)', () => {
    expect(src).toMatch(/export function isOrderSuperseded\(/);
  });
});
