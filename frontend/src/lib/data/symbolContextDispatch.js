/**
 * symbolContextDispatch.js — shared handler for the four context-menu
 * extras (Copy symbol, Detach/Re-attach, Add/Remove watchlist) on every
 * host EXCEPT MarketPulse, which keeps its own richer watchlist handling
 * (targets whichever list is focused in its own UI, not the generic
 * default list) and its own toast-free copy/detach handlers (2026-10-22
 * context-menu parity work).
 *
 * Each of OrderBook/LogPanel/PerformancePage/the derivatives Legs grid/
 * /orders calls `dispatchSharedContextAction(action, symbol, exchange)`
 * from its SymbolContextMenu `onAction` callback BEFORE falling through
 * to its own `place-order`/`chart`/`close`/`orders`/`log` handling —
 * `isSharedContextAction(action)` tells the caller whether to stop there
 * (the base/draft items are deliberately NOT handled here; each host
 * already has its own modal-mount plumbing for those).
 */

import { toast } from './toastStore.svelte.js';
import { detachSymbol, reattachSymbol } from './pulseDetached.svelte.js';
import { copySymbolToClipboard, openInOptionsHref } from './symbolContextHelpers.js';
import { addSymbolToDefaultWatchlist, removeSymbolFromWatchlist } from './watchlistContextActions.js';

const _SHARED_ACTIONS = new Set([
  'copy-symbol', 'open-options', 'detach', 'reattach', 'add-watchlist', 'remove-watchlist',
]);

/** @param {string} action */
export function isSharedContextAction(action) {
  return _SHARED_ACTIONS.has(action);
}

/**
 * @param {string} action
 * @param {string} symbol
 * @param {string} [exchange]
 * @returns {Promise<void>}
 */
export async function dispatchSharedContextAction(action, symbol, exchange) {
  switch (action) {
    case 'open-options':
      window.location.href = openInOptionsHref(symbol);
      return;
    case 'copy-symbol': {
      const ok = await copySymbolToClipboard(symbol);
      if (ok) toast.success('Symbol copied'); else toast.error('Copy failed');
      return;
    }
    case 'detach':
      // Only visibly regroups the Pulse grid — see pulseDetached.svelte.js
      // docblock. Toasted here (unlike MarketPulse's own silent version)
      // because this surface shows no visual feedback otherwise.
      detachSymbol(symbol);
      toast.success('Detached from Pulse group');
      return;
    case 'reattach':
      reattachSymbol(symbol);
      toast.success('Re-attached to Pulse group');
      return;
    case 'add-watchlist': {
      const r = await addSymbolToDefaultWatchlist(symbol, exchange || '');
      if (r.ok) toast.success('Added to watchlist'); else toast.error(r.reason || 'Add failed');
      return;
    }
    case 'remove-watchlist': {
      const r = await removeSymbolFromWatchlist(symbol);
      if (r.ok) toast.success('Removed from watchlist'); else toast.error(r.reason || 'Remove failed');
      return;
    }
    default:
      return;
  }
}
