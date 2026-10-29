/**
 * portfolioAggregatesTriggers.test.js
 *
 * §2 (NavStrip reactivity): portfolioStore.svelte.js's `portfolioAggregates`
 * derived values (P:2 lifetime, Margin, Cash, Holdings value/lifetime slots)
 * used to recompute ONLY on a price-tick-driven counter (`_tick`), missing
 * store updates from fills/polls. Fixed by adding `bookPollerTick` (from
 * marketDataStores.svelte.js — already used this way in PositionStrip.svelte
 * / RefreshButton.svelte) and `bookChanged` (from bookChanged.js, increments
 * immediately on a fill) as additional tracked dependencies.
 *
 * portfolioStore.svelte.js is a Svelte 5 runes module (`$state`/`$derived.by`)
 * that can't be imported directly into Vitest (no Svelte compiler in this
 * harness — see positionsDerivedStore.test.js / portfolioStore.test.js's own
 * local-mirror pattern for the established precedent). This file instead
 * verifies the fix structurally: every one of the seven portfolioAggregates
 * `$derived.by` blocks reads BOTH `bookPollerTick.value` and the bridged
 * `_bookChangedTick` as reactive dependencies, alongside the pre-existing
 * `_tick` counter — a regression guard against the trigger reads being
 * silently dropped in a future edit.
 *
 * Fix (item-1 audit, original pass): the three dependency-tracking lines
 * above (`void _tick`/`void bookPollerTick.value`/`void _bookChangedTick`)
 * were already present and correct — but each block's ACTUAL store read
 * (`positionsStore.value` / `pulseHoldingsStore.value` / `fundsStore.value`)
 * was wrapped in `untrack(() => ...)`. Since `bookChanged` fires on the
 * WebSocket fill-event BEFORE the store reload lands, and nothing re-ran
 * the derived once the reload DID land (the untracked read meant Svelte
 * never registered it as a dependency), these aggregates recomputed
 * against STALE (pre-reload) data and never caught up until an unrelated
 * price tick or the next poll. Fix: the store `.value` read itself must be
 * a TRACKED read (not wrapped in untrack) — `.value` is a `$state` getter,
 * so a direct (non-untrack) read inside the `$derived.by` body is both
 * safe and correct per CLAUDE.md's reactive-safety rule (untrack() is only
 * required for raw Map/getSnapshot-style reads that aren't $state
 * themselves — see the per-symbol getSnapshot() calls a few lines below
 * each store read, which correctly stay wrapped).
 *
 * Cleanup (item-8, round 4): once the store `.value` read above is itself
 * a TRACKED dependency, `void _tick`/`void bookPollerTick.value`/
 * `void _bookChangedTick` became REDUNDANT — the store's own `.set()` on
 * poll/fill completion already re-triggers these derived's; a separate
 * explicit tick/poll/bookChanged counter voided on top adds nothing.
 * `void _tick` specifically was also inconsistent with §1's poll-only
 * unification for positions/holdings (SSE-tick reactivity is reserved for
 * roots/underlyings — see `_rootSpotCache`, the one remaining `void _tick`
 * user in this file). All three void lines, the now-fully-unused
 * `bookPollerTick` import, and the now-fully-unused `_bookChangedTick`
 * bridge (state + subscribe + its `bookChanged` import) were removed.
 * The tracked store `.value` read itself (the actual item-1 fix) is
 * unchanged and still the thing that matters — this file's second describe
 * block below guards that.
 *
 * Five quality dimensions:
 *   1. SSOT   — reads the actual shipped source, not a stale copy
 *   2. Perf   — pure text scan, no DOM / network / Svelte compilation
 *   3. Stale  — guards against the store read being silently re-wrapped in
 *               untrack() (the actual item-1 root cause) AND against the
 *               now-redundant void-trio / dead bridge silently reappearing
 *   4. Reuse  — covers all 7 portfolioAggregates exports in one pass
 *   5. UX     — the tracked store read is what makes Margin/Cash/lifetime
 *               P&L update promptly on a fill during closed hours, without
 *               waiting for a live price tick that may never arrive
 */

import { describe, it, expect } from 'vitest';
// Vite `?raw` import — reads the source as a plain string without Node's
// `fs`/`path` (not available under this project's Vitest node types config).
// @ts-ignore — Vite raw-import suffix has no TS module declaration here.
import src from '$lib/data/portfolioStore.svelte.js?raw';

describe('portfolioAggregates — dead bookPollerTick/_bookChangedTick bridge removed (item-8, round 4)', () => {
  it('does NOT import bookPollerTick from marketDataStores.svelte.js — no consumer left', () => {
    expect(src).not.toMatch(/import\s*\{[^}]*bookPollerTick[^}]*\}\s*from\s*'\$lib\/data\/marketDataStores\.svelte\.js'/);
  });

  it('does NOT import bookChanged from bookChanged.js — no consumer left', () => {
    expect(src).not.toMatch(/import\s*\{\s*bookChanged\s*\}\s*from\s*'\$lib\/data\/bookChanged'/);
  });

  it('does NOT bridge bookChanged into a _bookChangedTick $state — the bridge was the only consumer and is gone', () => {
    expect(src).not.toMatch(/let _bookChangedTick = \$state\(0\)/);
    expect(src).not.toMatch(/bookChanged\.subscribe\(v => \{ _bookChangedTick = v; \}\)/);
  });
});

describe('portfolioAggregates — every $derived.by block has NO redundant void-trio (item-8, round 4)', () => {
  /** Names of the 7 portfolioAggregates cross-page derived values. */
  const NAMES = [
    '_livePositionsPnl',
    '_liveHoldingsTotal',
    '_liveHoldingsValue',
    '_liveCashTotal',
    '_longOptionsCashPaid',
    '_marginAvail',
    '_marginTotal',
  ];

  /**
   * Extract the body of `const <name> = $derived.by(() => { ... });` by
   * brace-matching from the declaration to its closing `});`.
   * @param {string} name
   * @returns {string}
   */
  function extractDerivedBody(name) {
    const start = src.indexOf(`const ${name} = $derived.by(() => {`);
    expect(start, `declaration for ${name} not found`).toBeGreaterThan(-1);
    const bodyStart = src.indexOf('{', start);
    let depth = 0;
    for (let i = bodyStart; i < src.length; i++) {
      if (src[i] === '{') depth++;
      else if (src[i] === '}') {
        depth--;
        if (depth === 0) return src.slice(bodyStart, i + 1);
      }
    }
    throw new Error(`unterminated block for ${name}`);
  }

  /** Which underlying store each aggregate reads, and the exact tracked
   *  read pattern that must appear (item-1 fix — see file header). */
  const STORE_READ = {
    _livePositionsPnl:   { store: 'positionsStore',      var: 'posRows' },
    _liveHoldingsTotal:  { store: 'pulseHoldingsStore',  var: 'holdRows' },
    _liveHoldingsValue:  { store: 'pulseHoldingsStore',  var: 'holdRows' },
    _liveCashTotal:      { store: 'fundsStore',          var: 'fundRows' },
    _longOptionsCashPaid:{ store: 'positionsStore',      var: 'posRows' },
    _marginAvail:        { store: 'fundsStore',          var: 'fundRows' },
    _marginTotal:        { store: 'fundsStore',          var: 'fundRows' },
  };

  for (const name of NAMES) {
    it(`${name} does NOT read void _tick / void bookPollerTick.value / void _bookChangedTick (redundant, removed item-8 round 4)`, () => {
      const body = extractDerivedBody(name);
      expect(body, `${name} still has a redundant void _tick`).not.toMatch(/void _tick;/);
      expect(body, `${name} still has a redundant void bookPollerTick.value`).not.toMatch(/void bookPollerTick\.value;/);
      expect(body, `${name} still has a redundant void _bookChangedTick`).not.toMatch(/void _bookChangedTick;/);
    });

    it(`${name}'s store read is TRACKED (item-1 fix) — NOT wrapped in untrack()`, () => {
      const body = extractDerivedBody(name);
      const { store, var: varName } = STORE_READ[name];
      // The actual fix: `const posRows = positionsStore.value;` (tracked),
      // NOT `const posRows = untrack(() => positionsStore.value);`
      // (untracked — the item-1 bug). A regex anchored on the exact
      // variable name + store avoids false-passing on some OTHER
      // untrack() call elsewhere in the same block (e.g. the per-symbol
      // getSnapshot() reads, which correctly stay wrapped).
      const trackedPattern = new RegExp(`const ${varName} = ${store}\\.value;`);
      const untrackedPattern = new RegExp(`const ${varName} = untrack\\(\\(\\) => ${store}\\.value\\);`);
      expect(body, `${name} must read ${store}.value directly (tracked)`).toMatch(trackedPattern);
      expect(body, `${name} must NOT wrap ${store}.value in untrack() — that was the item-1 bug`).not.toMatch(untrackedPattern);
    });
  }
});

describe('portfolioAggregates — _portfolio partial fallback (§2)', () => {
  it('the final collector no longer blocks ALL slots on every one of positions/holdings/funds landing', () => {
    // Old (buggy) guard: `if (!_posAgg || !_holdAgg || !_fundsAgg) return _last;`
    // — blocks the ENTIRE snapshot (including unrelated slices) whenever
    // ANY one dependency hasn't landed yet. Guard against this exact
    // pattern reappearing.
    expect(src).not.toMatch(/if \(!_posAgg \|\| !_holdAgg \|\| !_fundsAgg\) return _last;/);
  });

  it('the new guard only bails out to the previous snapshot when NONE of the three have landed (fresh = not-null AND not-degraded)', () => {
    // Real-money guard (2026-09): "landed" now means BOTH non-null AND
    // not backend-tagged degraded (stale_accounts substitution) — see
    // posFresh/holdFresh/fundsFresh below. A slice that is non-null but
    // degraded must NOT count as landed, or a partially-substituted
    // response would silently under-count instead of freezing at the
    // last known-good full snapshot.
    expect(src).toMatch(/const posFresh\s*=\s*_posAgg\s*&&\s*!posDegraded;/);
    expect(src).toMatch(/const holdFresh\s*=\s*_holdAgg\s*&&\s*!holdDegraded;/);
    expect(src).toMatch(/const fundsFresh\s*=\s*_fundsAgg\s*&&\s*!fundsDegraded;/);
    // 2026-09 Commit 2 completion (+ Commit 6 for holdings): the
    // early-return body now also forces `positions.fresh`/`holdings.fresh`
    // to false before returning `_last` — this is the simultaneous-outage
    // path (all three degraded at once), so a stale `fresh: true` baked
    // in during the last live cycle must not leak through untouched.
    expect(src).toMatch(/if \(!posFresh && !holdFresh && !fundsFresh\) \{/);
    expect(src).toMatch(/if \(_last\?\.positions\?\.fresh \|\| _last\?\.holdings\?\.fresh\) \{/);
    expect(src).toMatch(/positions: _last\.positions \? \{ \.\.\._last\.positions, fresh: false \} : _last\.positions,/);
    expect(src).toMatch(/holdings:\s*_last\.holdings\s*\? \{ \.\.\._last\.holdings,\s*fresh: false \} : _last\.holdings,/);
  });

  it('each slice\'s degraded flag is sourced from its own store\'s reactive .meta.degraded', () => {
    expect(src).toMatch(/const posDegraded\s*=\s*positionsStore\.meta\?\.degraded === true;/);
    expect(src).toMatch(/const holdDegraded\s*=\s*pulseHoldingsStore\.meta\?\.degraded === true;/);
    expect(src).toMatch(/const fundsDegraded\s*=\s*fundsStore\.meta\?\.degraded === true;/);
  });

  it('positions/holdings/funds each independently fall back to their own last-known or empty slice when not fresh', () => {
    // 2026-09 Commit 2 completion: `positions` is now always an object
    // (never the bare ternary) so a `fresh` flag can be attached on top,
    // reflecting THIS cycle's posFresh regardless of which branch supplied
    // the rest of the fields — spreading the fresh-computed slice OR the
    // frozen one, per-field fallback unchanged underneath.
    expect(src).toMatch(/\.\.\.\(posFresh \? \{/);
    expect(src).toMatch(/\} : \(_last\?\.positions \?\? _EMPTY_POSITIONS\)\)/);
    expect(src).toMatch(/fresh:\s*posFresh,/);
    // 2026-09 Commit 6: holdings now uses the SAME spread + explicit
    // `fresh` override pattern as positions (rows/fresh added).
    expect(src).toMatch(/holdings: \{/);
    expect(src).toMatch(/\.\.\.\(holdFresh \? _holdAgg : \(_last\?\.holdings \?\? _EMPTY_HOLDINGS\)\),/);
    expect(src).toMatch(/fresh: holdFresh,/);
    expect(src).toMatch(/funds:\s*fundsFresh \? _fundsAgg\s*: \(_last\?\.funds\s*\?\? _EMPTY_FUNDS\)/);
  });
});

// ── Real-money fix (2026-10): NavStrip P/H-pill null-vs-zero consistency
// with the margin/cash pills ─────────────────────────────────────────────
//
// Before this fix, `_lastLivePositionsPnl`/`_lastLiveHoldingsTotal`/
// `_lastLiveHoldingsValue` initialized to `0`, and `_EMPTY_POSITIONS.total.
// day_pnl`/`_EMPTY_HOLDINGS.total` also initialized to `0` — so "no poll
// has ever landed this session" and "broker confirmed a genuinely flat
// book" were indistinguishable, unlike `_lastLiveCashTotal`/`_marginAvail`/
// `_marginTotal` (already null-until-polled via fundsAggregate.js). These
// three sentinels and two cold-start defaults now match that convention.

describe('portfolioStore.svelte.js — _lastLiveXxx sentinels init to null, not 0 (real file)', () => {
  it('_lastLivePositionsPnl / _lastLiveHoldingsTotal / _lastLiveHoldingsValue all init to null', () => {
    expect(src).toMatch(/let _lastLivePositionsPnl\s*=\s*\/\*\*[^*]*\*\/\s*\(null\);/);
    expect(src).toMatch(/let _lastLiveHoldingsTotal\s*=\s*\/\*\*[^*]*\*\/\s*\(null\);/);
    expect(src).toMatch(/let _lastLiveHoldingsValue\s*=\s*\/\*\*[^*]*\*\/\s*\(null\);/);
  });

  it('_lastLongOptionsCashPaid is unchanged at 0 — out of scope, not its own NavStrip pill', () => {
    expect(src).toMatch(/let _lastLongOptionsCashPaid\s*=\s*0;/);
  });

  it('each $derived.by still returns its own _lastLiveXxx sentinel on a null/degraded read (freeze-to-last-good unchanged)', () => {
    expect(src).toMatch(/if \(!posRows \|\| positionsStore\.meta\?\.degraded\) return _lastLivePositionsPnl;/);
    expect(src).toMatch(/if \(!holdRows \|\| pulseHoldingsStore\.meta\?\.degraded\) return _lastLiveHoldingsTotal;/);
  });
});

describe('portfolioStore.svelte.js — cold-start defaults init day-delta fields to null, not 0 (real file)', () => {
  it('_EMPTY_POSITIONS.total.day_pnl is null; its lifetime/aux fields are unchanged at 0', () => {
    const idx = src.indexOf('const _EMPTY_POSITIONS = {');
    expect(idx, '_EMPTY_POSITIONS not found').toBeGreaterThan(-1);
    const blockEnd = src.indexOf('\n};', idx);
    const block = src.slice(idx, blockEnd);
    expect(block).toMatch(/total:\s*\{\s*day_pnl:\s*null,\s*exp_pnl:\s*0,\s*extrinsic:\s*0,\s*prev_mv:\s*0,\s*chg_pct:\s*null\s*\},/);
  });

  it('_EMPTY_HOLDINGS.total is null; its chg_pct/byKey/byAccount/rows/fresh fields are unchanged', () => {
    expect(src).toMatch(/const _EMPTY_HOLDINGS = \{ total: null, byKey: \{\}, byAccount: \{\}, chg_pct: null, chgPctByKey: \{\}, rows: \[\], fresh: false \};/);
  });

  it('the LIVE recompute object (_posAgg.posTotal) is unaffected — still inits day_pnl at a real 0, since it always gets overwritten by a real sum on a fresh cycle', () => {
    // Distinguishes the one-time cold-start PLACEHOLDER (_EMPTY_POSITIONS,
    // used only before the very first live cycle / while degraded) from
    // the PER-CYCLE accumulator inside _posAgg (posTotal), which must stay
    // a real 0 starting point so `posTotal.day_pnl += p._day_pnl` sums
    // correctly — changing this one to null would break every live cycle,
    // not just the cold-start placeholder.
    expect(src).toMatch(/const posTotal\s*=\s*\{ day_pnl: 0, exp_pnl: 0, extrinsic: 0, prev_mv: 0, chg_pct: null \};/);
  });
});

/**
 * Mirror of the freeze-to-last-good sentinel pattern: null until the first
 * successful (non-degraded) read, then a real number (even a confirmed 0)
 * forever after, surviving subsequent degraded/null reads.
 */
function makeFreezeToLastGoodMirror() {
  let last = /** @type {number|null} */ (null);
  return {
    read(rows, degraded) {
      if (!rows || degraded) return last;
      let s = 0;
      for (const v of rows) s += Number(v || 0);
      last = s;
      return s;
    },
  };
}

describe('freeze-to-last-good sentinel (mirror) — null before first poll, frozen real number after', () => {
  it('returns null before any successful read', () => {
    const agg = makeFreezeToLastGoodMirror();
    expect(agg.read(null, false)).toBeNull();
  });

  it('returns a real 0 on the first successful read of a confirmed-empty/flat set', () => {
    const agg = makeFreezeToLastGoodMirror();
    expect(agg.read([], false)).toBe(0);
  });

  it('returns a real nonzero number on a successful read', () => {
    const agg = makeFreezeToLastGoodMirror();
    expect(agg.read([100, 50], false)).toBe(150);
  });

  it('freezes at the last real number (never reverts to null) once degraded after a successful read', () => {
    const agg = makeFreezeToLastGoodMirror();
    expect(agg.read([100, 50], false)).toBe(150);
    expect(agg.read([999], true)).toBe(150); // degraded — frozen, not 999, not null
    expect(agg.read(null, false)).toBe(150); // null rows — frozen, not null
  });
});
