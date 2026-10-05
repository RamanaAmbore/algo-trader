/**
 * apiTimeoutMessage.test.js — a 504 from the gateway shows a timeout message,
 * not the generic "Server busy" line.
 *
 * Long Lab/Research chat answers can outlive the proxy's read timeout; the
 * user needs to know the request timed out and that a shorter question helps.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

vi.mock('$lib/stores', () => ({
  authStore: {
    getToken: vi.fn(() => null),
    logout: vi.fn(),
  },
}));

import { fetchNavHistory } from '$lib/api';

describe('504 gateway timeout message', () => {
  let originalFetch;

  beforeEach(() => {
    originalFetch = globalThis.fetch;
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
    vi.restoreAllMocks();
  });

  it('shows the timeout message for a bodyless 504', async () => {
    globalThis.fetch = vi.fn(async () =>
      new Response('', { status: 504, statusText: 'Gateway Timeout' }),
    );
    await expect(fetchNavHistory()).rejects.toThrow(
      'Timed out — try a shorter question.',
    );
  });

  it('keeps the generic message for other 5xx without a detail', async () => {
    globalThis.fetch = vi.fn(async () =>
      new Response('', { status: 500, statusText: '' }),
    );
    await expect(fetchNavHistory()).rejects.toThrow('Server busy — retry.');
  });
});
