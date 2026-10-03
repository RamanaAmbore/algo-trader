/**
 * symbolStoreDirtySyms.test.js — coverage for symbolStore.svelte.js's
 * dirty-symbol tracking (`drainDirtySyms` / `getResetGen`, 2026-10 perf
 * fix) and MarketPulse.svelte's incremental `_liveLtpSnap` rebuilder that
 * consumes it.
 *
 * Neither file can be imported directly in this harness:
 *   - symbolStore.svelte.js has top-level `$state(...)` calls (`snapTick`,
 *     etc.) that execute at module-eval time — no svelte-compiler plugin
 *     in vitest.config.js (same constraint documented in
 *     symbolStoreArbitration.js's own header and every other
 *     `*.svelte.js` test file in this directory).
 *   - MarketPulse.svelte is a Svelte component (Playwright territory per
 *     CLAUDE.md's test-location map); the established convention for this
 *     file (see sourceRowClasses.test.js, marketpulse.test.js) is a
 *     hand-mirrored copy of the pure logic, kept in sync by comment.
 *
 * Coverage strategy: hand-mirrored pure-logic copies (exercising the
 * actual reference-stability / dirty-set-touch contract) PLUS a `?raw`
 * source-grep against both real shipped files confirming the mirrors
 * are actually wired in, not just independently plausible.
 *
 * Five quality dimensions:
 *  1. SSOT   — source-scan reads the real shipped files, not stale copies.
 *  2. Perf   — this IS the perf fix under test: reference-equality +
 *              dirty-set-bounded-touch assertions directly exercise the
 *              "don't rebuild/rescan the whole map every tick" invariant.
 *  3. Stale  — regression-guards the exact dirty-set-vs-tickBus distinction
 *              (no 250ms throttle, no hidden-tab gate, no first-write gate)
 *              that motivated NOT reusing tickBus for this.
 *  4. Reuse  — mirrors match the real _mergeSymbolWrite / softReset /
 *              hardReset / _refreshLtpSnapIncremental wiring exactly.
 *  5. UX     — a reset (softReset/hardReset) must still fully repaint
 *              _liveLtpSnap (no stale cells left over from before the
 *              reset) — covered by the reset-generation mismatch test.
 */

import { describe, it, expect } from 'vitest';
import symbolStoreSrc from '$lib/data/symbolStore.svelte.js?raw';
import marketPulseSrc from '$lib/MarketPulse.svelte?raw';

// ── Mirror: drainDirtySyms / getResetGen (symbolStore.svelte.js) ───────────

function makeDirtyTracker() {
  let dirtySyms = new Set();
  let resetGen = 0;
  return {
    write(key) { dirtySyms.add(key); },
    reset() { resetGen++; },
    drain() {
      const syms = dirtySyms;
      dirtySyms = new Set();
      return { syms, gen: resetGen };
    },
    getResetGen() { return resetGen; },
  };
}

describe('dirty-symbol tracker (mirror) — drain semantics', () => {
  it('drain returns exactly the symbols written since the last drain, then clears', () => {
    const t = makeDirtyTracker();
    t.write('NIFTY');
    t.write('BANKNIFTY');
    const first = t.drain();
    expect([...first.syms].sort()).toEqual(['BANKNIFTY', 'NIFTY']);

    // A second drain with no intervening writes is empty.
    const second = t.drain();
    expect(second.syms.size).toBe(0);
  });

  it('gen is stable across ordinary writes, bumps only on reset', () => {
    const t = makeDirtyTracker();
    t.write('NIFTY');
    expect(t.drain().gen).toBe(0);
    t.reset();
    t.write('NIFTY');
    expect(t.drain().gen).toBe(1);
  });

  it('duplicate writes to the same symbol before a drain collapse to one entry', () => {
    const t = makeDirtyTracker();
    t.write('NIFTY');
    t.write('NIFTY');
    t.write('NIFTY');
    const { syms } = t.drain();
    expect(syms.size).toBe(1);
  });
});

// ── Mirror: MarketPulse's _refreshLtpSnapIncremental core loop ─────────────
//
// Hand-mirrored copy of the per-symbol apply loop (not the $state /
// setTimeout plumbing, which needs the Svelte runtime) — see
// MarketPulse.svelte's _refreshLtpSnapIncremental for the real version.

function applyDirtyLtpUpdatesMirror(prevSnap, dirtySyms, getLtp) {
  if (dirtySyms.size === 0) return prevSnap;
  const next = { ...prevSnap };
  let changed = false;
  const touched = new Set();
  for (const sym of dirtySyms) {
    const v = getLtp(sym);
    if (v != null && Number.isFinite(v) && v > 0) {
      if (next[sym] !== v) { next[sym] = v; changed = true; touched.add(sym); }
    } else if (sym in next) {
      delete next[sym];
      changed = true;
      touched.add(sym);
    }
  }
  return changed ? { snap: next, touched } : { snap: prevSnap, touched: new Set() };
}

describe('_refreshLtpSnapIncremental core loop (mirror) — bounded-touch + reference stability', () => {
  it('returns the SAME snapshot reference when the dirty set is empty', () => {
    const prev = { NIFTY: 24000 };
    const result = applyDirtyLtpUpdatesMirror(prev, new Set(), () => 24000);
    expect(result).toBe(prev); // early-return path, not even wrapped
  });

  it('returns the SAME reference when every dirty symbol resolves to its existing value', () => {
    const prev = { NIFTY: 24000, BANKNIFTY: 51000 };
    const result = applyDirtyLtpUpdatesMirror(prev, new Set(['NIFTY']), () => 24000);
    expect(result.snap).toBe(prev);
    expect(result.touched.size).toBe(0);
  });

  it('returns a NEW reference and the correct value when a dirty symbol changed', () => {
    const prev = { NIFTY: 24000, BANKNIFTY: 51000 };
    const result = applyDirtyLtpUpdatesMirror(prev, new Set(['NIFTY']), () => 24050);
    expect(result.snap).not.toBe(prev);
    expect(result.snap).toEqual({ NIFTY: 24050, BANKNIFTY: 51000 });
    expect([...result.touched]).toEqual(['NIFTY']);
  });

  it('untouched symbols keep their value even when the whole object is rebuilt', () => {
    const prev = { NIFTY: 24000, BANKNIFTY: 51000, RELIANCE: 2900 };
    const result = applyDirtyLtpUpdatesMirror(prev, new Set(['NIFTY']), (sym) => (sym === 'NIFTY' ? 24050 : 0));
    expect(result.snap.BANKNIFTY).toBe(51000);
    expect(result.snap.RELIANCE).toBe(2900);
  });

  it('removes a symbol whose LTP became non-positive/stale (price-zero guard parity)', () => {
    const prev = { NIFTY: 24000 };
    const result = applyDirtyLtpUpdatesMirror(prev, new Set(['NIFTY']), () => 0);
    expect(result.snap).not.toBe(prev);
    expect('NIFTY' in result.snap).toBe(false);
  });

  it('only touches symbols in the dirty set — an unrelated symbol is never scanned/compared', () => {
    const prev = { NIFTY: 24000, BANKNIFTY: 51000 };
    let callCount = 0;
    const getLtp = (sym) => { callCount++; return prev[sym]; };
    applyDirtyLtpUpdatesMirror(prev, new Set(['NIFTY']), getLtp);
    expect(callCount).toBe(1); // BANKNIFTY never queried
  });
});

// ── Source-grep — symbolStore.svelte.js wiring ──────────────────────────────

describe('symbolStore.svelte.js — dirty-sym tracking wired into real write/reset paths', () => {
  const src = symbolStoreSrc;

  it('exports drainDirtySyms and getResetGen', () => {
    expect(src).toMatch(/export function drainDirtySyms\(\)/);
    expect(src).toMatch(/export function getResetGen\(\)/);
  });

  it('_mergeSymbolWrite marks the written symbol dirty (not gated by tickBus throttle)', () => {
    const setIdx = src.indexOf('symbolStore.set(key, next);');
    expect(setIdx, 'symbolStore.set(key, next) call not found').toBeGreaterThan(-1);
    const after = src.slice(setIdx, setIdx + 120);
    expect(after).toContain('_dirtySyms.add(key);');
  });

  it('softReset bumps _resetGen', () => {
    const idx = src.indexOf('export function softReset()');
    expect(idx).toBeGreaterThan(-1);
    const end = src.indexOf('\n}', idx);
    expect(src.slice(idx, end)).toContain('_resetGen++;');
  });

  it('hardReset bumps _resetGen', () => {
    const idx = src.indexOf('export function hardReset()');
    expect(idx).toBeGreaterThan(-1);
    const end = src.indexOf('\n}', idx);
    expect(src.slice(idx, end)).toContain('_resetGen++;');
  });
});

// ── Source-grep — MarketPulse.svelte wiring ─────────────────────────────────

describe('MarketPulse.svelte — _liveLtpSnap uses $state.raw + incremental dirty-set apply', () => {
  const src = marketPulseSrc;

  it('_liveLtpSnap is declared with $state.raw, not a deep-proxied $state', () => {
    expect(src).toMatch(/let _liveLtpSnap = \$state\.raw\(/);
  });

  it('imports drainDirtySyms and getResetGen from symbolStore.svelte.js', () => {
    const importLine = src.split('\n').find(l => l.includes("from '$lib/data/symbolStore.svelte.js'"));
    expect(importLine, 'symbolStore.svelte.js import line not found').toBeTruthy();
    expect(importLine).toContain('drainDirtySyms');
    expect(importLine).toContain('getResetGen');
  });

  it('the symbolTickCount flush timer calls the incremental rebuilder, not a full _buildLtpSnap rescan', () => {
    const idx = src.indexOf('const unsub = symbolTickCount.subscribe(() => {');
    expect(idx, 'symbolTickCount.subscribe wiring not found').toBeGreaterThan(-1);
    const end = src.indexOf('});', idx);
    const block = src.slice(idx, end);
    expect(block).toContain('_refreshLtpSnapIncremental();');
    expect(block).not.toContain('_buildLtpSnap();');
  });

  it('_refreshLtpSnapIncremental falls back to a full rebuild on a reset-generation mismatch', () => {
    const idx = src.indexOf('function _refreshLtpSnapIncremental()');
    expect(idx).toBeGreaterThan(-1);
    const end = src.indexOf('\n  }\n', idx + 40); // first closing of the function
    const block = src.slice(idx, idx + 1200);
    expect(block).toContain('drainDirtySyms()');
    expect(block).toContain('gen !== _liveLtpLastGen');
    expect(block).toContain('_buildLtpSnap()');
  });

  it('the paint-diff $effect no longer runs a full Object.keys(snap) pre-scan to detect "changed"', () => {
    const idx = src.indexOf("_lastPaintedSnap = /** @type {Record<string, number>} */ ({});");
    expect(idx, '_lastPaintedSnap declaration not found').toBeGreaterThan(-1);
    const effectIdx = src.indexOf('$effect(() => {', idx);
    const effectEnd = src.indexOf('\n  });', effectIdx);
    const block = src.slice(effectIdx, effectEnd);
    expect(block).not.toMatch(/for \(const k of snapKeys\)/);
    expect(block).toContain('_changedSincePaint');
  });
});
