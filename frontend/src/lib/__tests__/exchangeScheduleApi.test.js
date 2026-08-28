/**
 * exchangeScheduleApi.test.js — Vitest unit tests for the three new
 * exchange-schedule API helpers in api.js.
 *
 * Five quality dimensions:
 *  1. SSOT  — calls the exact URL/method documented in the route spec
 *  2. Perf  — pure unit, no real network; globalThis.fetch mocked
 *  3. Stale — guards against path typos (wrong prefix, wrong method)
 *  4. Reuse — exercises the shared _get / _put / _del pipeline in api.js
 *  5. UX    — auth header forwarded (token present → Bearer header sent)
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

// ── Mock $lib/stores so api.js can be imported in a non-browser env ──────────
// api.js calls authStore.getToken() in _authHeaders(). We supply a stub
// that returns a fixed token so the auth-header path is exercised.
vi.mock('$lib/stores', () => ({
  authStore: {
    getToken: vi.fn(() => 'test-jwt-token'),
    logout: vi.fn(),
  },
}));

// Import AFTER the mock is registered so the module resolves correctly.
import {
  fetchExchangeSchedule,
  upsertExchangeSchedule,
  deleteExchangeSchedule,
} from '$lib/api';

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

// ── fetchExchangeSchedule ─────────────────────────────────────────────────────

describe('fetchExchangeSchedule', () => {
  it('calls GET /api/admin/exchange-schedule with auth header', async () => {
    const rows = [
      { id: 1, gate: 'NSE', session_name: 'regular', date: null },
    ];
    fetchSpy.mockResolvedValue(makeFetchResponse(rows));

    const result = await fetchExchangeSchedule();

    expect(fetchSpy).toHaveBeenCalledOnce();
    const [url, init] = fetchSpy.mock.calls[0];
    expect(url).toBe('/api/admin/exchange-schedule');
    expect(init.method).toBe('GET');
    expect(init.headers.Authorization).toBe('Bearer test-jwt-token');
    expect(result).toEqual(rows);
  });

  it('passes no body on GET', async () => {
    fetchSpy.mockResolvedValue(makeFetchResponse([]));
    await fetchExchangeSchedule();
    const [, init] = fetchSpy.mock.calls[0];
    expect(init.body).toBeUndefined();
  });
});

// ── upsertExchangeSchedule ────────────────────────────────────────────────────

describe('upsertExchangeSchedule', () => {
  it('calls PUT /api/admin/exchange-schedule with JSON body', async () => {
    const dto = {
      gate: 'NSE',
      exchanges: ['NSE', 'BSE'],
      date: '2026-08-15',
      session_name: 'closed',
      is_open: false,
      open_time: null,
      close_time: null,
      snapshot_time: null,
      snapshot_reset_time: null,
      reason: 'Independence Day',
    };
    const saved = { id: 99, ...dto };
    fetchSpy.mockResolvedValue(makeFetchResponse(saved));

    const result = await upsertExchangeSchedule(dto);

    expect(fetchSpy).toHaveBeenCalledOnce();
    const [url, init] = fetchSpy.mock.calls[0];
    expect(url).toBe('/api/admin/exchange-schedule');
    expect(init.method).toBe('PUT');
    // api.js sets Content-Type and JSON-stringifies the body
    expect(init.headers['Content-Type']).toBe('application/json');
    expect(JSON.parse(init.body)).toEqual(dto);
    expect(init.headers.Authorization).toBe('Bearer test-jwt-token');
    expect(result).toEqual(saved);
  });

  it('sends null date for default-schedule upserts', async () => {
    const dto = { gate: 'MCX', exchanges: ['MCX'], date: null,
                  session_name: 'morning', is_open: true,
                  open_time: '09:00', close_time: '17:00',
                  snapshot_time: null, snapshot_reset_time: null, reason: null };
    fetchSpy.mockResolvedValue(makeFetchResponse({ id: 2, ...dto }));

    await upsertExchangeSchedule(dto);

    const [, init] = fetchSpy.mock.calls[0];
    expect(JSON.parse(init.body).date).toBeNull();
  });

  it('does NOT double-stringify the body (no [object Object] bug)', async () => {
    // Regression guard: api.js already calls JSON.stringify internally.
    // If a caller accidentally passes a pre-stringified string, body would
    // be a string wrapped in another string — that is a bug. But callers
    // SHOULD pass a plain object and api.js handles stringify. This test
    // confirms the round-trip is clean.
    fetchSpy.mockResolvedValue(makeFetchResponse({ id: 1 }));
    const dto = { gate: 'NSE', exchanges: ['NSE'], date: null,
                  session_name: 'regular', is_open: true };
    await upsertExchangeSchedule(dto);
    const [, init] = fetchSpy.mock.calls[0];
    const parsed = JSON.parse(init.body);
    expect(typeof parsed).toBe('object');
    expect(parsed.gate).toBe('NSE');
  });
});

// ── deleteExchangeSchedule ────────────────────────────────────────────────────

describe('deleteExchangeSchedule', () => {
  it('calls DELETE /api/admin/exchange-schedule/{id} with auth header', async () => {
    // 204 No Content is the expected response for a DELETE
    fetchSpy.mockResolvedValue({ ok: true, status: 204, json: async () => null,
                                  headers: { get: () => null }, statusText: 'No Content' });

    const result = await deleteExchangeSchedule(42);

    expect(fetchSpy).toHaveBeenCalledOnce();
    const [url, init] = fetchSpy.mock.calls[0];
    expect(url).toBe('/api/admin/exchange-schedule/42');
    expect(init.method).toBe('DELETE');
    expect(init.headers.Authorization).toBe('Bearer test-jwt-token');
    // api.js returns null for 204 responses
    expect(result).toBeNull();
  });

  it('encodes numeric id in URL path (no encoding needed for integers)', async () => {
    fetchSpy.mockResolvedValue({ ok: true, status: 204, json: async () => null,
                                  headers: { get: () => null }, statusText: 'No Content' });
    await deleteExchangeSchedule(7);
    const [url] = fetchSpy.mock.calls[0];
    expect(url).toBe('/api/admin/exchange-schedule/7');
  });

  it('does not send a body on DELETE', async () => {
    fetchSpy.mockResolvedValue({ ok: true, status: 204, json: async () => null,
                                  headers: { get: () => null }, statusText: 'No Content' });
    await deleteExchangeSchedule(1);
    const [, init] = fetchSpy.mock.calls[0];
    expect(init.body).toBeUndefined();
  });
});
