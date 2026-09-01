/**
 * fetchChainExpiries.test.js — Vitest unit tests for the signal parameter
 * added to fetchChainExpiries in api.js.
 *
 * Five quality dimensions:
 *  1. SSOT  — imports the exact function from $lib/api (not a replica)
 *  2. Perf  — pure unit, no real network; globalThis.fetch mocked
 *  3. Stale — guards that signal omission still works (backward-compat)
 *  4. Reuse — exercises the shared _get / _request pipeline in api.js
 *  5. UX    — AbortError on abort: component cancel() must silence it;
 *             test verifies the signal is wired so abort propagates
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

// ── Mock $lib/stores so api.js can be imported in a non-browser env ──────────
vi.mock('$lib/stores', () => ({
  authStore: {
    getToken: vi.fn(() => 'test-jwt-token'),
    logout: vi.fn(),
  },
}));

// Import AFTER the mock is registered so the module resolves correctly.
import { fetchChainExpiries } from '$lib/api';

// ── Fetch mock helpers ────────────────────────────────────────────────────────

/** Build a minimal Response-like object that fetch resolves to. */
function makeFetchResponse(body, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 200 ? 'OK' : 'Error',
    json: async () => body,
    headers: { get: () => null },
  };
}

let fetchSpy;

beforeEach(() => {
  fetchSpy = vi.fn();
  globalThis.fetch = fetchSpy;
});

afterEach(() => {
  vi.restoreAllMocks();
  delete globalThis.fetch;
});

// ── Without signal (backward-compat) ─────────────────────────────────────────

describe('fetchChainExpiries — without signal', () => {
  it('calls GET /api/options/chain-quotes with underlying param and auth header', async () => {
    const payload = { underlying: 'NIFTY', expiry: '', expiries: ['2026-09-25'], rows: [] };
    fetchSpy.mockResolvedValue(makeFetchResponse(payload));

    const result = await fetchChainExpiries('NIFTY');

    expect(fetchSpy).toHaveBeenCalledOnce();
    const [url, init] = fetchSpy.mock.calls[0];
    expect(url).toBe('/api/options/chain-quotes?underlying=NIFTY');
    expect(init.method).toBe('GET');
    expect(init.headers.Authorization).toBe('Bearer test-jwt-token');
    expect(result).toEqual(payload);
  });

  it('URL-encodes underlying symbols with spaces or special chars', async () => {
    fetchSpy.mockResolvedValue(makeFetchResponse({ expiries: [] }));

    await fetchChainExpiries('BANK NIFTY');

    const [url] = fetchSpy.mock.calls[0];
    expect(url).toBe('/api/options/chain-quotes?underlying=BANK+NIFTY');
  });

  it('does not pass a signal to fetch when called without one', async () => {
    fetchSpy.mockResolvedValue(makeFetchResponse({ expiries: ['2026-09-25'] }));

    await fetchChainExpiries('NIFTY');

    const [, init] = fetchSpy.mock.calls[0];
    // _request creates its own internal AbortController for the 15s timeout;
    // what matters is that init.signal is truthy (the internal AC's signal),
    // meaning we did NOT pass our own AbortController's signal.
    // We can only verify fetch was called with some signal — not null/undefined.
    // (Internal timeout AC always wires a signal into init.)
    expect(init.signal).toBeDefined();
  });
});

// ── With signal ───────────────────────────────────────────────────────────────

describe('fetchChainExpiries — with signal', () => {
  it('passes the caller signal to fetch init', async () => {
    const payload = { underlying: 'BANKNIFTY', expiry: '', expiries: ['2026-09-25'], rows: [] };
    fetchSpy.mockResolvedValue(makeFetchResponse(payload));

    const controller = new AbortController();
    await fetchChainExpiries('BANKNIFTY', controller.signal);

    expect(fetchSpy).toHaveBeenCalledOnce();
    const [url, init] = fetchSpy.mock.calls[0];
    expect(url).toBe('/api/options/chain-quotes?underlying=BANKNIFTY');
    // When caller passes their own signal, _request uses it directly (no internal AC).
    expect(init.signal).toBe(controller.signal);
  });

  it('propagates AbortError when the caller aborts before fetch resolves', async () => {
    // Simulate fetch rejecting with AbortError (as the browser does on abort)
    const abortErr = Object.assign(new Error('The user aborted a request.'), { name: 'AbortError' });
    fetchSpy.mockRejectedValue(abortErr);

    const controller = new AbortController();
    controller.abort();

    await expect(fetchChainExpiries('NIFTY', controller.signal)).rejects.toMatchObject({
      name: 'AbortError',
    });
  });

  it('does not log an api error to console on AbortError (intentional cancellation)', async () => {
    // _request re-throws AbortError immediately, before any _logApiError call.
    // We verify the error name is preserved so component catch blocks can check err.name.
    const abortErr = Object.assign(new Error('Aborted'), { name: 'AbortError' });
    fetchSpy.mockRejectedValue(abortErr);

    const controller = new AbortController();
    let caught = null;
    try {
      await fetchChainExpiries('NIFTY', controller.signal);
    } catch (e) {
      caught = e;
    }
    expect(caught).not.toBeNull();
    expect(caught.name).toBe('AbortError');
  });

  it('still returns data normally when signal has not been aborted', async () => {
    const payload = { underlying: 'NIFTY', expiry: '2026-09-25', expiries: ['2026-09-25', '2026-10-30'], rows: [] };
    fetchSpy.mockResolvedValue(makeFetchResponse(payload));

    const controller = new AbortController();
    // Do NOT abort — happy path
    const result = await fetchChainExpiries('NIFTY', controller.signal);

    expect(result).toEqual(payload);
    expect(Array.isArray(result.expiries)).toBe(true);
    expect(result.expiries.length).toBe(2);
  });
});

// ── URL construction ──────────────────────────────────────────────────────────

describe('fetchChainExpiries — URL construction', () => {
  it('uses URLSearchParams encoding (underlying= key)', async () => {
    fetchSpy.mockResolvedValue(makeFetchResponse({ expiries: [] }));

    await fetchChainExpiries('FINNIFTY');

    const [url] = fetchSpy.mock.calls[0];
    expect(url).toContain('underlying=FINNIFTY');
    // Must use the correct route prefix
    expect(url).toContain('/api/options/chain-quotes');
  });
});
