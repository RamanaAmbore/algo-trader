/**
 * apiAbortError.test.js — Vitest unit tests for the AbortError discriminator
 * in `_request` inside api.js.
 *
 * The fix (2026-09-05) changes the re-throw behaviour:
 *   - Internal 15 s timeout AbortError (no external signal supplied) → return null
 *   - Caller-supplied AbortController abort → re-throw so the caller can detect
 *     intentional cancellation (e.g. component unmount, navigation)
 *
 * Five quality dimensions:
 *   1. SSOT   — exercises the real `_request` pipeline via a public api.js export
 *   2. Perf   — pure unit, globalThis.fetch mocked, no real network
 *   3. Stale  — verifies both paths of the discriminator (internal vs external abort)
 *   4. Reuse  — same fetch-mock helper pattern as fetchChainExpiries.test.js
 *   5. UX     — internal timeout must not surface an uncaught error; caller abort
 *               must propagate so component catch blocks can silence it gracefully
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

// ── Mock $lib/stores so api.js can be imported in a non-browser env ──────────
vi.mock('$lib/stores', () => ({
  authStore: {
    getToken: vi.fn(() => null),  // anonymous — avoids auth redirect logic
    logout: vi.fn(),
  },
}));

// Import AFTER the mock is registered.
// fetchWhoami calls _get with no external signal → exercises the internal
// AbortController branch. fetchChainExpiries accepts a caller signal → exercises
// the caller-abort branch. placeTicketOrder opts into `throwOnTimeout` (D3 fix,
// 2026-09) → exercises the new TimeoutError-throwing branch.
import { fetchWhoami, fetchChainExpiries, placeTicketOrder } from '$lib/api';
import { authStore } from '$lib/stores';

// ── Fetch mock helpers ────────────────────────────────────────────────────────

/** Build a minimal Response-like object. */
function makeFetchResponse(body, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 200 ? 'OK' : 'Error',
    json: async () => body,
    headers: { get: () => null },
  };
}

/** Create an AbortError matching the DOMException shape browsers produce. */
function makeAbortError() {
  return Object.assign(new Error('The user aborted a request.'), { name: 'AbortError' });
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

// ── Internal timeout path — must return null, not throw ───────────────────────

describe('_request — internal 15 s timeout AbortError', () => {
  it('returns null (does not throw) when fetch rejects with AbortError and no caller signal was supplied', async () => {
    // Simulate fetch hanging until the internal AbortController fires.
    // In this test we immediately reject to avoid a real 15 s wait.
    fetchSpy.mockRejectedValue(makeAbortError());

    // fetchWhoami calls _get without a caller signal → internal AC branch.
    const result = await fetchWhoami();
    expect(result).toBeNull();
  });

  it('does not propagate the AbortError to the caller on internal timeout', async () => {
    fetchSpy.mockRejectedValue(makeAbortError());

    // Must resolve (returning null), not reject.
    await expect(fetchWhoami()).resolves.toBeNull();
  });
});

// ── Caller-supplied signal path — must re-throw ───────────────────────────────

describe('_request — caller-supplied AbortController abort', () => {
  it('re-throws AbortError when the caller aborts their own signal', async () => {
    fetchSpy.mockRejectedValue(makeAbortError());

    const controller = new AbortController();
    controller.abort();

    // fetchChainExpiries accepts a caller signal → exercises the propagate path.
    await expect(fetchChainExpiries('NIFTY', controller.signal)).rejects.toMatchObject({
      name: 'AbortError',
    });
  });

  it('AbortError from caller signal is not swallowed — .name is preserved', async () => {
    fetchSpy.mockRejectedValue(makeAbortError());

    const controller = new AbortController();
    controller.abort();

    let caught = null;
    try {
      await fetchChainExpiries('BANKNIFTY', controller.signal);
    } catch (e) {
      caught = e;
    }
    expect(caught).not.toBeNull();
    expect(caught.name).toBe('AbortError');
  });
});

// ── Normal success path is unaffected ─────────────────────────────────────────

describe('_request — normal success path', () => {
  it('returns the parsed JSON body on a 200 response', async () => {
    const payload = { username: 'testuser', role: 'partner' };
    fetchSpy.mockResolvedValue(makeFetchResponse(payload));

    const result = await fetchWhoami();
    expect(result).toEqual(payload);
  });
});

// ── D3 fix (2026-09): opt-in `throwOnTimeout` — order placement must ─────────
// NEVER render a timeout as a false success. placeTicketOrder is the one
// caller that opts into this; every other _request caller (tested above via
// fetchWhoami) keeps the null-swallow default so a hung poll doesn't surface
// an error banner.
describe('_request — throwOnTimeout opt-in (placeTicketOrder)', () => {
  it('throws a distinctly-named TimeoutError instead of resolving null on the internal 15s timeout', async () => {
    fetchSpy.mockRejectedValue(makeAbortError());

    let caught = null;
    try {
      await placeTicketOrder({ tradingsymbol: 'NIFTY26JUN22000CE', side: 'BUY', quantity: 1 });
    } catch (e) {
      caught = e;
    }
    expect(caught).not.toBeNull();
    expect(caught.name).toBe('TimeoutError');
    // Must NOT be an AbortError — callers key off the distinct name to avoid
    // rendering the old false-success `submitOk` (order id shown as "#?").
    expect(caught.name).not.toBe('AbortError');
  });

  it('does not resolve null on timeout (the old false-success shape)', async () => {
    fetchSpy.mockRejectedValue(makeAbortError());

    await expect(
      placeTicketOrder({ tradingsymbol: 'NIFTY26JUN22000CE', side: 'BUY', quantity: 1 })
    ).rejects.toBeTruthy();
  });

  it('a genuine successful response is unaffected by throwOnTimeout', async () => {
    const payload = { order_id: '12345', mode: 'paper', status: 'COMPLETE' };
    fetchSpy.mockResolvedValue(makeFetchResponse(payload));

    const result = await placeTicketOrder({ tradingsymbol: 'NIFTY26JUN22000CE', side: 'BUY', quantity: 1 });
    expect(result).toEqual(payload);
  });

  it('a caller-supplied signal abort still re-throws a real AbortError, not TimeoutError', async () => {
    // Sanity check: throwOnTimeout only governs the INTERNAL (no external
    // signal) timeout branch — an external AbortController abort must keep
    // propagating as a genuine AbortError, unchanged by this fix.
    fetchSpy.mockRejectedValue(makeAbortError());
    const controller = new AbortController();
    controller.abort();

    let caught = null;
    try {
      await fetchChainExpiries('NIFTY', controller.signal);
    } catch (e) {
      caught = e;
    }
    expect(caught?.name).toBe('AbortError');
  });
});

// ── R6 fix (2026-09): `err.fullMessage` — full detail alongside the ──────────
// truncated ~32-char banner. Requires an authenticated (non-anonymous) caller
// — `_friendlyError` intentionally suppresses raw backend detail for
// anonymous/demo sessions, and `fullMessage` must respect the same gate.
describe('_request — err.fullMessage (R6)', () => {
  beforeEach(() => {
    vi.mocked(authStore.getToken).mockReturnValue('fake-jwt-token');
  });
  afterEach(() => {
    vi.mocked(authStore.getToken).mockReturnValue(null);
  });

  it('joins ALL blocked[] reasons, not just the first, in fullMessage', async () => {
    const longReason1 = 'Preflight blocked: available margin ₹12,345 is below the required ₹98,765 for this basket';
    const longReason2 = 'Second leg also blocked: lot size mismatch detected for CRUDEOIL26JUNFUT';
    fetchSpy.mockResolvedValue(makeFetchResponse(
      { detail: { blocked: [{ reason: longReason1 }, { reason: longReason2 }] } },
      422,
    ));

    let caught = null;
    try {
      await placeTicketOrder({ tradingsymbol: 'NIFTY26JUN22000CE', side: 'BUY', quantity: 1 });
    } catch (e) {
      caught = e;
    }
    expect(caught).not.toBeNull();
    // Short banner stays clamped (~32 chars + ellipsis) — layout guarantee.
    expect(caught.message.length).toBeLessThanOrEqual(35);
    // Full message carries BOTH reasons, not just blocked[0].
    expect(caught.fullMessage).toContain(longReason1);
    expect(caught.fullMessage).toContain(longReason2);
  });

  it('fullMessage equals the short message when nothing was actually truncated', async () => {
    fetchSpy.mockResolvedValue(makeFetchResponse({ detail: 'Pick an account' }, 400));

    let caught = null;
    try {
      await placeTicketOrder({ tradingsymbol: 'NIFTY26JUN22000CE', side: 'BUY', quantity: 1 });
    } catch (e) {
      caught = e;
    }
    expect(caught.message).toBe('Pick an account');
    expect(caught.fullMessage).toBe('Pick an account');
  });
});
