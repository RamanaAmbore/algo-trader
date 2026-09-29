/**
 * templates.test.js — Vitest unit tests for loadOrderTemplates().
 *
 * Guards the 2026-09-30 fix: a failed fetch (network blip, or — the
 * live-suspected cause — an auth-token-not-yet-attached race very early
 * in page load) must NOT be cached as a permanent "confirmed zero
 * templates" result. Before the fix, `if (_templates) return _templates;`
 * checked truthiness — and an empty array is truthy — so one transient
 * failure poisoned every future call in that browser tab for the rest of
 * its session (only a genuine fresh tab reset the module's `_templates`
 * back to `null`). This broke the Chain tab's Templ toggle (gated on
 * `_templates.length > 0`) invisibly and permanently until a full reload,
 * matching the live symptom: fresh Playwright test runs (auth pre-seeded
 * before navigation, no race) always worked; a real operator session that
 * happened to hit the race once stayed broken indefinitely.
 *
 * Five quality dimensions:
 *  1. SSOT  — exercises loadOrderTemplates(), the single fetch boundary
 *             every Templ-toggle-gating consumer (SymbolPanel.svelte)
 *             calls through.
 *  2. Perf  — mocked I/O; no live network calls.
 *  3. Stale — guards the exact truthiness bug pattern (empty array is
 *             truthy) so a future refactor can't silently reintroduce it.
 *  4. Reuse — loadOrderTemplates() is the sole entry point; no bespoke
 *             fetch-and-cache logic duplicated elsewhere.
 *  5. UX    — a failed-then-retried fetch must recover to real data on
 *             the next call, not stay permanently empty.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('$lib/api', () => ({
  fetchOrderTemplates: vi.fn(),
}));

const SAMPLE_TEMPLATES = [
  { id: 1, name: 'Default Bull', slug: 'default-bull', is_default: true, applies_to: 'buy_any', is_active: true },
  { id: 2, name: 'Default Bear', slug: 'default-bear', is_default: true, applies_to: 'sell_any', is_active: true },
];

describe('loadOrderTemplates', () => {
  beforeEach(() => {
    vi.resetModules();
    vi.clearAllMocks();
  });

  it('returns the fetched templates on a successful call', async () => {
    const api = await import('$lib/api');
    vi.mocked(api.fetchOrderTemplates).mockResolvedValue(SAMPLE_TEMPLATES);
    const { loadOrderTemplates } = await import('$lib/data/templates.js');

    const result = await loadOrderTemplates();
    expect(result).toEqual(SAMPLE_TEMPLATES);
  });

  it('a failed fetch does NOT permanently cache an empty result — the next call retries and can succeed', async () => {
    const api = await import('$lib/api');
    const fetchMock = vi.mocked(api.fetchOrderTemplates);
    // First call fails (simulating the auth-timing race / transient network blip).
    fetchMock.mockRejectedValueOnce(new Error('network blip'));
    // Second call (a later SymbolPanel mount — e.g. opening a new order)
    // succeeds with real data.
    fetchMock.mockResolvedValueOnce(SAMPLE_TEMPLATES);

    const { loadOrderTemplates } = await import('$lib/data/templates.js');

    const firstResult = await loadOrderTemplates();
    expect(firstResult).toEqual([]); // this caller sees an empty array (no crash)

    const secondResult = await loadOrderTemplates();
    // THE REGRESSION GUARD: before the fix, this would ALSO be `[]`
    // (the truthy empty array short-circuited every future call) even
    // though the second fetch would have succeeded if actually attempted.
    expect(secondResult).toEqual(SAMPLE_TEMPLATES);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('a successful fetch IS cached — a subsequent call does not re-fetch', async () => {
    const api = await import('$lib/api');
    const fetchMock = vi.mocked(api.fetchOrderTemplates);
    fetchMock.mockResolvedValue(SAMPLE_TEMPLATES);

    const { loadOrderTemplates } = await import('$lib/data/templates.js');

    await loadOrderTemplates();
    await loadOrderTemplates();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('a genuinely empty (but successful) response IS cached — not treated as a failure', async () => {
    const api = await import('$lib/api');
    const fetchMock = vi.mocked(api.fetchOrderTemplates);
    fetchMock.mockResolvedValue([]);

    const { loadOrderTemplates } = await import('$lib/data/templates.js');

    const first = await loadOrderTemplates();
    const second = await loadOrderTemplates();
    expect(first).toEqual([]);
    expect(second).toEqual([]);
    // A real empty response (no active templates in the DB) is a valid,
    // cacheable answer — only a THROWN exception should skip caching.
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('concurrent calls while a fetch is in flight share the same promise (dedup)', async () => {
    const api = await import('$lib/api');
    const fetchMock = vi.mocked(api.fetchOrderTemplates);
    /** @type {(v: any) => void} */
    let resolveFetch = () => {};
    fetchMock.mockReturnValue(new Promise((resolve) => { resolveFetch = resolve; }));

    const { loadOrderTemplates } = await import('$lib/data/templates.js');

    const p1 = loadOrderTemplates();
    const p2 = loadOrderTemplates();
    resolveFetch(SAMPLE_TEMPLATES);

    const [r1, r2] = await Promise.all([p1, p2]);
    expect(r1).toEqual(SAMPLE_TEMPLATES);
    expect(r2).toEqual(SAMPLE_TEMPLATES);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
