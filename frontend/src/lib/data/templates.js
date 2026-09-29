/**
 * Order-template catalog cache — memoised module-level loader for
 * /api/admin/templates/. Three surfaces currently fetch the catalog
 * on mount (OrderTicket, /admin/derivatives Optimize tab, and
 * /automation/templates);
 * without this cache, opening the order modal hits the DB once per
 * mount + once per nav to the templates page.
 *
 * Pattern mirrors `loadAccounts()` in `accounts.js`. Single in-flight
 * promise dedups concurrent callers; cached array survives subsequent
 * calls until reload() is invoked (e.g. after CRUD on /automation/templates).
 */

import { writable } from 'svelte/store';
import { fetchOrderTemplates as _apiFetch } from '$lib/api';

/** @type {any[] | null} */
let _templates = null;
/** @type {Promise<any[]> | null} */
let _loadPromise = null;

/** Reactive read-side for templates list. Subscribe in components that
 *  need to react to CRUD mutations elsewhere (e.g. /automation/templates
 *  edits → other open modals re-render with the new values). */
export const orderTemplatesStore = writable(/** @type {any[]} */ ([]));

export async function loadOrderTemplates() {
  if (_templates) return _templates;
  if (_loadPromise) return _loadPromise;
  _loadPromise = (async () => {
    try {
      const rows = await _apiFetch();
      _templates = Array.isArray(rows) ? rows : [];
      orderTemplatesStore.set(_templates);
      return _templates;
    } catch (e) {
      // 2026-09-30 fix — DO NOT cache a failed fetch as `[]`. `if
      // (_templates) return _templates;` above checks truthiness, and an
      // empty array is truthy — so caching a transient failure (auth
      // token not yet attached this early in page load, a momentary
      // network blip) as `[]` PERMANENTLY poisoned every future call in
      // that browser tab for the rest of its session: no retry, ever,
      // regardless of how many times the operator switched symbols/tabs
      // (only a genuine fresh tab reset `_templates` back to `null` —
      // and if the same early-load race happened again, it broke again
      // the same way). This is why the Templ toggle worked in fresh
      // Playwright test runs (auth pre-seeded before navigation, no
      // race) but could stay permanently invisible in a real operator
      // session that happened to hit this race once. Leave `_templates`
      // as `null` on failure so the NEXT call retries fresh instead of
      // reusing a poisoned empty result. Return `[]` to THIS caller only
      // (doesn't change the shape callers expect), without caching it.
      orderTemplatesStore.set([]);
      return [];
    }
  })();
  try { return await _loadPromise; }
  finally { _loadPromise = null; }
}

/** Force a fresh fetch — call after templates CRUD so all subscribers
 *  see the new values. Used by the /automation/templates page after
 *  save / delete; idempotent on concurrent calls. */
export async function reloadOrderTemplates() {
  _templates = null;
  return loadOrderTemplates();
}

/** Synchronous read of the cache. Returns [] until the first
 *  load resolves. Useful when the caller can render with an empty
 *  catalog and update reactively via the store. */
export function getCachedOrderTemplates() {
  return _templates || [];
}

// Kick off the fetch when the module evaluates (browser-only) so by
// the time any modal mounts, the catalog is warm.
if (typeof window !== 'undefined') {
  loadOrderTemplates().catch(() => { /* silent — store stays empty */ });
}
