/**
 * symbolContextHelpers.js — pure helpers shared by every host that wires
 * the unified SymbolContextMenu.svelte (MarketPulse, OrderBook, LogPanel,
 * PerformancePage, the derivatives Legs grid, /orders) so the "Open in
 * Options" and "Copy symbol" items behave identically everywhere instead
 * of five independent re-derivations (2026-10-22 context-menu parity
 * work — operator: "make sure all the values available in all context
 * menus").
 *
 * Deliberately dependency-free of any `.svelte.js` reactive module (see
 * `pulseDetached.svelte.js` for the `groupPropFor()` builder that
 * composes `hasFnOUnderlying()` below with live detach state) — a
 * top-level `$state(...)` call can't be evaluated by the plain `node`
 * environment Vitest runs this project's unit tests under (no
 * `@sveltejs/vite-plugin-svelte` registered in vitest.config.js), so
 * keeping this file's own imports plain lets its test import it directly
 * with no mocking.
 */

import { decomposeSymbol } from './decomposeSymbol.js';

/**
 * Derive the F&O underlying root to jump to from "Open in Options".
 * Mirrors MarketPulse's original inline derivation byte-for-byte: strip
 * a trailing digit-onward run (expiry/strike/CE/PE/FUT suffix) from the
 * tradingsymbol; fall back to a supplied underlying hint, then the raw
 * symbol itself.
 *
 * @param {string} tradingsymbol
 * @param {string|null} [underlyingHint]
 * @returns {string}
 */
export function rootForOptionsLink(tradingsymbol, underlyingHint) {
  const ts = String(tradingsymbol || '').toUpperCase();
  return ts.replace(/\d.*$/, '') || String(underlyingHint || '').replace(/\s+/g, '') || ts;
}

/**
 * Full `/admin/derivatives?u=<root>` href for "Open in Options".
 *
 * @param {string} tradingsymbol
 * @param {string|null} [underlyingHint]
 * @returns {string}
 */
export function openInOptionsHref(tradingsymbol, underlyingHint) {
  return `/admin/derivatives?u=${encodeURIComponent(rootForOptionsLink(tradingsymbol, underlyingHint))}`;
}

/**
 * True when the symbol is itself an option or future contract (has a
 * real underlying root distinct from a bare equity/index spot symbol).
 * Drives the Detach/Re-attach group item's `hasUnderlying` gate —
 * matches the same signal MarketPulse's own row-level `underlying` field
 * carries (populated only for opt/fut rows by pulseUnified.js's
 * `parseSymbol`/`fillSymbolMeta`), derived here purely from the symbol
 * string so surfaces whose rows don't carry a stored `underlying` field
 * (OrderBook, LogPanel, /orders) don't need new plumbing to know it.
 *
 * Known gaps vs. MarketPulse's own `row.underlying` signal (documented,
 * not fixed here): a bare MCX virtual-root anchor symbol (e.g.
 * "CRUDEOIL" with no trailing digits) decomposes as kind 'eq' here, and
 * an equity row grouped under an F&O underlying via Pulse's
 * `displayUnderlying` tagging has no such tag outside Pulse's own
 * buildUnified pipeline.
 *
 * @param {string} tradingsymbol
 * @returns {boolean}
 */
export function hasFnOUnderlying(tradingsymbol) {
  const kind = decomposeSymbol(tradingsymbol).kind;
  return kind === 'opt' || kind === 'fut';
}

/**
 * Copy a tradingsymbol to the clipboard. Swallows failure (clipboard API
 * unavailable/denied) — matches MarketPulse's original silent-catch
 * behaviour; callers that want operator feedback show their own toast.
 *
 * @param {string} tradingsymbol
 * @returns {Promise<boolean>} true on success
 */
export async function copySymbolToClipboard(tradingsymbol) {
  try {
    await navigator.clipboard.writeText(tradingsymbol || '');
    return true;
  } catch (_) {
    return false;
  }
}
