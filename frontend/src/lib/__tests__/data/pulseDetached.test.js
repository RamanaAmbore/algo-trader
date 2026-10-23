/**
 * pulseDetached.test.js — coverage for pulseDetached.svelte.js, the
 * single shared owner of Pulse's underlying-group detach/re-attach
 * override (2026-10-22 context-menu parity work; extracted from
 * MarketPulse.svelte's own private `$state` so LogPanel's
 * layout-mounted ActivityLogModal — co-mounted with a live MarketPulse
 * on /pulse and /dashboard — writes to the SAME array instead of racing
 * a second independent copy).
 *
 * pulseDetached.svelte.js has a top-level `$state([])` call that
 * executes at module-eval time, so it can't be imported directly in
 * this harness (no svelte-compiler plugin in vitest.config.js — same
 * documented constraint as marketDataStoresMeta.test.js /
 * fillWatchIntervalMs.test.js). Two-part coverage instead:
 *  1. A pure-function mirror of the array-membership logic
 *     (isDetached/detachSymbol/reattachSymbol/resetDetached), which is
 *     a trivial uppercase-normalise + include/filter — real behavioural
 *     coverage of the LOGIC even though the real module's reactive
 *     wiring can't be exercised here.
 *  2. A source-scan (Vite `?raw` import — same convention as
 *     marketDataStoresMeta.test.js) confirming the real shipped file:
 *       - persists to the exact localStorage key `pulse:detached`
 *         (MarketPulse's pre-existing key — changing it would silently
 *         orphan every operator's already-saved overrides)
 *       - exports every function the five SymbolContextMenu hosts
 *         (MarketPulse, OrderBook, LogPanel, PerformancePage, the
 *         derivatives Legs grid) actually import
 *       - `groupPropFor` checks `isDetached` before `hasFnOUnderlying`
 *         (a detached symbol must always show "Re-attach", regardless
 *         of whether it still parses as an option/future)
 *
 * Five quality dimensions:
 *  1. SSOT   — asserts there is exactly ONE `$state` array + ONE
 *              localStorage key in the file (a second independent copy
 *              would silently reintroduce the race this extraction fixed).
 *  2. Perf   — pure array ops, no I/O in the mirror; one string read for
 *              the source-scan.
 *  3. Stale  — regression-guards the EXACT localStorage key string and
 *              the function export names every host imports.
 *  4. Reuse  — the mirror's semantics (uppercase-normalise, no-op on a
 *              no-change detach/reattach) match MarketPulse's original
 *              inline implementation byte-for-byte.
 *  5. UX     — groupPropFor's isDetached-before-hasFnOUnderlying order
 *              is the exact invariant the Re-attach escape hatch
 *              depends on (SymbolContextMenu.svelte's own docblock).
 */

import { describe, it, expect } from 'vitest';
import src from '$lib/data/pulseDetached.svelte.js?raw';

// ── Pure-function mirror of the array-membership logic ────────────────────

function makeDetachedStore() {
  let detached = [];
  return {
    isDetached(sym) { return detached.includes(String(sym || '').toUpperCase()); },
    detach(sym) {
      const s = String(sym || '').toUpperCase();
      if (!s || detached.includes(s)) return;
      detached = [...detached, s];
    },
    reattach(sym) {
      const s = String(sym || '').toUpperCase();
      if (!detached.includes(s)) return;
      detached = detached.filter((x) => x !== s);
    },
    reset() { detached = []; },
    list() { return detached; },
  };
}

describe('pulseDetached mirror — isDetached/detach/reattach', () => {
  it('starts with nothing detached', () => {
    const s = makeDetachedStore();
    expect(s.isDetached('NIFTY')).toBe(false);
  });

  it('detach adds the uppercased symbol', () => {
    const s = makeDetachedStore();
    s.detach('nifty25apr22000ce');
    expect(s.isDetached('NIFTY25APR22000CE')).toBe(true);
    expect(s.list()).toEqual(['NIFTY25APR22000CE']);
  });

  it('detach is idempotent — detaching twice does not duplicate', () => {
    const s = makeDetachedStore();
    s.detach('NIFTY');
    s.detach('NIFTY');
    expect(s.list()).toEqual(['NIFTY']);
  });

  it('detach on an empty symbol is a no-op', () => {
    const s = makeDetachedStore();
    s.detach('');
    expect(s.list()).toEqual([]);
  });

  it('reattach removes the symbol', () => {
    const s = makeDetachedStore();
    s.detach('NIFTY');
    s.reattach('NIFTY');
    expect(s.isDetached('NIFTY')).toBe(false);
  });

  it('reattach on a symbol that was never detached is a no-op', () => {
    const s = makeDetachedStore();
    s.detach('NIFTY');
    s.reattach('BANKNIFTY');
    expect(s.list()).toEqual(['NIFTY']);
  });

  it('reset clears every detached symbol', () => {
    const s = makeDetachedStore();
    s.detach('NIFTY');
    s.detach('BANKNIFTY');
    s.reset();
    expect(s.list()).toEqual([]);
  });
});

// ── Source-scan ─────────────────────────────────────────────────────────────

describe('pulseDetached.svelte.js — source invariants', () => {
  it('persists to the exact pre-existing localStorage key', () => {
    expect(src).toContain("'pulse:detached'");
  });

  it('has exactly one top-level $state array (single owner, no second copy)', () => {
    const matches = src.match(/\$state\(\[\]\)/g) || [];
    expect(matches.length).toBe(1);
  });

  it('exports every function the five SymbolContextMenu hosts import', () => {
    for (const fn of [
      'detachedSymbolsList', 'isDetached', 'detachSymbol',
      'reattachSymbol', 'resetDetached', 'groupPropFor',
    ]) {
      expect(src).toContain(`export function ${fn}(`);
    }
  });

  it('groupPropFor checks isDetached before hasFnOUnderlying', () => {
    const start = src.indexOf('function groupPropFor(');
    expect(start).toBeGreaterThan(-1);
    const body = src.slice(start, src.indexOf('\n}', start));
    const isDetachedIdx = body.indexOf('isDetached(sym)');
    const hasUnderlyingIdx = body.indexOf('hasFnOUnderlying(sym)');
    expect(isDetachedIdx).toBeGreaterThan(-1);
    expect(hasUnderlyingIdx).toBeGreaterThan(-1);
    expect(isDetachedIdx).toBeLessThan(hasUnderlyingIdx);
  });

  it('does not depend on any .svelte.js module other than itself', () => {
    // symbolContextHelpers.js is plain (no runes) by design — see its own
    // docblock — so pulseDetached.svelte.js importing it is fine; this
    // guards against a future edit pulling in a second reactive module.
    /** @type {string[]} */
    const importLines = src.match(/^import .*$/gm) || [];
    const svelteJsImports = importLines.filter((l) => l.includes(".svelte.js'"));
    expect(svelteJsImports).toEqual([]);
  });
});
