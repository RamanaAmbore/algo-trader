/**
 * Frontend RBAC mirror.
 *
 * The backend (`backend/api/rbac.py`) is the authoritative source of
 * truth — every route's capability gate runs server-side and a UI
 * that hides a button is NOT a security boundary. This module exists
 * for two reasons:
 *
 *   1. UX. Hiding the "Manage brokers" nav item from a trader avoids
 *      a confusing 403 toast. Disabling the Submit button on the
 *      order ticket when the operator is in demo mode avoids a
 *      pointless round-trip.
 *
 *   2. Bootstrap. /api/auth/whoami returns `{ role, caps }` so the
 *      frontend doesn't have to re-implement the matrix. We surface
 *      that via a `$userCaps` derived store; consumer code calls
 *      `hasCap('cap_name')` instead of branching on role string.
 *
 * The matrix below is a FALLBACK used only when the whoami fetch
 * hasn't resolved yet (initial mount). It must stay in rough sync
 * with backend/api/rbac.py — but stale fallback is fine because the
 * server enforces the real check.
 */

import { derived, writable } from 'svelte/store';

/** Canonical assignable roles (matches backend ASSIGNABLE_ROLES). */
export const ASSIGNABLE_ROLES = /** @type {const} */ ([
  'designated', 'trader', 'risk', 'admin', 'partner',
]);

/** Sanity check + lowercase the role string. All five canonical roles
 *  are operator-domain words; there are no legacy aliases to map any
 *  more — the role-rename migration (Jun 2026) settled the DB on the
 *  canonical values and bumped token_version on every affected row to
 *  invalidate stale JWTs. */
export function normaliseRole(/** @type {string | null | undefined} */ role) {
  if (!role) return 'partner';
  return String(role).trim().toLowerCase();
}

/** Fallback capability map used while /whoami is in flight. Sync this
 *  loosely with backend/api/rbac.py::CAPS — staleness is acceptable
 *  because the server still rejects unauthorised mutations.
 */
const FALLBACK_CAPS = /** @type {Record<string, ReadonlyArray<string>>} */ ({
  // reads
  view_aggregate:           ['designated', 'trader', 'risk', 'admin', 'partner', 'demo'],
  view_all_books:           ['designated', 'trader', 'risk', 'admin', 'demo'],
  view_derivatives:         ['designated', 'trader', 'risk', 'demo'],
  view_strategies_catalog:  ['designated', 'trader', 'risk', 'partner', 'demo'],
  view_agents_catalog:      ['designated', 'trader', 'risk', 'demo'],
  view_settings_readonly:   ['designated', 'risk', 'admin', 'demo'],
  view_audit:               ['designated', 'risk', 'admin'],
  view_users:               ['designated'],
  view_brokers:             ['designated', 'admin', 'risk', 'demo'],
  view_lab:                 ['designated', 'trader', 'risk', 'demo'],
  view_pulse:               ['designated', 'trader', 'risk', 'admin', 'demo'],
  view_charts:              ['designated', 'trader', 'risk', 'admin', 'demo'],
  view_market_summary:      ['designated', 'trader', 'risk', 'admin', 'partner', 'demo'],
  // trading
  place_order:              ['designated', 'trader'],
  modify_order:             ['designated', 'trader'],
  cancel_order:             ['designated', 'trader'],
  // strategies + agents
  view_strategies:          ['designated', 'trader', 'risk', 'admin', 'partner', 'demo'],
  manage_own_strategies:    ['designated', 'trader'],
  reassign_strategies:      ['designated'],
  manage_own_agents:        ['designated', 'trader'],
  disable_any_agent:        ['designated', 'risk'],
  manage_grammar_tokens:    ['designated'],
  // risk / settings / brokers / users
  adjust_risk_floors:       ['designated', 'risk'],
  manage_settings:          ['designated'],
  view_hedge_proxies:       ['designated', 'trader', 'risk', 'demo'],
  manage_hedge_proxies:     ['designated', 'trader'],
  manage_brokers:           ['designated', 'admin'],
  test_broker_connection:   ['designated', 'admin'],
  manage_users:             ['designated'],
  approve_users:            ['designated'],
  manage_admins:            ['designated'],
  impersonate:              ['designated'],
  manage_investor_tokens:   ['designated'],
  // sim / replay / lab
  run_simulator:            ['designated', 'trader', 'risk', 'demo'],
  run_replay:               ['designated', 'trader', 'risk', 'demo'],
  manage_lab_threads:       ['designated', 'trader'],
  mint_mcp_token:           ['designated'],
  export_reports:           ['designated', 'trader', 'risk', 'admin', 'partner'],
  view_nav:                 ['designated', 'trader', 'risk', 'admin', 'partner', 'demo'],
  trigger_nav_compute:      ['designated', 'admin'],
  use_mcp_tools:            ['designated', 'trader'],
});

function fallbackHasCap(/** @type {string} */ role, /** @type {string} */ cap) {
  const r = normaliseRole(role);
  const allowed = FALLBACK_CAPS[cap];
  if (!allowed) return false;       // unknown cap → fail closed
  return allowed.includes(r);
}

// ── Reactive stores ─────────────────────────────────────────────────────

/** Current effective role (normalised). Bootstrap = 'demo' until /whoami
 *  responds; on dev / non-prod the bootstrap is 'partner' so dev pages
 *  that gate off caps don't show as "demo mode" before auth resolves. */
const _bootRole = (typeof window !== 'undefined'
  && (window.location?.hostname?.startsWith('dev.')
      || window.location?.hostname === 'localhost'
      || window.location?.hostname === '127.0.0.1'))
  ? 'partner'
  : 'demo';

export const userRole = writable(/** @type {string} */ (_bootRole));
export const userCaps = writable(/** @type {string[]} */ ([]));

/** Flips to true once bootstrapRBAC() settles (success or failure). Pages
 *  that gate on narrow caps (manage_settings, manage_brokers, etc.) should
 *  wait for this before rendering an access-denied panel — the initial
 *  userCaps=[] would otherwise trigger a false-negative during the brief
 *  whoami in-flight window and show a spurious "Access denied" banner to
 *  legitimately-authorised users. */
export const userCapsReady = writable(false);

/** Convenience derived — quick "is this demo?" check.  Most call sites
 *  should use hasCap() instead; this is for marketing-banner predicates. */
export const isDemo = derived(userRole, $r => normaliseRole($r) === 'demo');

/** Has-cap predicate. Prefers the freshly-fetched caps list from
 *  /whoami; falls back to the local matrix during the boot window. */
export function hasCap(/** @type {string} */ cap, /** @type {string[]|null} */ caps, /** @type {string|null} */ role) {
  if (caps && caps.length > 0) return caps.includes(cap);
  return fallbackHasCap(role || _bootRole, cap);
}

/** Reactive `hasCap` — useful in `class:disabled={!$has('place_order')}`
 *  expressions. Returns a function: hasFn => hasFn(capName) -> bool. */
export const has = derived(
  [userCaps, userRole],
  ([$caps, $role]) => (/** @type {string} */ cap) => hasCap(cap, $caps, $role),
);

/** One-shot bootstrap. Call from a top-level layout so every page sees
 *  the resolved role. Idempotent — safe to call from multiple mounts. */
let _bootstrapped = false;
export async function bootstrapRBAC() {
  if (_bootstrapped) return;
  _bootstrapped = true;
  try {
    const { fetchWhoami } = await import('$lib/api');
    const me = await fetchWhoami();
    if (me?.role) userRole.set(normaliseRole(me.role));
    if (Array.isArray(me?.caps)) userCaps.set(me.caps);
  } catch {
    // /whoami failed — keep boot defaults. The server still enforces
    // the real gate so a wrong UI guess just means a 403 if the
    // operator clicks something they shouldn't see.
  } finally {
    // Always mark ready — even on network failure, the fallback matrix
    // is the best we can do. Pages waiting on userCapsReady can now
    // render their access-denied panel (or grant) based on that fallback.
    userCapsReady.set(true);
  }
}
