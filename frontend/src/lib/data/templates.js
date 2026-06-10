/**
 * Order-template catalog cache — memoised module-level loader for
 * /api/admin/templates/. Three surfaces currently fetch the catalog
 * on mount (OrderTicket, TemplateAttachPanel, /automation/templates);
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
    } catch (e) {
      _templates = [];
    }
    orderTemplatesStore.set(_templates);
    return _templates;
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
