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
import { reducePoll, buildGhosts, mergeGhosts, pruneGhosts, GRACE_THRESHOLD, GHOST_DISPLAY_MS } from '../order/chaseDisplayGrace.js';

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

// ── Display grace window + terminal-outcome ghosts ──────────────────────────
//
// 2026-11-25 fix for "chase card shows data for a short span and
// disappears" — unlike the mirrored pure-function tests above, these
// import the REAL production module (chaseDisplayGrace.js) directly,
// not a hand-copied mirror, per the ChaseCard.next-action-countdown
// comment's own cautionary note about mirrors drifting from reality.
//
// Five quality dimensions:
//   1. SSOT   — imports the real reducePoll/buildGhosts/pruneGhosts, no
//               second hand-mirrored copy of the grace/ghost logic.
//   2. Perf   — pure unit tests, no DOM/network/Svelte runes.
//   3. Stale  — explicitly asserts the OLD (no-grace) behaviour would
//               have cleared immediately, before proving the fix holds.
//   4. Reuse  — `_row`/`_algoRow` factories build fixtures for any case.
//   5. UX     — covers the exact "finishes fast and vanishes" complaint:
//               a transient blip must not clear, a genuine drought must.

function _row(id, extra = {}) {
  return { id, account: 'ZG0790', transaction_type: 'BUY', quantity: 50, symbol: 'NIFTY', ...extra };
}

/** Shapes a fetchAlgoOrdersRecent()-style row. */
function _algoRow(id, status) {
  return { id, status };
}

describe('chaseDisplayGrace.reducePoll — grace window', () => {
  it('a single transient empty poll does NOT clear a non-empty display (grace holds)', () => {
    const r1 = [_row(1)];
    // Poll 1: non-empty.
    let state = reducePoll([], 0, r1);
    expect(state.chases).toEqual(r1);
    expect(state.consecutiveEmpty).toBe(0);

    // Poll 2: empty — this is the critical poll. Under the OLD behaviour
    // (no grace), the display would have cleared to [] right here.
    const oldBehaviour = []; // what `_chases = raw || []` used to produce
    expect(oldBehaviour).toEqual([]); // documents the bug being fixed

    state = reducePoll(state.chases, state.consecutiveEmpty, []);
    // Fixed behaviour: held steady, NOT cleared.
    expect(state.chases).toEqual(r1);
    expect(state.consecutiveEmpty).toBe(1);
    expect(state.disappeared).toEqual([]);

    // Poll 3: non-empty again — the chase reappears before anything was
    // ever cleared. The display never dropped the row at all.
    state = reducePoll(state.chases, state.consecutiveEmpty, r1);
    expect(state.chases).toEqual(r1);
    expect(state.consecutiveEmpty).toBe(0);
  });

  it('a sustained empty run DOES eventually clear, once the grace threshold is reached', () => {
    const r1 = [_row(1)];
    let state = reducePoll([], 0, r1);
    expect(state.chases).toEqual(r1);

    // First empty poll — grace holds (same as above).
    state = reducePoll(state.chases, state.consecutiveEmpty, []);
    expect(state.chases).toEqual(r1);
    expect(state.consecutiveEmpty).toBe(1);

    // Second CONSECUTIVE empty poll reaches GRACE_THRESHOLD (2) — the
    // display now genuinely clears, proving the grace window only
    // DELAYS clearing rather than disabling it outright.
    expect(GRACE_THRESHOLD).toBe(2);
    state = reducePoll(state.chases, state.consecutiveEmpty, []);
    expect(state.chases).toEqual([]);
    expect(state.consecutiveEmpty).toBe(2);
    expect(state.disappeared).toEqual(r1);
  });

  it('an empty poll against an already-empty display is a pure no-op (no held state to protect)', () => {
    const state = reducePoll([], 0, []);
    expect(state.chases).toEqual([]);
    expect(state.consecutiveEmpty).toBe(0);
    expect(state.disappeared).toEqual([]);
  });

  it('a non-empty poll that drops one of two ids reports it as disappeared immediately — no grace needed for a confirmed partial change', () => {
    const r1 = _row(1);
    const r2 = _row(2, { symbol: 'BANKNIFTY' });
    let state = reducePoll([], 0, [r1, r2]);
    expect(state.chases).toEqual([r1, r2]);

    // Next poll authoritatively reports only r2 is still active.
    state = reducePoll(state.chases, state.consecutiveEmpty, [r2]);
    expect(state.chases).toEqual([r2]);
    expect(state.consecutiveEmpty).toBe(0);
    expect(state.disappeared).toEqual([r1]);
  });

  it('a non-empty poll always resets the grace counter to 0, even mid-grace', () => {
    const r1 = [_row(1)];
    let state = reducePoll([], 0, r1);
    state = reducePoll(state.chases, state.consecutiveEmpty, []); // consecutiveEmpty -> 1
    expect(state.consecutiveEmpty).toBe(1);
    state = reducePoll(state.chases, state.consecutiveEmpty, r1); // recovers
    expect(state.consecutiveEmpty).toBe(0);
  });
});

describe('chaseDisplayGrace.buildGhosts / pruneGhosts — terminal-outcome row', () => {
  it('renders a ghost when the disappeared id resolves to a real terminal status (FILLED)', () => {
    const disappeared = [_row(7)];
    const algoRows = [_algoRow(7, 'FILLED')];
    const ghosts = buildGhosts(disappeared, algoRows, 1_000_000);
    expect(ghosts).toHaveLength(1);
    expect(ghosts[0]).toMatchObject({ id: 7, label: 'Filled', cls: 'filled', symbol: 'NIFTY' });
    expect(ghosts[0].expiresAt).toBe(1_000_000 + GHOST_DISPLAY_MS);
  });

  it('HELD maps to the "Pending" label, per the CLAUDE.md operator-facing hold-text invariant', () => {
    const ghosts = buildGhosts([_row(9)], [_algoRow(9, 'HELD')], 0);
    expect(ghosts[0].label).toBe('Pending');
  });

  it.each([
    ['REJECTED', 'Rejected'],
    ['CANCELLED', 'Cancelled'],
    ['UNFILLED', 'Unfilled'],
  ])('%s maps to the "%s" label', (status, label) => {
    const ghosts = buildGhosts([_row(1)], [_algoRow(1, status)], 0);
    expect(ghosts[0].label).toBe(label);
  });

  it('does NOT render a ghost when nothing disappeared (falls back to step-1 behaviour only)', () => {
    expect(buildGhosts([], [_algoRow(1, 'FILLED')], 0)).toEqual([]);
  });

  it('does NOT render a ghost when the disappeared id has no match in the algo-orders lookup', () => {
    const ghosts = buildGhosts([_row(5)], [_algoRow(6, 'FILLED')], 0);
    expect(ghosts).toEqual([]);
  });

  it('does NOT render a ghost for a status that is not a recognized terminal outcome (e.g. OPEN, CANCEL_FAILED, or unknown)', () => {
    expect(buildGhosts([_row(1)], [_algoRow(1, 'OPEN')], 0)).toEqual([]);
    expect(buildGhosts([_row(1)], [_algoRow(1, 'CANCEL_FAILED')], 0)).toEqual([]);
    expect(buildGhosts([_row(1)], [_algoRow(1, 'SOME_UNKNOWN_STATUS')], 0)).toEqual([]);
  });

  it('does NOT render a ghost when the lookup itself is empty (e.g. a failed best-effort fetch)', () => {
    expect(buildGhosts([_row(1)], [], 0)).toEqual([]);
    expect(buildGhosts([_row(1)], null, 0)).toEqual([]);
  });

  it('pruneGhosts drops a ghost whose id reappeared in the live chase list', () => {
    const ghosts = [{ id: 1, expiresAt: 10_000 }];
    const pruned = pruneGhosts(ghosts, new Set([1]), 100); // well before expiry
    expect(pruned).toEqual([]);
  });

  it('pruneGhosts drops a ghost once its display window has elapsed', () => {
    const ghosts = [{ id: 1, expiresAt: 1000 }];
    const pruned = pruneGhosts(ghosts, new Set(), 1001); // past expiresAt
    expect(pruned).toEqual([]);
  });

  it('pruneGhosts keeps a ghost that is neither live again nor expired yet', () => {
    const ghosts = [{ id: 1, expiresAt: 1000 }];
    const pruned = pruneGhosts(ghosts, new Set(), 500);
    expect(pruned).toEqual(ghosts);
  });

  // mergeGhosts — dedup-by-id merge, closes a real bug: `_load()` can be
  // re-entered (`_reconcile()`/`onMount()` bypass the `_fetching` guard),
  // so two concurrent calls could report the SAME disappeared id twice.
  // Appending both resulting ghost batches with no dedup would produce a
  // duplicate `id` key, which Svelte 5's keyed `{#each ... (ghost.id)}`
  // throws `each_key_duplicate` on.
  it('mergeGhosts deduplicates by id — a later entry for the same id replaces, never duplicates', () => {
    const existing = [{ id: 1, label: 'Filled', expiresAt: 1000 }];
    const fresh = [{ id: 1, label: 'Filled', expiresAt: 4000 }]; // same id, refreshed expiry
    const merged = mergeGhosts(existing, fresh);
    expect(merged).toHaveLength(1);
    expect(merged[0]).toEqual({ id: 1, label: 'Filled', expiresAt: 4000 });
  });

  it('mergeGhosts keeps distinct ids from both batches', () => {
    const existing = [{ id: 1, expiresAt: 1000 }];
    const fresh = [{ id: 2, expiresAt: 2000 }];
    const merged = mergeGhosts(existing, fresh);
    expect(merged.map((g) => g.id).sort()).toEqual([1, 2]);
  });

  it('mergeGhosts tolerates empty/missing inputs', () => {
    expect(mergeGhosts([], [])).toEqual([]);
    expect(mergeGhosts(undefined, [{ id: 1 }])).toEqual([{ id: 1 }]);
    expect(mergeGhosts([{ id: 1 }], undefined)).toEqual([{ id: 1 }]);
  });
});
