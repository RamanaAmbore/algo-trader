/**
 * activityStore — shared tab + filter state for the activity log surfaces.
 *
 * Scope: ActivityLogModal + /activity page only. Embedded cards (dashboard
 * "news" card, /orders inline card, /automation panel) are intentionally
 * excluded — they have different default tabs and context-specific filter
 * needs, so coupling them to this store would cause the "Agents" tab
 * selected in the modal to flip the news card too.
 *
 * What persists:
 *   activeTab     — which LogPanel tab is selected
 *   accountFilter — selected account codes ([] = show all)
 *   levelFilter   — log-level filter chip
 *
 * What doesn't:
 *   availableAccounts — per-mount ephemeral (derived from current order rows
 *                       by LogPanel); different mounts have different row
 *                       sets, so a stale value from a closed modal would bleed
 *                       into the page's dropdown. Kept local per mount.
 *
 * Usage:
 *   import { activityStore } from '$lib/data/activityStore.svelte.js';
 *
 *   // Read (reactive — works in both component scripts and .svelte.js):
 *   activityStore.activeTab     // string
 *   activityStore.accountFilter // string[]
 *   activityStore.levelFilter   // 'all'|'error'|'warning'|'info'
 *
 *   // Write:
 *   activityStore.activeTab = 'agent';
 *   activityStore.setAccountFilter(['ZG0790']);
 *   activityStore.setLevelFilter('error');
 *
 * Shape mirrors the project's other `.svelte.js` stores: a single
 * module-level object with `$state`-backed reactive getters, no class.
 */

/** @typedef {'order'|'agent'|'terminal'|'simulator'|'system'|'conn'|'news'} ActivityTab */

/** Canonical allowed tab ids — kept in sync with LogPanel's default tab list. */
export const ACTIVITY_TABS = /** @type {ActivityTab[]} */ (
  ['order', 'agent', 'terminal', 'simulator', 'system', 'conn', 'news']
);

// ── Reactive backing state ───────────────────────────────────────────────
let _activeTab      = $state(/** @type {ActivityTab} */ ('order'));
let _accountFilter  = $state(/** @type {string[]} */ ([]));
let _levelFilter    = $state(/** @type {'all'|'error'|'warning'|'info'} */ ('all'));

export const activityStore = {
  // ── Getters ─────────────────────────────────────────────────────────
  get activeTab()     { return _activeTab;     },
  get accountFilter() { return _accountFilter; },
  get levelFilter()   { return _levelFilter;   },

  // ── Setters ─────────────────────────────────────────────────────────
  // Defined as proper JS property setters so Svelte 5 `bind:` directives
  // can write through: `bind:accountFilter={activityStore.accountFilter}`
  // calls `activityStore.accountFilter = newVal` on child change, which
  // routes through the setter to `_accountFilter = newVal`.
  // Note: for `bind:` to work correctly, both getter and setter must be
  // defined; Svelte 5 resolves `bind:x={obj.x}` as a get+set pair.

  /** Set the active tab. Ignores unknown ids (guards against stale URL params). */
  set activeTab(/** @type {string} */ id) {
    if (ACTIVITY_TABS.includes(/** @type {ActivityTab} */ (id))) {
      _activeTab = /** @type {ActivityTab} */ (id);
    }
  },

  /** Set the account filter array. */
  set accountFilter(/** @type {string[]} */ codes) {
    _accountFilter = codes;
  },

  /** Set the level filter. */
  set levelFilter(/** @type {'all'|'error'|'warning'|'info'} */ level) {
    _levelFilter = level;
  },
};
