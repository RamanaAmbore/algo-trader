/**
 * pulseDetached.svelte.js — single shared owner of the Pulse
 * underlying-group "detach / re-attach" override (2026-10-22 context-menu
 * parity work).
 *
 * Previously this was private `$state` inside MarketPulse.svelte
 * (`detachedSymbols`, keyed to localStorage `pulse:detached`). Once the
 * unified SymbolContextMenu's Detach/Re-attach item became available from
 * OTHER surfaces that can be mounted ALONGSIDE a live MarketPulse instance
 * in the same tab (LogPanel's ActivityLogModal is layout-mounted on every
 * (algo) route, including /pulse and /dashboard; MarketPulse itself is
 * also embedded on /dashboard), a second independent `$state` array
 * reading/writing the same localStorage key would race: a detach fired
 * from LogPanel's menu would never regroup the live Pulse grid, and
 * Pulse's own next save would blow away LogPanel's write (whole-array
 * overwrite). This module is the one place the array lives; every
 * consumer (MarketPulse.svelte included) imports it instead of keeping a
 * private copy.
 *
 * Storage format unchanged: a plain JSON array of uppercase tradingsymbols
 * under localStorage key `pulse:detached`.
 */

import { hasFnOUnderlying } from './symbolContextHelpers.js';

/** @type {string[]} */
let _detached = $state([]);
let _loaded = false;

function _ensureLoaded() {
  if (_loaded) return;
  _loaded = true;
  if (typeof localStorage === 'undefined') return;
  try {
    const d = JSON.parse(localStorage.getItem('pulse:detached') || '[]');
    if (Array.isArray(d)) _detached = d;
  } catch (_) { /* corrupt JSON — start fresh */ }
}

function _persist() {
  if (typeof localStorage === 'undefined') return;
  try { localStorage.setItem('pulse:detached', JSON.stringify(_detached)); } catch (_) { /* quota/private-mode */ }
}

/**
 * Live, reactive array of detached symbols. Read this inside a
 * `$derived`/`$derived.by` body (not destructured at module scope) so the
 * compiler can track later mutations — mirrors how MarketPulse's own
 * `groupOrder` dependency is touched in `unifiedRows`.
 *
 * @returns {string[]}
 */
export function detachedSymbolsList() {
  _ensureLoaded();
  return _detached;
}

/** @param {string} sym */
export function isDetached(sym) {
  _ensureLoaded();
  return _detached.includes(String(sym || '').toUpperCase());
}

/** @param {string} sym */
export function detachSymbol(sym) {
  _ensureLoaded();
  const s = String(sym || '').toUpperCase();
  if (!s || _detached.includes(s)) return;
  _detached = [..._detached, s];
  _persist();
}

/** @param {string} sym */
export function reattachSymbol(sym) {
  _ensureLoaded();
  const s = String(sym || '').toUpperCase();
  if (!_detached.includes(s)) return;
  _detached = _detached.filter((x) => x !== s);
  _persist();
}

/** Clears every detach override. Used by MarketPulse's "Reset overrides". */
export function resetDetached() {
  _ensureLoaded();
  _detached = [];
  _persist();
}

/**
 * Build SymbolContextMenu's `group` prop (detach/re-attach) for a bare
 * symbol string — the one-stop call every non-MarketPulse host
 * (OrderBook, LogPanel, PerformancePage, the derivatives Legs grid,
 * /orders) makes when opening the menu, so none of them need to
 * re-derive the isDetached/hasUnderlying pair by hand. Lives here
 * (rather than in the dependency-free symbolContextHelpers.js) because
 * it reads this module's own live `isDetached` state.
 *
 * @param {string} tradingsymbol
 * @returns {{ isDetached: boolean, hasUnderlying?: boolean } | null}
 */
export function groupPropFor(tradingsymbol) {
  const sym = String(tradingsymbol || '').toUpperCase();
  if (!sym) return null;
  if (isDetached(sym)) return { isDetached: true };
  if (hasFnOUnderlying(sym)) return { isDetached: false, hasUnderlying: true };
  return null;
}
