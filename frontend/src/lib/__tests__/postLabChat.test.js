/**
 * postLabChat.test.js — Vitest unit tests for `postLabChat` in api.js.
 *
 * Covers the Lab chat contract: POST /api/lab/chat with {message}, the
 * 200 reply shape, and a 503 whose `detail` must reach the caller intact
 * (the Lab panel shows it as-is in a neutral info style).
 *
 * Same fetch-mock + $lib/stores mock pattern as apiAbortError.test.js.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('$lib/stores', () => ({
  authStore: {
    getToken: vi.fn(() => 'test-token'),
    logout: vi.fn(),
  },
}));

import { postLabChat } from '$lib/api';

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

describe('postLabChat', () => {
  it('POSTs {message} as JSON to /api/lab/chat and returns the reply', async () => {
    fetchSpy.mockResolvedValue(makeFetchResponse({ reply: 'pong', duration_ms: 812 }));

    const res = await postLabChat('ping');

    expect(fetchSpy).toHaveBeenCalledTimes(1);
    const [url, init] = fetchSpy.mock.calls[0];
    expect(url).toBe('/api/lab/chat');
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body)).toEqual({ message: 'ping' });
    expect(init.headers['Content-Type']).toBe('application/json');
    expect(init.headers.Authorization).toBe('Bearer test-token');
    expect(res).toEqual({ reply: 'pong', duration_ms: 812 });
  });

  it('surfaces the 503 detail text unchanged on the thrown error', async () => {
    const detail = 'Claude is not configured on this server. Set ANTHROPIC_API_KEY.';
    fetchSpy.mockResolvedValue(makeFetchResponse({ detail }, 503));

    let caught = null;
    try {
      await postLabChat('hello');
    } catch (e) {
      caught = e;
    }

    expect(caught).not.toBeNull();
    expect(caught.status).toBe(503);
    expect(caught.detail).toBe(detail);
  });

  it('surfaces the 502 detail on the thrown error', async () => {
    fetchSpy.mockResolvedValue(makeFetchResponse({ detail: 'Claude upstream failed' }, 502));

    await expect(postLabChat('hello')).rejects.toMatchObject({
      status: 502,
      detail: 'Claude upstream failed',
    });
  });
});
