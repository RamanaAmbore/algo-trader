/**
 * accounts.test.js — Vitest unit tests for loadAccounts().
 *
 * Guards the 2026-10-02 fix: a failed `/accounts/` fetch (network blip, or
 * an auth-token-not-yet-attached race very early in page load) must NOT be
 * cached as a permanent "confirmed zero accounts" result. Before the fix,
 * `if (_accounts) return _accounts;` checked truthiness — and an empty
 * array is truthy — so the catch block's `_accounts = []` poisoned every
 * future call in that browser tab's session: no retry, ever, regardless of
 * how many components remounted or re-called loadAccounts(). Exact same
 * bug class already fixed once in `templates.js:loadOrderTemplates()`
 * (commit 85ca09f5) — see `templates.test.js` for the sibling test.
 *
 * Five quality dimensions:
 *  1. SSOT  — exercises loadAccounts(), the single fetch boundary every
 *             account-picker / order-ticket consumer calls through.
 *  2. Perf  — mocked I/O; no live network calls.
 *  3. Stale — guards the exact truthiness bug pattern (empty array is
 *             truthy) so a future refactor can't silently reintroduce it.
 *  4. Reuse — loadAccounts() is the sole entry point; no bespoke
 *             fetch-and-cache logic duplicated elsewhere.
 *  5. UX    — a failed-then-retried fetch must recover to real account
 *             data on the next call, not stay permanently empty (which
 *             would strand the order ticket's account dropdown blank).
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('$lib/api', () => ({
  fetchAccounts: vi.fn(),
}));

const SAMPLE_RESPONSE = {
  accounts: [
    { account_id: 'ZG0790', display: 'ZG0790' },
    { account_id: 'ZJ6294', display: 'ZJ6294' },
  ],
  default_account: 'ZG0790',
  default_symbol: 'NIFTY',
};

describe('loadAccounts', () => {
  beforeEach(() => {
    vi.resetModules();
    vi.clearAllMocks();
  });

  it('returns the fetched accounts on a successful call', async () => {
    const api = await import('$lib/api');
    vi.mocked(api.fetchAccounts).mockResolvedValue(SAMPLE_RESPONSE);
    const { loadAccounts } = await import('$lib/data/accounts.js');

    const result = await loadAccounts();
    expect(result).toEqual(SAMPLE_RESPONSE.accounts);
  });

  it('a failed fetch does NOT permanently cache an empty result — the next call retries and can succeed', async () => {
    const api = await import('$lib/api');
    const fetchMock = vi.mocked(api.fetchAccounts);
    // First call fails (simulating the auth-timing race / transient network blip).
    fetchMock.mockRejectedValueOnce(new Error('network blip'));
    // Second call (e.g. a later order-modal open) succeeds with real data.
    fetchMock.mockResolvedValueOnce(SAMPLE_RESPONSE);

    const { loadAccounts, getAccountsSync } = await import('$lib/data/accounts.js');

    const firstResult = await loadAccounts();
    expect(firstResult).toEqual([]); // this caller sees an empty array (no crash)

    const secondResult = await loadAccounts();
    // THE REGRESSION GUARD: before the fix, this would ALSO be `[]` (the
    // truthy empty array short-circuited every future call) even though
    // the second fetch would have succeeded if actually attempted.
    expect(secondResult).toEqual(SAMPLE_RESPONSE.accounts);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    // Synchronous reader reflects the recovered data too.
    expect(getAccountsSync()).toEqual(SAMPLE_RESPONSE.accounts);
  });

  it('a successful fetch IS cached — a subsequent call does not re-fetch', async () => {
    const api = await import('$lib/api');
    const fetchMock = vi.mocked(api.fetchAccounts);
    fetchMock.mockResolvedValue(SAMPLE_RESPONSE);

    const { loadAccounts } = await import('$lib/data/accounts.js');

    await loadAccounts();
    await loadAccounts();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('a genuinely empty (but successful) response IS cached — not treated as a failure', async () => {
    const api = await import('$lib/api');
    const fetchMock = vi.mocked(api.fetchAccounts);
    fetchMock.mockResolvedValue({ accounts: [], default_account: '', default_symbol: '' });

    const { loadAccounts } = await import('$lib/data/accounts.js');

    const first = await loadAccounts();
    const second = await loadAccounts();
    expect(first).toEqual([]);
    expect(second).toEqual([]);
    // A real empty response (no configured broker accounts) is a valid,
    // cacheable answer — only a THROWN exception should skip caching.
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('concurrent calls while a fetch is in flight share the same promise (dedup)', async () => {
    const api = await import('$lib/api');
    const fetchMock = vi.mocked(api.fetchAccounts);
    /** @type {(v: any) => void} */
    let resolveFetch = () => {};
    fetchMock.mockReturnValue(new Promise((resolve) => { resolveFetch = resolve; }));

    const { loadAccounts } = await import('$lib/data/accounts.js');

    const p1 = loadAccounts();
    const p2 = loadAccounts();
    resolveFetch(SAMPLE_RESPONSE);

    const [r1, r2] = await Promise.all([p1, p2]);
    expect(r1).toEqual(SAMPLE_RESPONSE.accounts);
    expect(r2).toEqual(SAMPLE_RESPONSE.accounts);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
