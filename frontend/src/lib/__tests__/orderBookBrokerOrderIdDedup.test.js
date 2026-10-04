/**
 * orderBookBrokerOrderIdDedup.test.js
 *
 * Unit tests for the Sprint 2a dedup fix in OrderBook.svelte /
 * LogPanel.svelte's `_loadOrders()` merge step, and the companion
 * `_isOpenBroker` origin check — exercised as pure functions, no Svelte
 * mounting, mirroring the existing ChaseCard.inflight.test.js pattern.
 *
 * Context: `AlgoOrderInfo` never carried an `order_id` field (that's a
 * broker-only shape). The old dedup compared a broker row's `order_id`
 * against `algoRow.order_id || algoRow.id` — silently falling through to
 * the algo row's own internal DB `id`, which can never equal a broker
 * `order_id`. The dedup never matched, so a live, algo-tracked order
 * rendered TWICE. `broker_order_id` is now a real surfaced field and the
 * fix: dedup on that instead.
 *
 * Separately, `_isOpenBroker` (gates Modify/Cancel) used `!o?.mode` as a
 * proxy for "is this a broker row" — replaced with an explicit
 * `_rowOrigin` flag stamped at merge time.
 *
 * Five quality dimensions:
 *   1. SSOT   — one dedup key (broker_order_id), no fallback to internal id
 *   2. Perf   — pure unit tests, no DOM / network
 *   3. Stale  — the OLD buggy comparison is explicitly tested first, to
 *               document the bug before verifying the fix
 *   4. Reuse  — same merge/origin functions mirrored for both OrderBook
 *               and LogPanel (identical logic, verified identically here)
 *   5. UX     — Modify/Cancel visibility (_isOpenBroker) verified to
 *               survive the merge for a deduped broker-origin row
 */

import { describe, it, expect } from 'vitest';

// ── Pre-fix behaviour (documents the bug) ────────────────────────────────
// Mirrors the OLD buggy dedup from OrderBook.svelte / LogPanel.svelte
// before the Sprint 2a fix.
function _oldBuggyAlgoOnly(brokerRows, algoRows) {
  const brokerIds = new Set(brokerRows.map(o => String(o?.order_id || '')));
  return algoRows.filter(o => {
    const oid = String(o?.order_id || o?.id || '');
    return !brokerIds.has(oid);
  });
}

// ── Fixed behaviour — mirrors OrderBook.svelte / LogPanel.svelte's
//    current `_loadOrders()` dedup + origin-stamping. ────────────────────
function _fixedAlgoOnly(brokerRows, algoRows) {
  const brokerIds = new Set(brokerRows.map(o => String(o?.order_id || '')));
  return algoRows.filter(o => {
    const bid = String(o?.broker_order_id || '');
    return !bid || !brokerIds.has(bid);
  });
}

function _mergeWithOrigin(brokerRows, algoOnly) {
  return [
    ...brokerRows.map(o => ({ ...o, _rowOrigin: 'broker' })),
    ...algoOnly.map(o => ({ ...o, _rowOrigin: 'algo' })),
  ];
}

// Mirrors the fixed OrderBook.svelte / LogPanel.svelte `_isOpenBroker`.
function _isOpenBroker(o) {
  const st = (o?.status || '').toUpperCase();
  return (st === 'OPEN' || st === 'TRIGGER PENDING' || st === 'TRIGGER_PENDING') && o?._rowOrigin === 'broker';
}

// Mirrors the OLD buggy `_isOpenBroker` (field-presence heuristic).
function _isOpenBrokerOldBuggy(o) {
  const st = (o?.status || '').toUpperCase();
  return (st === 'OPEN' || st === 'TRIGGER PENDING' || st === 'TRIGGER_PENDING') && !o?.mode;
}

const BROKER_ROW = {
  order_id: 'B5001', account: 'T1', exchange: 'NFO', tradingsymbol: 'RBQ-DEDUPE1',
  transaction_type: 'BUY', quantity: 50, status: 'OPEN', order_timestamp: '2026-09-30 09:05:00',
};
// Real shape: broker_order_id now populated, matching BROKER_ROW.order_id.
// `id` (777) is the algo row's own internal DB id — deliberately a
// DIFFERENT number from the broker's order_id, as it always is in prod.
const ALGO_ROW_DUPLICATE = {
  id: 777, account: 'T1', symbol: 'RBQ-DEDUPE1', exchange: 'NFO', transaction_type: 'BUY',
  quantity: 50, status: 'OPEN', mode: 'live', created_at: '2026-09-30T04:05:00',
  broker_order_id: 'B5001',
};
// An algo-only row with NO broker counterpart yet (paper/sim/shadow, or a
// live order whose placement hasn't reached the broker book this poll).
const ALGO_ROW_STANDALONE = {
  id: 999, account: 'T1', symbol: 'RBQ-PAPERONLY', exchange: 'NFO', transaction_type: 'SELL',
  quantity: 10, status: 'OPEN', mode: 'paper', created_at: '2026-09-30T04:06:00',
  broker_order_id: null,
};

describe('OrderBook/LogPanel dedup — pre-fix bug documentation', () => {
  it('pre-fix: the duplicate algo row is NEVER excluded (bug reproduced)', () => {
    const algoOnly = _oldBuggyAlgoOnly([BROKER_ROW], [ALGO_ROW_DUPLICATE]);
    // Bug: comparing against the algo row's internal `id` (777) against
    // brokerIds (containing 'B5001') never matches — the duplicate leaks
    // through unfiltered.
    expect(algoOnly).toHaveLength(1);
    expect(algoOnly[0]).toBe(ALGO_ROW_DUPLICATE);
  });
});

describe('OrderBook/LogPanel dedup — fixed broker_order_id comparison', () => {
  it('excludes an algo row whose broker_order_id matches a broker row order_id', () => {
    const algoOnly = _fixedAlgoOnly([BROKER_ROW], [ALGO_ROW_DUPLICATE]);
    expect(algoOnly).toHaveLength(0);
  });

  it('keeps an algo-only row with no broker_order_id (paper/sim/shadow, not yet placed)', () => {
    const algoOnly = _fixedAlgoOnly([BROKER_ROW], [ALGO_ROW_STANDALONE]);
    expect(algoOnly).toHaveLength(1);
    expect(algoOnly[0]).toBe(ALGO_ROW_STANDALONE);
  });

  it('a merged render-ready list has exactly ONE row for the duplicate order', () => {
    const algoOnly = _fixedAlgoOnly([BROKER_ROW], [ALGO_ROW_DUPLICATE, ALGO_ROW_STANDALONE]);
    const merged = _mergeWithOrigin([BROKER_ROW], algoOnly);
    const dupeMatches = merged.filter(o => (o.tradingsymbol || o.symbol) === 'RBQ-DEDUPE1');
    expect(dupeMatches).toHaveLength(1);
    // The surviving row is the BROKER row (display-data choice unchanged
    // by this fix — broker rows still win on duplicate order_id).
    expect(dupeMatches[0]._rowOrigin).toBe('broker');
    // The standalone algo-only row is untouched and still present.
    expect(merged).toHaveLength(2);
  });
});

describe('_isOpenBroker — explicit _rowOrigin flag replaces !o?.mode heuristic', () => {
  it('fixed: an OPEN broker-origin row is actionable (Modify/Cancel visible)', () => {
    const merged = _mergeWithOrigin([BROKER_ROW], []);
    expect(_isOpenBroker(merged[0])).toBe(true);
  });

  it('fixed: an OPEN algo-origin row is NOT actionable via this path, even with extra fields', () => {
    const merged = _mergeWithOrigin([], [ALGO_ROW_STANDALONE]);
    expect(_isOpenBroker(merged[0])).toBe(false);
  });

  it('old heuristic and new flag AGREE on today\'s shapes (regression-safety: the fix is not a behaviour change for existing fields)', () => {
    const mergedBroker = _mergeWithOrigin([BROKER_ROW], [])[0];
    const mergedAlgo = _mergeWithOrigin([], [ALGO_ROW_STANDALONE])[0];
    expect(_isOpenBroker(mergedBroker)).toBe(_isOpenBrokerOldBuggy(BROKER_ROW));
    expect(_isOpenBroker(mergedAlgo)).toBe(_isOpenBrokerOldBuggy(ALGO_ROW_STANDALONE));
  });

  it('demonstrates the forward risk the old heuristic carried: a broker row that gains a `mode` field would silently lose Modify/Cancel under the OLD heuristic, but not under the fix', () => {
    const futureBrokerRowWithMode = { ...BROKER_ROW, mode: 'live' }; // hypothetical future enrichment
    expect(_isOpenBrokerOldBuggy(futureBrokerRowWithMode)).toBe(false); // old: silently breaks
    const merged = _mergeWithOrigin([futureBrokerRowWithMode], []);
    expect(_isOpenBroker(merged[0])).toBe(true); // fixed: unaffected by extra fields
  });
});

describe('ChaseCard broker_order_id dedup (orders +page.svelte pendingOrders exclusion)', () => {
  // Mirrors ChaseCard.svelte's `chaseOrderIds` derivation and the orders
  // page's `_pendingOrders` filter.
  function chaseOrderIdsFrom(chases) {
    return new Set(chases.map(c => String(c.broker_order_id || '')).filter(Boolean));
  }
  function pendingOrdersFrom(brokerOrders, chaseOrderIds) {
    return brokerOrders.filter(o =>
      (o.status === 'OPEN' || o.status === 'TRIGGER PENDING' || o.status === 'TRIGGER_PENDING') &&
      !chaseOrderIds.has(String(o.order_id || ''))
    );
  }

  const CHASE_BROKER_ROW = { order_id: 'B6002', status: 'OPEN', tradingsymbol: 'RBQ-CHASEDUP' };
  const CHASE_ROW = { id: 888, broker_order_id: 'B6002', status: 'OPEN', mode: 'live' };

  it('pre-fix: chaseOrderIds is empty when broker_order_id is absent (bug reproduced)', () => {
    const legacyChaseRow = { id: 888, status: 'OPEN', mode: 'live' }; // no broker_order_id at all
    const ids = chaseOrderIdsFrom([legacyChaseRow]);
    expect(ids.size).toBe(0);
    const pending = pendingOrdersFrom([CHASE_BROKER_ROW], ids);
    // Bug: the chased order leaks into Pending Orders too.
    expect(pending).toHaveLength(1);
  });

  it('fixed: a real broker_order_id excludes the chased order from Pending Orders', () => {
    const ids = chaseOrderIdsFrom([CHASE_ROW]);
    expect(ids.has('B6002')).toBe(true);
    const pending = pendingOrdersFrom([CHASE_BROKER_ROW], ids);
    expect(pending).toHaveLength(0);
  });

  it('an unrelated OPEN order (different order_id) still shows in Pending Orders', () => {
    const ids = chaseOrderIdsFrom([CHASE_ROW]);
    const unrelated = { order_id: 'B7003', status: 'OPEN', tradingsymbol: 'RBQ-PLAIN' };
    const pending = pendingOrdersFrom([CHASE_BROKER_ROW, unrelated], ids);
    expect(pending).toHaveLength(1);
    expect(pending[0].order_id).toBe('B7003');
  });
});
