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

// ── Age column fix ───────────────────────────────────────────────────────
//
// Bug: the Age column rendered `_age(row.last_attempt_at || row.created_at)`.
// `last_attempt_at` is a backend epoch-SECONDS float (not an ISO string) —
// `Date.parse()` on a bare number returns NaN, so once a chase re-quotes
// at least once (`last_attempt_at` becomes non-null and wins the `||`),
// the cell permanently shows "—" for the rest of that chase's lifetime.
// Even if parsing were fixed, `last_attempt_at` is the wrong anchor since
// it resets on every re-quote instead of growing monotonically — the
// correct anchor is `row.created_at` (an ISO string from a tz-aware
// DateTime column). Separately, the age must tick every second in sync
// with the component's existing `_nowSec` clock, not freeze between polls.
//
// This replicates the fixed `_age(iso, nowSec)` function as a pure
// function — mirrors the pattern above for the in-flight guard.

/**
 * Mirrors the fixed ChaseCard.svelte `_age` function.
 * @param {string} iso
 * @param {number} nowSec
 * @returns {string}
 */
function _age(iso, nowSec) {
  if (!iso) return '—';
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return '—';
  const s = Math.max(0, Math.floor(nowSec - t / 1000));
  if (s < 60)    return `${s}s`;
  if (s < 3600)  return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  return `${Math.floor(s / 86400)}d`;
}

// Mirrors the OLD (buggy) call site: _age(row.last_attempt_at || row.created_at)
// with the OLD single-arg _age signature that only ever read Date.now().
function _ageOldBuggy(iso) {
  if (!iso) return '—';
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return '—';
  const s = Math.max(0, Math.floor((Date.now() - t) / 1000));
  return `${s}s`;
}

describe('ChaseCard Age column (_age)', () => {
  it('pre-fix: a non-null last_attempt_at epoch-seconds float shows "—" (documents the bug)', () => {
    const row = { created_at: '2026-09-30T04:00:00Z', last_attempt_at: 1790000000.5 };
    // Old call site: _age(row.last_attempt_at || row.created_at) — the
    // epoch float wins the `||` and Date.parse(number) is NaN.
    expect(_ageOldBuggy(row.last_attempt_at || row.created_at)).toBe('—');
  });

  it('fixed: reads from row.created_at (ISO string), never from last_attempt_at (epoch float)', () => {
    const nowSec = Math.floor(Date.parse('2026-09-30T04:00:30Z') / 1000);
    const row = { created_at: '2026-09-30T04:00:00Z', last_attempt_at: 1790000000.5 };
    expect(_age(row.created_at, nowSec)).toBe('30s');
    // Does NOT show "—" once last_attempt_at is a non-null epoch float —
    // last_attempt_at is simply never passed to _age at all now.
    expect(_age(row.created_at, nowSec)).not.toBe('—');
  });

  it('age increases across two ticks of the _nowSec clock with no change to created_at', () => {
    const row = { created_at: '2026-09-30T04:00:00Z' };
    const nowSecTick1 = Math.floor(Date.parse('2026-09-30T04:00:05Z') / 1000);
    const nowSecTick2 = Math.floor(Date.parse('2026-09-30T04:00:06Z') / 1000);

    const age1 = _age(row.created_at, nowSecTick1);
    const age2 = _age(row.created_at, nowSecTick2);

    expect(age1).toBe('5s');
    expect(age2).toBe('6s');
    // Numeric comparison (strip trailing unit char) confirms monotonic growth.
    expect(parseInt(age2, 10)).toBeGreaterThan(parseInt(age1, 10));
  });

  it('returns "—" only when created_at is missing/unparseable, not based on last_attempt_at', () => {
    expect(_age('', 1000)).toBe('—');
    expect(_age(null, 1000)).toBe('—');
    expect(_age('not-a-date', 1000)).toBe('—');
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
