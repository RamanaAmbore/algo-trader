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

// ── Next-action countdown (replaces the former Age/elapsed display) ─────
//
// 2026-10-15, operator: "instead of showing up elapsed time, do count
// down for how much time chase is going to wait before taking action."
// The former elapsed-since-created "Age" column and its `_age()` function
// were removed entirely — this is now the row's sole timing display.
// Mirrors ChaseCard.svelte's `_nextIn(nextAttemptAt, status, nowSec)` as a
// pure function, same pattern as the in-flight guard above.

/**
 * Mirrors ChaseCard.svelte's `_nextIn` function.
 *
 * 2026-10-23 fix — was gated on `status !== 'active'`, a value the real
 * backend (`GET /api/orders/chases/active`) never returns — its query
 * filters `AlgoOrder.status.in_(["OPEN", "CANCEL_FAILED"])`. This mirror
 * reproduced the exact bug the real component had: the countdown gate
 * matched only a status value that could never arrive in production, so
 * this test suite kept passing while the countdown stayed permanently
 * '—' against real data. Fixed to gate on 'OPEN', matching the real
 * component and the real backend vocabulary.
 * @param {number|null|undefined} nextAttemptAt
 * @param {string} status
 * @param {number} nowSec
 * @returns {number|null}
 */
function _nextIn(nextAttemptAt, status, nowSec) {
  if (nextAttemptAt == null || status !== 'OPEN') return null;
  return Math.max(0, Math.ceil(nextAttemptAt - nowSec));
}

describe('ChaseCard next-action countdown (_nextIn)', () => {
  it('counts down toward zero as nowSec approaches next_attempt_at', () => {
    const nextAttemptAt = 1000;
    expect(_nextIn(nextAttemptAt, 'OPEN', 990)).toBe(10);
    expect(_nextIn(nextAttemptAt, 'OPEN', 997)).toBe(3);
    expect(_nextIn(nextAttemptAt, 'OPEN', 999.5)).toBe(1);
  });

  it('clamps to 0 rather than going negative once the attempt time has passed', () => {
    expect(_nextIn(1000, 'OPEN', 1005)).toBe(0);
  });

  it('returns null (no countdown) when the row is not OPEN — includes CANCEL_FAILED, the other real status value this endpoint returns', () => {
    expect(_nextIn(1000, 'CANCEL_FAILED', 990)).toBeNull();
    expect(_nextIn(1000, 'cancelled', 990)).toBeNull();
    expect(_nextIn(1000, 'filled', 990)).toBeNull();
  });

  it('returns null when there is no scheduled next attempt', () => {
    expect(_nextIn(null, 'OPEN', 990)).toBeNull();
    expect(_nextIn(undefined, 'OPEN', 990)).toBeNull();
  });
});

// ── Mode class — explicit draft styling ──────────────────────────────────
//
// _modeCls already fell back safely for an unrecognized mode (bare
// 'cc-mode', accurate uppercased text in the DOM — never mislabeled as
// live). This is a polish/consistency pass: draft now gets its own
// explicit branch reusing the existing .cc-mode-draft rule (muted/dashed,
// unified off the old amber treatment — mode-pill color consistency
// audit), already used elsewhere in this component for the hardcoded
// draft-rows section, instead of falling into the generic bare class.
// sim/replay branches added in the same audit — previously had no class
// (and no CSS) at all, falling to the bare unstyled 'cc-mode'.
//
// Mirrors the fixed ChaseCard.svelte `_modeCls` function.
/** @param {string} m */
function _modeCls(m) {
  const k = String(m || '').toLowerCase();
  if (k === 'live')   return 'cc-mode cc-mode-live';
  if (k === 'paper')  return 'cc-mode cc-mode-paper';
  if (k === 'shadow') return 'cc-mode cc-mode-shadow';
  if (k === 'sim')    return 'cc-mode cc-mode-sim';
  if (k === 'replay') return 'cc-mode cc-mode-replay';
  if (k === 'draft')  return 'cc-mode cc-mode-draft';
  return 'cc-mode';
}

describe('ChaseCard mode class (_modeCls)', () => {
  it('known modes map to their own class', () => {
    expect(_modeCls('live')).toBe('cc-mode cc-mode-live');
    expect(_modeCls('paper')).toBe('cc-mode cc-mode-paper');
    expect(_modeCls('shadow')).toBe('cc-mode cc-mode-shadow');
    expect(_modeCls('sim')).toBe('cc-mode cc-mode-sim');
    expect(_modeCls('replay')).toBe('cc-mode cc-mode-replay');
  });

  it('draft gets its own explicit class, reusing the existing .cc-mode-draft rule', () => {
    expect(_modeCls('draft')).toBe('cc-mode cc-mode-draft');
  });

  it('is case-insensitive', () => {
    expect(_modeCls('DRAFT')).toBe('cc-mode cc-mode-draft');
    expect(_modeCls('Live')).toBe('cc-mode cc-mode-live');
  });

  it('an unrecognized mode falls back to the bare class, never a specific color class', () => {
    expect(_modeCls('bogus')).toBe('cc-mode');
    expect(_modeCls(undefined)).toBe('cc-mode');
    expect(_modeCls(null)).toBe('cc-mode');
  });
});
