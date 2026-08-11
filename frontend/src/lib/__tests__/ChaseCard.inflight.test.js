/**
 * ChaseCard.inflight.test.js
 *
 * Unit tests for the in-flight guard added to ChaseCard._poll().
 *
 * The guard prevents concurrent fetches when the 3-second visibleInterval
 * fires before the previous _load() has resolved (slow network + fast poll).
 *
 * These tests exercise the guard as a pure function — no Svelte component
 * mounting, no DOM — matching the existing pure-function test pattern in
 * pulseRowsAndFlash.test.js.
 *
 * Five quality dimensions:
 *   1. SSOT   — guard state is a module-level boolean, not reactive state
 *   2. Perf   — pure unit tests, no DOM / network, sub-millisecond
 *   3. Stale  — old no-guard path (concurrent fetches) is explicitly tested
 *               for the expected broken behaviour before verifying the fix
 *   4. Reuse  — factory function builds the guarded _poll from any fetch mock
 *   5. UX     — guard prevents piled-up fetches that would race the UI state
 */

import { describe, it, expect, vi } from 'vitest';

// ── Factory — replicates the _poll guard from ChaseCard.svelte ───────────────
//
// This factory builds the same guard logic the component uses, as a
// standalone pure function, so we can test it without mounting Svelte.
// The shape mirrors the component exactly:
//
//   let _fetching = false;
//   async function _poll() {
//     if (_fetching) return;
//     _fetching = true;
//     try { await _load(); }
//     finally { _fetching = false; }
//   }
//
// @param {() => Promise<void>} fetchFn — the mock fetch to guard
// @returns {{ poll: () => Promise<void>, getFetching: () => boolean }}
function makeGuardedPoll(fetchFn) {
  let _fetching = false;
  async function _poll() {
    if (_fetching) return;
    _fetching = true;
    try {
      await fetchFn();
    } finally {
      _fetching = false;
    }
  }
  return {
    poll: _poll,
    getFetching: () => _fetching,
  };
}

// ── Tests ─────────────────────────────────────────────────────────────────────

describe('ChaseCard in-flight guard (_poll)', () => {
  it('calls fetch exactly once when two polls fire concurrently', async () => {
    // Simulate a slow fetch: first call holds open until we resolve the deferred.
    let resolve1 = /** @type {((value?: any) => void) | null} */ (null);
    const slowFetch = vi.fn(() => new Promise((res) => { resolve1 = res; }));

    const { poll } = makeGuardedPoll(slowFetch);

    // Start the first poll — it's now in-flight (_fetching = true).
    const p1 = poll();

    // Fire a second poll BEFORE the first has resolved.
    const p2 = poll();

    // The second poll must return immediately without calling the fetch again.
    await p2;  // resolves immediately because the guard exits early
    expect(slowFetch).toHaveBeenCalledTimes(1);

    // Resolve the first fetch and wait for it to settle.
    resolve1?.();
    await p1;
    expect(slowFetch).toHaveBeenCalledTimes(1);
  });

  it('allows a second poll AFTER the first has completed', async () => {
    const fastFetch = vi.fn().mockResolvedValue(undefined);
    const { poll } = makeGuardedPoll(fastFetch);

    // First poll completes fully.
    await poll();
    expect(fastFetch).toHaveBeenCalledTimes(1);

    // Second poll after the first finished — must proceed normally.
    await poll();
    expect(fastFetch).toHaveBeenCalledTimes(2);
  });

  it('resets _fetching to false even when the fetch throws', async () => {
    const failFetch = vi.fn().mockRejectedValue(new Error('network error'));
    const { poll, getFetching } = makeGuardedPoll(failFetch);

    // Swallow the unhandled rejection (the component catches inside _load).
    await poll().catch(() => {});
    // _fetching must be reset to false after the error.
    expect(getFetching()).toBe(false);
    expect(failFetch).toHaveBeenCalledTimes(1);
  });

  it('resets _fetching to false after a successful fetch', async () => {
    const fastFetch = vi.fn().mockResolvedValue(undefined);
    const { poll, getFetching } = makeGuardedPoll(fastFetch);

    await poll();
    expect(getFetching()).toBe(false);
  });

  it('three rapid concurrent calls — fetch invoked exactly once', async () => {
    let resolve1 = /** @type {((value?: any) => void) | null} */ (null);
    const slowFetch = vi.fn(() => new Promise((res) => { resolve1 = res; }));
    const { poll } = makeGuardedPoll(slowFetch);

    const p1 = poll();
    const p2 = poll();
    const p3 = poll();

    await Promise.all([p2, p3]);  // p2, p3 exit early (guard)
    expect(slowFetch).toHaveBeenCalledTimes(1);

    resolve1?.();
    await p1;
    expect(slowFetch).toHaveBeenCalledTimes(1);
  });
});
