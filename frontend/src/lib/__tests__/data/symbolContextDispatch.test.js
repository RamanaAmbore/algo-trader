/**
 * symbolContextDispatch.test.js — Vitest unit tests for the shared
 * context-menu-extra dispatcher every SymbolContextMenu host EXCEPT
 * MarketPulse uses (OrderBook, LogPanel, PerformancePage, the
 * derivatives Legs grid, /orders — 2026-10-22 context-menu parity work).
 *
 * The module under test transitively imports two `.svelte.js` files
 * with top-level `$state(...)` calls (toastStore.svelte.js,
 * pulseDetached.svelte.js) that this project's `node`-environment
 * Vitest config can't evaluate directly (no svelte-compiler plugin —
 * same documented constraint as marketDataStoresMeta.test.js). Both are
 * mocked below so the real files are never loaded; `watchlistContextActions.js`
 * is also mocked since its own behaviour is covered by
 * watchlistContextActions.test.js — this file tests ONLY the dispatch
 * switch (which action maps to which call + which toast).
 *
 * Five quality dimensions:
 *  1. SSOT   — every non-Pulse host routes all six extra actions through
 *              this one switch; a regression here breaks all five
 *              surfaces identically.
 *  2. Perf   — fully mocked, no real I/O/localStorage/clipboard.
 *  3. Stale  — regression-guards the EXACT toast message + success/
 *              error branch per action, so a future edit can't silently
 *              flip a success path to the sticky error toast UX.
 *  4. Reuse  — `isSharedContextAction` is asserted against the full set
 *              AND against the base/draft action names it must NOT
 *              claim (place-order/chart/close/orders/log/select-draft/
 *              promote) — each host relies on that boundary to decide
 *              whether to keep its own fallback handling.
 *  5. UX     — error-path toasts use the real `reason` string (not a
 *              generic fallback) when one is available, matching the
 *              "short, operator-useful" error-string convention.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';

const toastSuccess = vi.fn();
const toastError    = vi.fn();
vi.mock('$lib/data/toastStore.svelte.js', () => ({
  toast: { success: toastSuccess, error: toastError },
}));

const detachSymbol   = vi.fn();
const reattachSymbol = vi.fn();
vi.mock('$lib/data/pulseDetached.svelte.js', () => ({
  detachSymbol, reattachSymbol,
}));

const addSymbolToDefaultWatchlist = vi.fn();
const removeSymbolFromWatchlist   = vi.fn();
vi.mock('$lib/data/watchlistContextActions.js', () => ({
  addSymbolToDefaultWatchlist, removeSymbolFromWatchlist,
}));

const { isSharedContextAction, dispatchSharedContextAction } =
  await import('$lib/data/symbolContextDispatch.js');

beforeEach(() => {
  vi.clearAllMocks();
  vi.stubGlobal('window', { location: { href: '' } });
});

describe('isSharedContextAction', () => {
  it('claims all six shared extras', () => {
    for (const a of ['copy-symbol', 'open-options', 'detach', 'reattach', 'add-watchlist', 'remove-watchlist']) {
      expect(isSharedContextAction(a)).toBe(true);
    }
  });

  it('does not claim any base or draft action — hosts must keep handling these themselves', () => {
    for (const a of ['place-order', 'close', 'chart', 'orders', 'log', 'select-draft', 'promote']) {
      expect(isSharedContextAction(a)).toBe(false);
    }
  });
});

describe('dispatchSharedContextAction — open-options', () => {
  it('navigates to the derivatives page for the symbol\'s root', async () => {
    await dispatchSharedContextAction('open-options', 'NIFTY25APR22000CE', 'NFO');
    expect(window.location.href).toBe('/admin/derivatives?u=NIFTY');
  });
});

describe('dispatchSharedContextAction — copy-symbol', () => {
  it('toasts success when the clipboard write succeeds', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal('navigator', { clipboard: { writeText } });
    await dispatchSharedContextAction('copy-symbol', 'RELIANCE');
    expect(writeText).toHaveBeenCalledWith('RELIANCE');
    expect(toastSuccess).toHaveBeenCalledWith('Symbol copied');
    expect(toastError).not.toHaveBeenCalled();
  });

  it('toasts an error when the clipboard write fails', async () => {
    const writeText = vi.fn().mockRejectedValue(new Error('denied'));
    vi.stubGlobal('navigator', { clipboard: { writeText } });
    await dispatchSharedContextAction('copy-symbol', 'RELIANCE');
    expect(toastError).toHaveBeenCalledWith('Copy failed');
    expect(toastSuccess).not.toHaveBeenCalled();
  });
});

describe('dispatchSharedContextAction — detach / reattach', () => {
  it('detaches the symbol and toasts success', async () => {
    await dispatchSharedContextAction('detach', 'NIFTY25APR22000CE');
    expect(detachSymbol).toHaveBeenCalledWith('NIFTY25APR22000CE');
    expect(toastSuccess).toHaveBeenCalledWith('Detached from Pulse group');
  });

  it('reattaches the symbol and toasts success', async () => {
    await dispatchSharedContextAction('reattach', 'NIFTY25APR22000CE');
    expect(reattachSymbol).toHaveBeenCalledWith('NIFTY25APR22000CE');
    expect(toastSuccess).toHaveBeenCalledWith('Re-attached to Pulse group');
  });
});

describe('dispatchSharedContextAction — add-watchlist / remove-watchlist', () => {
  it('toasts success when add succeeds', async () => {
    addSymbolToDefaultWatchlist.mockResolvedValue({ ok: true });
    await dispatchSharedContextAction('add-watchlist', 'RELIANCE', 'NSE');
    expect(addSymbolToDefaultWatchlist).toHaveBeenCalledWith('RELIANCE', 'NSE');
    expect(toastSuccess).toHaveBeenCalledWith('Added to watchlist');
  });

  it('defaults the exchange to an empty string when none is supplied', async () => {
    addSymbolToDefaultWatchlist.mockResolvedValue({ ok: true });
    await dispatchSharedContextAction('add-watchlist', 'RELIANCE');
    expect(addSymbolToDefaultWatchlist).toHaveBeenCalledWith('RELIANCE', '');
  });

  it('toasts the real failure reason when add fails', async () => {
    addSymbolToDefaultWatchlist.mockResolvedValue({ ok: false, reason: 'No watchlist available' });
    await dispatchSharedContextAction('add-watchlist', 'RELIANCE', 'NSE');
    expect(toastError).toHaveBeenCalledWith('No watchlist available');
  });

  it('falls back to a generic message when add fails with no reason', async () => {
    addSymbolToDefaultWatchlist.mockResolvedValue({ ok: false });
    await dispatchSharedContextAction('add-watchlist', 'RELIANCE', 'NSE');
    expect(toastError).toHaveBeenCalledWith('Add failed');
  });

  it('toasts success when remove succeeds', async () => {
    removeSymbolFromWatchlist.mockResolvedValue({ ok: true });
    await dispatchSharedContextAction('remove-watchlist', 'RELIANCE');
    expect(removeSymbolFromWatchlist).toHaveBeenCalledWith('RELIANCE');
    expect(toastSuccess).toHaveBeenCalledWith('Removed from watchlist');
  });

  it('toasts the real failure reason when remove fails', async () => {
    removeSymbolFromWatchlist.mockResolvedValue({ ok: false, reason: 'Not in watchlist' });
    await dispatchSharedContextAction('remove-watchlist', 'RELIANCE');
    expect(toastError).toHaveBeenCalledWith('Not in watchlist');
  });

  it('falls back to a generic message when remove fails with no reason', async () => {
    removeSymbolFromWatchlist.mockResolvedValue({ ok: false });
    await dispatchSharedContextAction('remove-watchlist', 'RELIANCE');
    expect(toastError).toHaveBeenCalledWith('Remove failed');
  });
});

describe('dispatchSharedContextAction — unknown action', () => {
  it('is a silent no-op', async () => {
    await expect(dispatchSharedContextAction('not-a-real-action', 'RELIANCE')).resolves.toBeUndefined();
    expect(toastSuccess).not.toHaveBeenCalled();
    expect(toastError).not.toHaveBeenCalled();
  });
});
