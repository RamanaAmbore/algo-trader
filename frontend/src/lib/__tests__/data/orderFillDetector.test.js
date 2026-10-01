/**
 * orderFillDetector.test.js
 *
 * Unit tests for `noteOrderPollFills` (frontend/src/lib/data/orderFillDetector.js)
 * — the channel-agnostic "order log just showed Filled" trigger added as a
 * 2026-09-30 follow-up to commit c90a9d04.
 *
 * Context: c90a9d04 fixed the LAST link in the "order fulfilled → fresh
 * books → fresh legs → fresh payoff" chain (the derivatives legs-watcher
 * effect). This module fixes the FIRST link for channels that never
 * broadcast a WS event at all:
 *   - the live broker-order-book read (`/api/orders/`, 15s TTL passthrough)
 *   - the 5-min `_task_open_order_watchdog` backstop sweep (Dhan/Groww's
 *     documented webhook-unreliable path)
 *   - the admin `/algo/reconcile` sweep and the per-card reconcile button
 * None of those backend paths broadcast `order_update` / `position_filled`
 * / `positions_refreshed` / `book_changed`. `noteOrderPollFills` closes the
 * gap by watching the SAME merged row array `OrderBook.svelte` /
 * `LogPanel.svelte` already build on every poll, and bumping the existing
 * `bookChanged` bus the moment any row transitions into a filled state —
 * reusing the one refresh mechanism every WS handler already drives,
 * rather than inventing a second one.
 *
 * Five quality dimensions:
 *   1. SSOT     — one shared module-level Map; OrderBook and LogPanel
 *                 observing the identical transition must bump the bus
 *                 only once (not twice).
 *   2. Perf     — coalesces a whole poll/basket-fill burst into a single
 *                 bus bump, not one per row.
 *   3. Stale    — an order that STAYS filled across polls (the common
 *                 steady state) must never refire — this is an edge
 *                 trigger, not a level signal.
 *   4. Reuse    — asserts against the real `bookChanged` writable store,
 *                 the exact bus every WS fill handler already drives
 *                 (see bookChanged.js) — no second mechanism invented.
 *   5. UX       — covers the two concrete gap shapes from the task:
 *                 (a) an order sitting OPEN across polls that later shows
 *                 COMPLETE (the 5-min watchdog / reconcile shape), and
 *                 (b) an order that fills so fast it's never observed OPEN
 *                 at all — appears already COMPLETE on its first-ever
 *                 appearance (a MARKET order inside one 15s `/api/orders/`
 *                 TTL window) — diffing only previously-known keys would
 *                 silently miss this, the single most common fill shape.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { get } from 'svelte/store';
import { noteOrderPollFills, _resetOrderFillDetector } from '../../data/orderFillDetector.js';
import { bookChanged } from '../../data/bookChanged.js';

function _bump() {
  return get(bookChanged);
}

beforeEach(() => {
  _resetOrderFillDetector();
  bookChanged.set(0);
});

describe('noteOrderPollFills — seeding (first-ever call)', () => {
  it('does not bump bookChanged on the very first observation, even when an order is already filled', () => {
    // Page-load scenario: an order that filled before this component ever
    // mounted (e.g. yesterday's book, or /orders opened post-fill) must not
    // look like a "just happened" fill the moment the UI first observes it.
    noteOrderPollFills([
      { order_id: 'A1', status: 'COMPLETE' },
      { order_id: 'A2', status: 'OPEN' },
    ]);
    expect(_bump()).toBe(0);
  });

  it('seeds an empty first poll without throwing and without bumping', () => {
    noteOrderPollFills([]);
    expect(_bump()).toBe(0);
    // A real fill observed on the NEXT poll still fires normally —
    // seeding must not get permanently stuck because the first poll
    // happened to be empty (e.g. no orders placed yet this session).
    noteOrderPollFills([{ order_id: 'B1', status: 'OPEN' }]);
    expect(_bump()).toBe(0); // still no fill yet
    noteOrderPollFills([{ order_id: 'B1', status: 'COMPLETE' }]);
    expect(_bump()).toBe(1);
  });
});

describe('noteOrderPollFills — known-order transition (5-min watchdog / reconcile shape)', () => {
  it('bumps bookChanged when a previously OPEN order flips to COMPLETE', () => {
    noteOrderPollFills([{ order_id: 'O1', status: 'OPEN' }]); // seed
    expect(_bump()).toBe(0);
    noteOrderPollFills([{ order_id: 'O1', status: 'OPEN' }]); // unchanged poll
    expect(_bump()).toBe(0);
    noteOrderPollFills([{ order_id: 'O1', status: 'COMPLETE' }]); // fill detected
    expect(_bump()).toBe(1);
  });

  it('bumps for AlgoOrder FILLED vocabulary, not only broker COMPLETE', () => {
    noteOrderPollFills([{ id: 42, status: 'OPEN' }]);
    noteOrderPollFills([{ id: 42, status: 'FILLED' }]);
    expect(_bump()).toBe(1);
  });

  it('does not refire while an order stays filled across subsequent polls', () => {
    noteOrderPollFills([{ order_id: 'O2', status: 'OPEN' }]);
    noteOrderPollFills([{ order_id: 'O2', status: 'COMPLETE' }]);
    expect(_bump()).toBe(1);
    noteOrderPollFills([{ order_id: 'O2', status: 'COMPLETE' }]);
    noteOrderPollFills([{ order_id: 'O2', status: 'COMPLETE' }]);
    expect(_bump()).toBe(1); // no refire — edge trigger, not a level signal
  });

  it('does not fire on a non-fill terminal transition (CANCELLED/REJECTED)', () => {
    noteOrderPollFills([{ order_id: 'O3', status: 'OPEN' }]);
    noteOrderPollFills([{ order_id: 'O3', status: 'CANCELLED' }]);
    expect(_bump()).toBe(0);
  });
});

describe('noteOrderPollFills — unseen-but-already-filled (fast MARKET order shape)', () => {
  it('fires for a brand-new order_id that appears already filled, after the seeding pass', () => {
    // Seed the module with some unrelated order first.
    noteOrderPollFills([{ order_id: 'SEED', status: 'OPEN' }]);
    expect(_bump()).toBe(0);
    // A MARKET order placed and filled entirely between two polls never
    // shows up OPEN — the first time this poll loop ever sees order_id
    // "M1" at all, it's already COMPLETE. Must still count as a fill.
    noteOrderPollFills([
      { order_id: 'SEED', status: 'OPEN' },
      { order_id: 'M1', status: 'COMPLETE' },
    ]);
    expect(_bump()).toBe(1);
  });
});

describe('noteOrderPollFills — burst coalescing (basket fill shape)', () => {
  it('bumps bookChanged exactly once for multiple rows filling in the same poll', () => {
    noteOrderPollFills([
      { order_id: 'L1', status: 'OPEN' },
      { order_id: 'L2', status: 'OPEN' },
      { order_id: 'L3', status: 'OPEN' },
    ]);
    noteOrderPollFills([
      { order_id: 'L1', status: 'COMPLETE' },
      { order_id: 'L2', status: 'COMPLETE' },
      { order_id: 'L3', status: 'OPEN' },
    ]);
    expect(_bump()).toBe(1);
  });
});

describe('noteOrderPollFills — SSOT (shared module state across callers)', () => {
  it('a transition already observed by one caller (e.g. OrderBook) does not refire for a second caller (e.g. LogPanel) seeing the identical merged array', () => {
    // Both components call the SAME exported function against the SAME
    // merged rows on their own poll tick — simulate OrderBook's call
    // immediately followed by LogPanel's call for the identical poll.
    noteOrderPollFills([{ order_id: 'X1', status: 'OPEN' }]); // OrderBook seed
    noteOrderPollFills([{ order_id: 'X1', status: 'OPEN' }]); // LogPanel seed (same tick)
    noteOrderPollFills([{ order_id: 'X1', status: 'COMPLETE' }]); // OrderBook poll
    expect(_bump()).toBe(1);
    noteOrderPollFills([{ order_id: 'X1', status: 'COMPLETE' }]); // LogPanel poll, same tick
    expect(_bump()).toBe(1); // no double-bump
  });
});
