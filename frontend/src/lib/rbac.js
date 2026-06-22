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
  'admin', 'trader', 'risk', 'ops', 'observer',
]);

/** Legacy role aliases — map to canonical. Mirrors normalise_role() server-side. */
const LEGACY_MAP = {
  partner:    'observer',
  designated: 'admin',
};

export function normaliseRole(/** @type {string | null | undefined} */ role) {
  if (!role) return 'observer';
  const r = String(role).trim().toLowerCase();
  if (LEGACY_MAP[r]) return LEGACY_MAP[r];
  return r;
}

/** Fallback capability map used while /whoami is in flight. Sync this
 *  loosely with backend/api/rbac.py::CAPS — staleness is acceptable
 *  because the server still rejects unauthorised mutations.
 */
const FALLBACK_CAPS = /** @type {Record<string, ReadonlyArray<string>>} */ ({
  // reads
  view_aggregate:           ['admin', 'trader', 'risk', 'ops', 'observer', 'demo'],
  view_all_books:           ['admin', 'trader', 'risk', 'ops', 'demo'],
  view_derivatives:         ['admin', 'trader', 'risk', 'demo'],
  view_strategies_catalog:  ['admin', 'trader', 'risk', 'observer', 'demo'],
  view_agents_catalog:      ['admin', 'trader', 'risk', 'demo'],
  view_settings_readonly:   ['admin', 'risk', 'ops', 'demo'],
  view_audit:               ['admin', 'risk', 'ops'],
  view_users:               ['admin'],
  view_brokers:             ['admin', 'ops', 'risk', 'demo'],
  view_lab:                 ['admin', 'trader', 'risk', 'demo'],
  view_pulse:               ['admin', 'trader', 'risk', 'ops', 'demo'],
  view_charts:              ['admin', 'trader', 'risk', 'ops', 'demo'],
  view_market_summary:      ['admin', 'trader', 'risk', 'ops', 'observer', 'demo'],
  // trading
  place_order:              ['admin', 'trader'],
  modify_order:             ['admin', 'trader'],
  cancel_order:             ['admin', 'trader'],
  // strategies + agents
  manage_own_strategies:    ['admin', 'trader'],
  reassign_strategies:      ['admin'],
  manage_own_agents:        ['admin', 'trader'],
  disable_any_agent:        ['admin', 'risk'],
  manage_grammar_tokens:    ['admin'],
  // risk / settings / brokers / users
  adjust_risk_floors:       ['admin', 'risk'],
  manage_settings:          ['admin'],
  manage_brokers:           ['admin', 'ops'],
  test_broker_connection:   ['admin', 'ops'],
  manage_users:             ['admin'],
  approve_users:            ['admin'],
  manage_admins:            ['admin'],
  impersonate:              ['admin'],
  // sim / replay / lab
  run_simulator:            ['admin', 'trader', 'risk', 'demo'],
  run_replay:               ['admin', 'trader', 'risk', 'demo'],
  manage_lab_threads:       ['admin', 'trader'],
  mint_mcp_token:           ['admin'],
  export_reports:           ['admin', 'trader', 'risk', 'ops', 'observer'],
  use_mcp_tools:            ['admin', 'trader'],
});

function fallbackHasCap(/** @type {string} */ role, /** @type {string} */ cap) {
  const r = normaliseRole(role);
  const allowed = FALLBACK_CAPS[cap];
  if (!allowed) return false;       // unknown cap → fail closed
  return allowed.includes(r);
}

// ── Reactive stores ─────────────────────────────────────────────────────

/** Current effective role (normalised). Bootstrap = 'demo' until /whoami
 *  responds; on dev / non-prod the bootstrap is 'observer' so dev pages
 *  that gate off caps don't show as "demo mode" before auth resolves. */
const _bootRole = (typeof window !== 'undefined'
  && (window.location?.hostname?.startsWith('dev.')
      || window.location?.hostname === 'localhost'
      || window.location?.hostname === '127.0.0.1'))
  ? 'observer'
  : 'demo';

export const userRole = writable(/** @type {string} */ (_bootRole));
export const userCaps = writable(/** @type {string[]} */ ([]));

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
  }
}
