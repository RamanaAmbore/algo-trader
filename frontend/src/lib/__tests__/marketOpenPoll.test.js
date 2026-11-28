/**
 * marketOpenPoll.test.js
 *
 * Unit tests for `createMarketOpenPoll()` (frontend/src/lib/marketHours.js) —
 * the shared market-open poll factory that consolidates what used to be
 * four independent 30s `visibleInterval` timers (RefreshButton.svelte,
 * PositionStrip.svelte, OrderTicket.svelte, SymbolPanel.svelte's Chain-tab
 * resolution), each re-invoking isNseOpen()/isMcxOpen() on its own schedule
 * and caching the result in local component `$state`. One instance of this
 * factory is wired as the `marketOpenStore` singleton in `stores.js`;
 * tested here directly (not via stores.js) because stores.js transitively
 * imports a runes (`.svelte.js`) module this project's plain-Node Vitest
 * config has no Svelte-compiler plugin to evaluate — see CLAUDE.md's
 * "Vitest trap" convention documented across this test suite
 * (marketDataStoresMeta.test.js, bookPollerClosedWindow.test.js, etc).
 *
 * Five quality dimensions:
 *  1. SSOT    — one orchestration (timer + onMarketStatusChange wiring),
 *               fully dependency-injected, with no module-level duplicate
 *               state across the four former call sites.
 *  2. Perf    — verifies exactly ONE interval is registered per start(),
 *               never one per onChange call, and that it's cleared on
 *               teardown (no leaked timer).
 *  3. Stale   — regression-guards the "fires immediately on start, before
 *               the first tick" contract — without it, a late subscriber
 *               (a modal opened mid-session) would render a stale seed
 *               value for up to 30s.
 *  4. Reuse   — read() always delegates to the real isNseOpen()/isMcxOpen()
 *               exports, never a re-implementation of market-hours math.
 *  5. UX      — onStatusChange firing recomputes immediately, so a fresh
 *               /api/market/status poll landing doesn't wait for the next
 *               30s tick to reach the UI (closes the exact race
 *               onMarketStatusChange's own docstring describes).
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import {
  createMarketOpenPoll,
  setServerMarketStatus,
  isNseOpen,
  isMcxOpen,
} from '../marketHours.js';

function mockStatus(nse, mcx) {
  setServerMarketStatus({ nse_open: nse, mcx_open: mcx, any_open: nse || mcx, is_holiday: false });
}

describe('createMarketOpenPoll', () => {
  beforeEach(() => {
    // Reset server-status override between tests so each starts from a
    // known state (setServerMarketStatus has no "clear" API — overwrite
    // with an explicit closed reading instead).
    mockStatus(false, false);
  });

  it('read() delegates to the real isNseOpen()/isMcxOpen() exports, never a re-implementation', () => {
    const poll = createMarketOpenPoll();
    mockStatus(true, false);
    expect(poll.read()).toEqual({ nseOpen: isNseOpen(), mcxOpen: isMcxOpen() });
    expect(poll.read()).toEqual({ nseOpen: true, mcxOpen: false });
  });

  it('start() fires onChange immediately with the current reading, before any timer tick', () => {
    mockStatus(true, true);
    const poll = createMarketOpenPoll({ setIntervalFn: vi.fn(() => 1), clearIntervalFn: vi.fn(), onStatusChange: vi.fn(() => () => {}) });
    const onChange = vi.fn();
    poll.start(onChange);
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange).toHaveBeenCalledWith({ nseOpen: true, mcxOpen: true });
  });

  it('start() registers exactly ONE interval — not one per subscriber-side concern', () => {
    const setIntervalFn = vi.fn((_fn, _ms) => 42);
    const clearIntervalFn = vi.fn();
    const poll = createMarketOpenPoll({ setIntervalFn, clearIntervalFn, intervalMs: 30_000, onStatusChange: vi.fn(() => () => {}) });
    poll.start(vi.fn());
    expect(setIntervalFn).toHaveBeenCalledTimes(1);
    expect(setIntervalFn.mock.calls[0][1]).toBe(30_000);
  });

  it('each interval tick re-reads state and calls onChange again', () => {
    /** @type {() => void} */
    let tickFn = () => {};
    const setIntervalFn = vi.fn((fn) => { tickFn = fn; return 1; });
    const poll = createMarketOpenPoll({ setIntervalFn, clearIntervalFn: vi.fn(), onStatusChange: vi.fn(() => () => {}) });
    const onChange = vi.fn();
    mockStatus(false, false);
    poll.start(onChange);
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange).toHaveBeenLastCalledWith({ nseOpen: false, mcxOpen: false });

    mockStatus(true, false);
    tickFn();
    expect(onChange).toHaveBeenCalledTimes(2);
    expect(onChange).toHaveBeenLastCalledWith({ nseOpen: true, mcxOpen: false });
  });

  it('onStatusChange firing recomputes and calls onChange immediately, without waiting for the next tick', () => {
    /** @type {() => void} */
    let statusCb = () => {};
    const onStatusChange = vi.fn((cb) => { statusCb = cb; return vi.fn(); });
    const poll = createMarketOpenPoll({ setIntervalFn: vi.fn(() => 1), clearIntervalFn: vi.fn(), onStatusChange });
    const onChange = vi.fn();
    mockStatus(false, false);
    poll.start(onChange);
    expect(onChange).toHaveBeenCalledTimes(1);

    mockStatus(false, true);
    statusCb();
    expect(onChange).toHaveBeenCalledTimes(2);
    expect(onChange).toHaveBeenLastCalledWith({ nseOpen: false, mcxOpen: true });
  });

  it('the returned teardown clears the interval and unsubscribes from status changes exactly once each', () => {
    const clearIntervalFn = vi.fn();
    const unsubStatus = vi.fn();
    const onStatusChange = vi.fn(() => unsubStatus);
    const poll = createMarketOpenPoll({ setIntervalFn: vi.fn(() => 7), clearIntervalFn, onStatusChange });
    const teardown = poll.start(vi.fn());
    expect(clearIntervalFn).not.toHaveBeenCalled();
    expect(unsubStatus).not.toHaveBeenCalled();

    teardown();
    expect(clearIntervalFn).toHaveBeenCalledWith(7);
    expect(clearIntervalFn).toHaveBeenCalledTimes(1);
    expect(unsubStatus).toHaveBeenCalledTimes(1);
  });

  it('defaults to the real global setInterval/clearInterval when no deps are injected (uses fake timers)', () => {
    vi.useFakeTimers();
    try {
      mockStatus(true, false);
      const poll = createMarketOpenPoll({ onStatusChange: vi.fn(() => () => {}) });
      const onChange = vi.fn();
      const teardown = poll.start(onChange);
      expect(onChange).toHaveBeenCalledTimes(1);

      mockStatus(false, true);
      vi.advanceTimersByTime(30_000);
      expect(onChange).toHaveBeenCalledTimes(2);
      expect(onChange).toHaveBeenLastCalledWith({ nseOpen: false, mcxOpen: true });

      teardown();
      vi.advanceTimersByTime(60_000);
      // No further calls after teardown — the real setInterval was cleared.
      expect(onChange).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
    }
  });
});
