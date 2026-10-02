/**
 * bookChangedPositionsRefreshed.test.js
 *
 * Fix 2 (2026-10) — bookChanged.js's WS subscriber silently dropped the
 * `positions_refreshed` event kind, falling through the final
 * `if (msg.event !== 'book_changed') return;` guard with no effect at
 * all. This is the ONLY backstop refresh signal for Dhan/Groww fills
 * when their postback webhook isn't configured (see CLAUDE.md
 * "Dhan/Groww order detection" + `_positions_refresh_after_fill` in
 * backend/api/routes/orders.py).
 *
 * Mocks `$lib/ws`'s `createPerformanceSocket` to capture the callback
 * `startBookChangedBus()` registers, then invokes it directly with a
 * synthetic `positions_refreshed` frame — exactly the shape the backend
 * broadcasts (`{event, tradingsymbol, account, ts}`, NOT `symbol`).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

let _capturedCb = null;

vi.mock('$lib/ws', () => ({
  createPerformanceSocket: (cb) => {
    _capturedCb = cb;
    return () => { _capturedCb = null; };
  },
}));

import { get } from 'svelte/store';
import {
  bookChanged,
  lastFillEvent,
  lastBookEvent,
  startBookChangedBus,
  stopBookChangedBus,
} from '../../data/bookChanged.js';

beforeEach(() => {
  bookChanged.set(0);
  lastFillEvent.set(null);
  lastBookEvent.set(null);
  startBookChangedBus();
});

afterEach(() => {
  stopBookChangedBus();
});

describe('bookChanged bus — positions_refreshed event kind', () => {
  it('bumps bookChanged immediately (no debounce) on a positions_refreshed frame', () => {
    expect(_capturedCb).toBeTypeOf('function');
    _capturedCb({
      event: 'positions_refreshed',
      tradingsymbol: 'CRUDEOIL26OCTFUT',
      account: 'ZG0790',
      ts: 1700000000000,
    });
    // No setTimeout/debounce involved — must be reflected synchronously,
    // unlike book_changed's 200ms-debounced lane.
    expect(get(bookChanged)).toBe(1);
  });

  it('maps the tradingsymbol field onto lastFillEvent.symbol (shape bridge)', () => {
    _capturedCb({
      event: 'positions_refreshed',
      tradingsymbol: 'NIFTY26JAN22000CE',
      account: 'ZJ6294',
      ts: 42,
    });
    const ev = get(lastFillEvent);
    expect(ev).toEqual({ account: 'ZJ6294', symbol: 'NIFTY26JAN22000CE', ts: 42 });
  });

  it('does not touch lastBookEvent (that store stays scoped to book_changed frames)', () => {
    _capturedCb({ event: 'positions_refreshed', tradingsymbol: 'X', account: 'A', ts: 1 });
    expect(get(lastBookEvent)).toBeNull();
  });

  it('still bumps bookChanged for the pre-existing fill_event kind (no regression)', () => {
    _capturedCb({ event: 'fill_event', symbol: 'INFY', account: 'ZG0790', ts: 5 });
    expect(get(bookChanged)).toBe(1);
    expect(get(lastFillEvent)).toEqual({ account: 'ZG0790', symbol: 'INFY', ts: 5 });
  });

  it('defaults ts to Date.now() when the backend omits it', () => {
    const before = Date.now();
    _capturedCb({ event: 'positions_refreshed', tradingsymbol: 'Y', account: 'B' });
    const ev = get(lastFillEvent);
    expect(ev.ts).toBeGreaterThanOrEqual(before);
  });
});
