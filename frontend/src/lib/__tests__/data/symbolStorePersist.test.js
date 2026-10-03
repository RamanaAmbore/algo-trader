/**
 * symbolStorePersist.test.js — coverage for symbolStore.svelte.js's
 * localStorage persist throttle + per-write eviction (2026-10 perf fix).
 *
 * symbolStore.svelte.js has top-level `$state(...)` calls that execute
 * at module-eval time, so it can't be imported directly in this harness
 * (same constraint documented in every other `*.svelte.js` test file in
 * this directory). Coverage strategy:
 *  1. `buildPersistPayload` is a plain, exported, pure function (array/
 *     iterable in, object out) — but importing the WHOLE module to reach
 *     it still fails, so it's exercised via a hand-mirrored copy here,
 *     same convention as computeRootSpotCache / warnPrevMvNullOnce in
 *     portfolioStore.test.js.
 *  2. A `?raw` source-grep confirms the real file: (a) defines
 *     buildPersistPayload with the exact eviction predicate, (b) wires
 *     it into `_schedulePersist` instead of the old unconditional
 *     `for...of symbolStore.entries()` copy, and (c) the debounce
 *     constant is 5000, not 500.
 *  3. A fake-timers test on the debounce ARM-ONCE semantics itself,
 *     mirroring `_schedulePersist`'s exact timer-guard shape (the real
 *     function can't be driven directly, but the guard logic — "first
 *     write arms a timer; writes landing before it fires don't rearm or
 *     extend it" — is trivial, self-contained, and worth locking down
 *     explicitly since a RESETTING debounce would never fire at all
 *     under continuous ticks).
 *
 * Five quality dimensions:
 *  1. SSOT   — source-scan reads the real shipped file, not a stale copy.
 *  2. Perf   — this IS the perf fix under test: write-cadence + payload-
 *              size (eviction) are both directly asserted.
 *  3. Stale  — regression-guards the exact "touched===0 never pruned"
 *              defensive convention shared with the hydration-time prune.
 *  4. Reuse  — mirrors the identical rule already used by symbolStore's
 *              own `_hydrate()` function (not duplicated ad hoc).
 *  5. UX     — closing-value survival across Fri→Mon / holiday clusters
 *              (the whole reason `_PRUNE_AGE_MS` is 7 days, not shorter)
 *              is preserved — the eviction window is UNCHANGED, only
 *              WHEN it's applied (every write, not just hydration).
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import symbolStoreSrc from '$lib/data/symbolStore.svelte.js?raw';

// ── Mirror: buildPersistPayload ──────────────────────────────────────────

function buildPersistPayloadMirror(entries, now, pruneAgeMs) {
  const out = {};
  for (const [sym, snap] of entries) {
    const touched = Number(snap?.touched_at) || 0;
    if (touched && now - touched > pruneAgeMs) continue;
    out[sym] = snap;
  }
  return out;
}

const DAY_MS = 24 * 60 * 60 * 1000;
const PRUNE_AGE_MS = 7 * DAY_MS;

describe('buildPersistPayload (mirror) — per-write eviction', () => {
  it('keeps a freshly-touched entry', () => {
    const now = 1_000_000_000_000;
    const entries = [['NIFTY', { ltp: 24000, touched_at: now - 1000 }]];
    const out = buildPersistPayloadMirror(entries, now, PRUNE_AGE_MS);
    expect(out).toEqual({ NIFTY: { ltp: 24000, touched_at: now - 1000 } });
  });

  it('evicts an entry older than pruneAgeMs', () => {
    const now = 1_000_000_000_000;
    const entries = [
      ['NIFTY', { ltp: 24000, touched_at: now - 1000 }],
      ['STALECO', { ltp: 50, touched_at: now - (PRUNE_AGE_MS + DAY_MS) }],
    ];
    const out = buildPersistPayloadMirror(entries, now, PRUNE_AGE_MS);
    expect(Object.keys(out)).toEqual(['NIFTY']);
  });

  it('keeps an entry exactly at the prune boundary (strict > only)', () => {
    const now = 1_000_000_000_000;
    const entries = [['NIFTY', { ltp: 24000, touched_at: now - PRUNE_AGE_MS }]];
    const out = buildPersistPayloadMirror(entries, now, PRUNE_AGE_MS);
    expect(out).toEqual({ NIFTY: { ltp: 24000, touched_at: now - PRUNE_AGE_MS } });
  });

  it('never prunes an entry with touched_at === 0 (unstamped, defensive convention)', () => {
    const now = 1_000_000_000_000;
    const entries = [['NIFTY', { ltp: 24000, touched_at: 0 }]];
    const out = buildPersistPayloadMirror(entries, now, PRUNE_AGE_MS);
    expect(out).toEqual({ NIFTY: { ltp: 24000, touched_at: 0 } });
  });

  it('handles an empty entries iterable', () => {
    expect(buildPersistPayloadMirror([], Date.now(), PRUNE_AGE_MS)).toEqual({});
  });

  it('survives a Friday close → Monday open gap (≈65h) without eviction', () => {
    const now = 1_000_000_000_000;
    const friCloseAgeMs = 65 * 60 * 60 * 1000; // 65h, well under the 7-day window
    const entries = [['RELIANCE', { ltp: 2900, touched_at: now - friCloseAgeMs }]];
    const out = buildPersistPayloadMirror(entries, now, PRUNE_AGE_MS);
    expect(out).toEqual({ RELIANCE: { ltp: 2900, touched_at: now - friCloseAgeMs } });
  });
});

// ── Source-grep — real file wiring ──────────────────────────────────────

describe('symbolStore.svelte.js — persist throttle + eviction wiring (real file)', () => {
  const src = symbolStoreSrc;

  it('exports buildPersistPayload with the exact eviction predicate', () => {
    expect(src).toMatch(/export function buildPersistPayload\(/);
    expect(src).toContain('if (touched && now - touched > pruneAgeMs) continue;');
  });

  it('debounce constant is 5000ms, not the old 500ms', () => {
    expect(src).toMatch(/const _PERSIST_DEBOUNCE_MS = 5000;/);
  });

  it('_schedulePersist calls buildPersistPayload with _PRUNE_AGE_MS, not an unconditional full copy', () => {
    const idx = src.indexOf('function _schedulePersist()');
    expect(idx).toBeGreaterThan(-1);
    const end = src.indexOf('\n}', idx);
    const block = src.slice(idx, end);
    expect(block).toContain('buildPersistPayload(symbolStore.entries(), Date.now(), _PRUNE_AGE_MS)');
    expect(block).toContain('_PERSIST_DEBOUNCE_MS');
    // The old unconditional copy loop must be gone from this function.
    expect(block).not.toMatch(/for \(const \[sym, snap\] of symbolStore\.entries\(\)\) \{\s*out\[sym\] = snap;/);
  });
});

// ── Arm-once debounce semantics (fake timers) ────────────────────────────
//
// _schedulePersist's actual guard (`if (_persistTimer != null) return;`)
// is a trivial, self-contained shape — mirrored here with fake timers to
// lock down the "arm once, don't extend on subsequent writes" contract
// explicitly, since a RESETTING debounce (clearTimeout + re-setTimeout on
// every write) would never fire at all under a continuous tick stream.

describe('persist debounce (mirror) — arm-once, not resetting', () => {
  let timer = null;
  let fireCount = 0;
  function scheduleMirror(debounceMs) {
    if (timer != null) return;
    timer = setTimeout(() => { timer = null; fireCount++; }, debounceMs);
  }

  beforeEach(() => {
    vi.useFakeTimers();
    timer = null;
    fireCount = 0;
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('fires exactly once per 5s window even under continuous writes every 100ms', () => {
    for (let t = 0; t < 4900; t += 100) {
      scheduleMirror(5000);
      vi.advanceTimersByTime(100);
    }
    expect(fireCount).toBe(0); // hasn't reached 5000ms yet from the FIRST arm
    vi.advanceTimersByTime(200);
    expect(fireCount).toBe(1);
  });

  it('a write landing AFTER the timer fires arms a fresh window', () => {
    scheduleMirror(5000);
    vi.advanceTimersByTime(5000);
    expect(fireCount).toBe(1);
    scheduleMirror(5000);
    vi.advanceTimersByTime(5000);
    expect(fireCount).toBe(2);
  });
});
