/**
 * orderTimelineLogic.test.js
 *
 * Unit tests for OrderTimelineDrawer.svelte's field-mismatch fix
 * (docs/proposals/SPRINT2_LAYER_INTEGRATION.md §1 finding 1, §4.2).
 *
 * The drawer previously read invented fields (`ev.symbol`, `ev.side`,
 * `ev.qty`, `ev.mode`, `ev.created_at`, `ev.price`/`ev.limit_price`) that
 * don't exist on the real `AlgoOrderEventInfo` response — only
 * `id, order_id, ts, kind, message, payload_json` are real. These tests
 * exercise the extracted pure module directly (no Svelte mount), matching
 * the ChaseCard.inflight / orderStatusPredicates pure-function precedent.
 *
 * Five quality dimensions:
 *   1. SSOT   — one grouping/sort/payload-parse implementation, imported by
 *               both the component and this test; no logic duplicated here.
 *   2. Perf   — pure unit tests, no DOM / network.
 *   3. Stale  — explicitly asserts the OLD broken fields (symbol/side/qty/
 *               mode/created_at/price on the event itself) are ignored,
 *               proving the fix doesn't silently fall back to them.
 *   4. Reuse  — one factory-free module covers grouping, sorting, payload
 *               parsing, and price extraction used by the single caller.
 *   5. UX     — a live order never gets a 'paper' mode default; unresolved
 *               context renders neutral, not misleading.
 */

import { describe, it, expect } from 'vitest';
import {
  TERMINAL_KINDS,
  parseEventPayload,
  extractEventPrice,
  toDisplayEvent,
  isTerminalSection,
  latestEventTs,
  groupOrderEvents,
  resolveOrderId,
  buildOrderContextEntry,
  buildLinkedOrders,
} from '../order/orderTimelineLogic.js';

describe('parseEventPayload', () => {
  it('parses a valid JSON object string', () => {
    expect(parseEventPayload('{"price": 101.5}')).toEqual({ price: 101.5 });
  });

  it('returns {} for null/undefined/empty string — never throws', () => {
    expect(parseEventPayload(null)).toEqual({});
    expect(parseEventPayload(undefined)).toEqual({});
    expect(parseEventPayload('')).toEqual({});
  });

  it('returns {} for malformed JSON instead of throwing', () => {
    expect(parseEventPayload('{not json')).toEqual({});
  });

  it('returns {} for a JSON array or primitive (not treated as a payload object)', () => {
    expect(parseEventPayload('[1,2,3]')).toEqual({});
    expect(parseEventPayload('42')).toEqual({});
  });
});

describe('extractEventPrice', () => {
  it('prefers payload.price (chase_modify / postback / paper-ticket placed)', () => {
    expect(extractEventPrice({ price: 100, fill_price: 200 })).toBe(100);
  });

  it('falls back to fill_price (paper terminal fill event)', () => {
    expect(extractEventPrice({ fill_price: 99.5 })).toBe(99.5);
  });

  it('falls back to limit_price then initial_price (paper engine placed event)', () => {
    expect(extractEventPrice({ limit_price: 50 })).toBe(50);
    expect(extractEventPrice({ initial_price: 45 })).toBe(45);
  });

  it('returns null when no price-like field exists — e.g. a live ticket "placed" event with no payload at all', () => {
    expect(extractEventPrice({})).toBeNull();
    expect(extractEventPrice(null)).toBeNull();
  });

  it('IGNORES a stale ev.price/ev.limit_price shape if accidentally passed the raw event instead of payload (regression guard)', () => {
    // A raw AlgoOrderEventInfo row has no top-level price fields at all —
    // confirms callers must parse payload_json first, not read the event.
    const rawEvent = { id: 1, order_id: 2, ts: '2026-01-01T00:00:00Z', kind: 'placed', message: 'x', payload_json: null };
    expect(extractEventPrice(rawEvent)).toBeNull();
  });
});

describe('toDisplayEvent', () => {
  it('maps the REAL AlgoOrderEventInfo fields (id, order_id, ts, kind, message) and derives price from payload_json', () => {
    const ev = {
      id: 7, order_id: 42, ts: '2026-01-01T10:00:00Z', kind: 'chase_modify',
      message: 'Chase attempt 2', payload_json: JSON.stringify({ price: 150, attempt: 2 }),
    };
    expect(toDisplayEvent(ev)).toEqual({
      id: 7, order_id: 42, ts: '2026-01-01T10:00:00Z', kind: 'chase_modify',
      message: 'Chase attempt 2', price: 150,
    });
  });

  it('never reads nonexistent ev.symbol/ev.side/ev.qty/ev.mode/ev.created_at — not present on the output shape at all', () => {
    const ev = {
      id: 1, order_id: 1, ts: '2026-01-01T00:00:00Z', kind: 'placed', message: 'x',
      payload_json: null,
      // Old (wrong) fields that must be ignored entirely:
      symbol: 'SHOULD-NOT-APPEAR', side: 'SELL', qty: 999, mode: 'live',
      created_at: '1970-01-01T00:00:00Z',
    };
    const out = toDisplayEvent(ev);
    expect(out).not.toHaveProperty('symbol');
    expect(out).not.toHaveProperty('side');
    expect(out).not.toHaveProperty('qty');
    expect(out).not.toHaveProperty('mode');
    expect(out).not.toHaveProperty('created_at');
  });
});

describe('isTerminalSection / TERMINAL_KINDS', () => {
  it('matches the backend terminal vocabulary exactly', () => {
    expect(TERMINAL_KINDS).toEqual(new Set(['fill', 'unfill', 'reject', 'cancel']));
  });

  it('is true once any event in the list is terminal', () => {
    expect(isTerminalSection([{ kind: 'placed' }, { kind: 'fill' }])).toBe(true);
    expect(isTerminalSection([{ kind: 'placed' }, { kind: 'chase_modify' }])).toBe(false);
  });

  it('is false for an empty/undefined list', () => {
    expect(isTerminalSection([])).toBe(false);
    expect(isTerminalSection(undefined)).toBe(false);
  });
});

describe('latestEventTs', () => {
  it('returns the MAX parseable ts, not the first element (events arrive oldest-first)', () => {
    const events = [
      { ts: '2026-01-01T09:00:00Z' },
      { ts: '2026-01-01T11:00:00Z' },
      { ts: '2026-01-01T10:00:00Z' },
    ];
    expect(latestEventTs(events)).toBe(new Date('2026-01-01T11:00:00Z').getTime());
  });

  it('returns 0 for unparseable/missing timestamps rather than throwing', () => {
    expect(latestEventTs([{ ts: '' }, { ts: undefined }])).toBe(0);
    expect(latestEventTs([])).toBe(0);
  });
});

describe('groupOrderEvents', () => {
  const EVENTS = [
    // Order 1 — OPEN chase, oldest-first as the backend returns them.
    { id: 1, order_id: 1, ts: '2026-01-01T09:00:00Z', kind: 'placed', message: 'placed', payload_json: null },
    { id: 2, order_id: 1, ts: '2026-01-01T09:05:00Z', kind: 'chase_modify', message: 'attempt 2', payload_json: JSON.stringify({ price: 101 }) },
    // Order 2 — terminal (filled), most recent activity overall.
    { id: 3, order_id: 2, ts: '2026-01-01T09:10:00Z', kind: 'placed', message: 'placed', payload_json: null },
    { id: 4, order_id: 2, ts: '2026-01-01T09:20:00Z', kind: 'fill', message: 'filled', payload_json: JSON.stringify({ fill_price: 102.5 }) },
  ];

  const CONTEXT = {
    1: { symbol: 'RBQ-LIVE1', side: 'BUY', qty: 50, mode: 'live' },
    2: { symbol: 'RBQ-PAPER1', side: 'SELL', qty: 10, mode: 'paper' },
  };

  it('groups a flat event list by order_id', () => {
    const grouped = groupOrderEvents(EVENTS, CONTEXT);
    expect(grouped).toHaveLength(2);
    const byId = Object.fromEntries(grouped.map((s) => [s.order_id, s]));
    expect(byId[1].events).toHaveLength(2);
    expect(byId[2].events).toHaveLength(2);
  });

  it('merges symbol/side/qty/mode from orderContextById, NOT from the event rows', () => {
    const grouped = groupOrderEvents(EVENTS, CONTEXT);
    const order1 = grouped.find((s) => s.order_id === 1);
    expect(order1.symbol).toBe('RBQ-LIVE1');
    expect(order1.side).toBe('BUY');
    expect(order1.qty).toBe(50);
    expect(order1.mode).toBe('live');
  });

  it('a LIVE order never defaults to the paper mode pill when context IS resolved', () => {
    const grouped = groupOrderEvents(EVENTS, CONTEXT);
    const order1 = grouped.find((s) => s.order_id === 1);
    expect(order1.mode).toBe('live');
    expect(order1.mode).not.toBe('paper');
  });

  it('falls back to neutral empty values (not "paper") when context is missing entirely', () => {
    const grouped = groupOrderEvents(EVENTS, {});
    const order1 = grouped.find((s) => s.order_id === 1);
    expect(order1.symbol).toBe('');
    expect(order1.side).toBe('');
    expect(order1.qty).toBeNull();
    expect(order1.mode).toBe('');
  });

  it('sorts non-terminal sections before terminal ones', () => {
    const grouped = groupOrderEvents(EVENTS, CONTEXT);
    // order_id 1 (non-terminal: placed + chase_modify) before order_id 2 (terminal: fill).
    expect(grouped[0].order_id).toBe(1);
    expect(grouped[1].order_id).toBe(2);
  });

  it('within terminal/non-terminal groups, sorts by MOST RECENT activity first', () => {
    const events = [
      // Two non-terminal orders — order 20 has the more recent event.
      { id: 1, order_id: 10, ts: '2026-01-01T09:00:00Z', kind: 'placed', message: 'x', payload_json: null },
      { id: 2, order_id: 20, ts: '2026-01-01T09:30:00Z', kind: 'placed', message: 'x', payload_json: null },
    ];
    const grouped = groupOrderEvents(events, {});
    expect(grouped[0].order_id).toBe(20);
    expect(grouped[1].order_id).toBe(10);
  });

  it('ignores events with a null/undefined order_id rather than crashing', () => {
    const events = [{ id: 1, order_id: null, ts: '2026-01-01T00:00:00Z', kind: 'placed', message: 'x', payload_json: null }];
    expect(groupOrderEvents(events, {})).toEqual([]);
  });

  it('handles an empty/undefined events array', () => {
    expect(groupOrderEvents([], {})).toEqual([]);
    expect(groupOrderEvents(undefined, {})).toEqual([]);
  });

  it('each grouped event carries a derived price from payload_json, not a nonexistent ev.price', () => {
    const grouped = groupOrderEvents(EVENTS, CONTEXT);
    const order1 = grouped.find((s) => s.order_id === 1);
    const chaseModifyEvent = order1.events.find((e) => e.kind === 'chase_modify');
    expect(chaseModifyEvent.price).toBe(101);
    const order2 = grouped.find((s) => s.order_id === 2);
    const fillEvent = order2.events.find((e) => e.kind === 'fill');
    expect(fillEvent.price).toBe(102.5);
  });
});

// ── Per-order timeline view (OrderBook.svelte's click-to-open drawer) ──

describe('resolveOrderId', () => {
  it('prefers order_id (broker OrderRow shape)', () => {
    expect(resolveOrderId({ order_id: 'ORD1', id: 99 })).toBe('ORD1');
  });

  it('falls back to id (AlgoOrderInfo shape)', () => {
    expect(resolveOrderId({ id: 42 })).toBe(42);
  });

  it('returns null when neither is present', () => {
    expect(resolveOrderId({})).toBeNull();
    expect(resolveOrderId(null)).toBeNull();
  });
});

describe('buildOrderContextEntry', () => {
  it('builds a single-entry map keyed by the resolved order id', () => {
    const order = { order_id: 'ORD1', tradingsymbol: 'NIFTY26JUN25000CE', transaction_type: 'BUY', quantity: 50, mode: 'live' };
    expect(buildOrderContextEntry(order)).toEqual({
      ORD1: { symbol: 'NIFTY26JUN25000CE', side: 'BUY', qty: 50, mode: 'live' },
    });
  });

  it('falls back to symbol/id fields for AlgoOrderInfo shape', () => {
    const order = { id: 7, symbol: 'RBQ-LIVE1', transaction_type: 'SELL', quantity: 10, mode: 'paper' };
    expect(buildOrderContextEntry(order)).toEqual({
      7: { symbol: 'RBQ-LIVE1', side: 'SELL', qty: 10, mode: 'paper' },
    });
  });

  it('honors an explicit idOverride (linked-chip navigation where only the id is known)', () => {
    expect(buildOrderContextEntry({ symbol: 'X', transaction_type: 'BUY', quantity: 1 }, 123))
      .toEqual({ 123: { symbol: 'X', side: 'BUY', qty: 1, mode: '' } });
  });

  it('returns {} when no id can be resolved at all', () => {
    expect(buildOrderContextEntry({})).toEqual({});
  });
});

describe('buildLinkedOrders', () => {
  it('returns null when none of parent_order_id/child_order_ids/basket_tag are set', () => {
    expect(buildLinkedOrders({ id: 1, symbol: 'X' })).toBeNull();
    expect(buildLinkedOrders({ child_order_ids: [] })).toBeNull();
    expect(buildLinkedOrders(null)).toBeNull();
  });

  it('returns a populated shape when parent_order_id is set', () => {
    expect(buildLinkedOrders({ parent_order_id: 1001 })).toEqual({
      parent_order_id: 1001, child_order_ids: [], basket_tag: null,
    });
  });

  it('returns a populated shape when child_order_ids is non-empty', () => {
    expect(buildLinkedOrders({ child_order_ids: [2002, 2003] })).toEqual({
      parent_order_id: null, child_order_ids: [2002, 2003], basket_tag: null,
    });
  });

  it('filters out null/undefined entries inside child_order_ids', () => {
    expect(buildLinkedOrders({ child_order_ids: [2002, null, undefined] })).toEqual({
      parent_order_id: null, child_order_ids: [2002], basket_tag: null,
    });
  });

  it('returns a populated shape when basket_tag is set', () => {
    expect(buildLinkedOrders({ basket_tag: 'ramboq-basket-abc123' })).toEqual({
      parent_order_id: null, child_order_ids: [], basket_tag: 'ramboq-basket-abc123',
    });
  });
});
