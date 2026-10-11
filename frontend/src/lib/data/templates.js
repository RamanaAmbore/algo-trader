/**
 * Order-template ("Bracket") catalog cache — memoised module-level
 * loader. Several surfaces fetch the catalog on mount (OrderTicket,
 * /admin/derivatives Optimize tab, TemplateBar, SymbolPanel, the
 * Automation page's Brackets kind-card, console's `bracket="slug"`
 * resolution); without this cache, opening the order modal hits the DB
 * once per mount + once per nav to the Brackets card.
 *
 * Sprint E (Agent/Bracket/Template unification phase) — sources from
 * `GET /api/agents/` (every `kind='bracket'` row, reshaped via
 * `bracketAgentToTemplateOut`/`bracketRowsFromAgents` in
 * automationKinds.js) instead of `GET /api/admin/templates/` directly.
 * The RECONSTRUCTED row shape is held byte-for-byte identical to the
 * old `OrderTemplateOut` shape (including `id` = the real, canonical
 * `OrderTemplate.id`, via `Agent.legacy_template_id` — never the
 * Agent row's own `id`) so every existing consumer below needs ZERO
 * change. CRUD (create/patch/delete) still POSTs to
 * `/api/admin/templates/` (`createOrderTemplate`/`patchOrderTemplate`
 * in api.js, unchanged) — that write path now ALSO mirrors into the
 * matching Agent row server-side (backend.api.algo.bracket_registry),
 * so a `reloadOrderTemplates()` call after a CRUD mutation re-fetches
 * from `/api/agents/` and sees the fresh mirror immediately.
 *
 * Pattern mirrors `loadAccounts()` in `accounts.js`. Single in-flight
 * promise dedups concurrent callers; cached array survives subsequent
 * calls until reload() is invoked (e.g. after CRUD on the Brackets
 * kind-card).
 */

import { writable } from 'svelte/store';
import { fetchAgents } from '$lib/api';
import { bracketRowsFromAgents } from '$lib/data/automationKinds';

/** @type {any[] | null} */
let _templates = null;
/** @type {Promise<any[]> | null} */
let _loadPromise = null;

/** Reactive read-side for templates list. Subscribe in components that
 *  need to react to CRUD mutations elsewhere (e.g. Brackets tab
 *  edits → other open modals re-render with the new values). */
export const orderTemplatesStore = writable(/** @type {any[]} */ ([]));

export async function loadOrderTemplates() {
  if (_templates) return _templates;
  if (_loadPromise) return _loadPromise;
  _loadPromise = (async () => {
    try {
      const agentRows = await fetchAgents();
      const rows = bracketRowsFromAgents(agentRows);
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
 *  see the new values. Used by the Brackets tab after
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
