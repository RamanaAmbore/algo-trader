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

// Sprint E (Agent/Bracket/Template unification phase) — loadOrderTemplates()
// now sources from GET /api/agents/ (fetchAgents), reshaping every
// kind='bracket' Agent row via bracketAgentToTemplateOut/
// bracketRowsFromAgents — not fetchOrderTemplates()/GET /api/admin/
// templates/ directly anymore. See templates.js's own updated doc.
vi.mock('$lib/api', () => ({
  fetchAgents: vi.fn(),
}));

// Raw Agent(kind='bracket') rows, as GET /api/agents/ now returns them.
const SAMPLE_AGENT_ROWS = [
  {
    id: 101, legacy_template_id: 1, slug: 'default-bull', name: 'Default Bull',
    kind: 'bracket', is_system: true, status: 'active',
    bracket_config: { applies_to: 'buy_any', is_default: true },
  },
  {
    id: 102, legacy_template_id: 2, slug: 'default-bear', name: 'Default Bear',
    kind: 'bracket', is_system: true, status: 'active',
    bracket_config: { applies_to: 'sell_any', is_default: true },
  },
];

// Expected OrderTemplateOut-shaped output after bracketRowsFromAgents
// reshapes SAMPLE_AGENT_ROWS — `id` is `legacy_template_id`, NEVER the
// Agent row's own `id` (101/102 above must not leak through).
const SAMPLE_TEMPLATES = [
  {
    id: 1, slug: 'default-bull', name: 'Default Bull', description: '',
    applies_to: 'buy_any', tp_pct: null, sl_pct: null, wing_premium_pct: null,
    wing_strike_offset: null, wing_max_spread_pct: null, tp_order_type: 'LIMIT',
    tp_scales_json: null, sl_trail_pct: null, is_default: true, is_system: true,
    is_active: true, group: null, tags: [],
  },
  {
    id: 2, slug: 'default-bear', name: 'Default Bear', description: '',
    applies_to: 'sell_any', tp_pct: null, sl_pct: null, wing_premium_pct: null,
    wing_strike_offset: null, wing_max_spread_pct: null, tp_order_type: 'LIMIT',
    tp_scales_json: null, sl_trail_pct: null, is_default: true, is_system: true,
    is_active: true, group: null, tags: [],
  },
];

describe('loadOrderTemplates', () => {
  beforeEach(() => {
    vi.resetModules();
    vi.clearAllMocks();
  });

  it('returns the fetched templates on a successful call', async () => {
    const api = await import('$lib/api');
    vi.mocked(api.fetchAgents).mockResolvedValue(SAMPLE_AGENT_ROWS);
    const { loadOrderTemplates } = await import('$lib/data/templates.js');

    const result = await loadOrderTemplates();
    expect(result).toEqual(SAMPLE_TEMPLATES);
  });

  it('a failed fetch does NOT permanently cache an empty result — the next call retries and can succeed', async () => {
    const api = await import('$lib/api');
    const fetchMock = vi.mocked(api.fetchAgents);
    // First call fails (simulating the auth-timing race / transient network blip).
    fetchMock.mockRejectedValueOnce(new Error('network blip'));
    // Second call (a later SymbolPanel mount — e.g. opening a new order)
    // succeeds with real data.
    fetchMock.mockResolvedValueOnce(SAMPLE_AGENT_ROWS);

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
    const fetchMock = vi.mocked(api.fetchAgents);
    fetchMock.mockResolvedValue(SAMPLE_AGENT_ROWS);

    const { loadOrderTemplates } = await import('$lib/data/templates.js');

    await loadOrderTemplates();
    await loadOrderTemplates();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('a genuinely empty (but successful) response IS cached — not treated as a failure', async () => {
    const api = await import('$lib/api');
    const fetchMock = vi.mocked(api.fetchAgents);
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
    const fetchMock = vi.mocked(api.fetchAgents);
    /** @type {(v: any) => void} */
    let resolveFetch = () => {};
    fetchMock.mockReturnValue(new Promise((resolve) => { resolveFetch = resolve; }));

    const { loadOrderTemplates } = await import('$lib/data/templates.js');

    const p1 = loadOrderTemplates();
    const p2 = loadOrderTemplates();
    resolveFetch(SAMPLE_AGENT_ROWS);

    const [r1, r2] = await Promise.all([p1, p2]);
    expect(r1).toEqual(SAMPLE_TEMPLATES);
    expect(r2).toEqual(SAMPLE_TEMPLATES);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
