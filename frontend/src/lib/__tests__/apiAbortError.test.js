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
// the caller-abort branch.
import { fetchWhoami, fetchChainExpiries } from '$lib/api';

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
