/**
 * Shared per-account color palette + colour mapping.
 *
 * Each operator account (ZG0790 / ZJ6294 / …) gets a stable colour.
 * The same code lands on the same colour everywhere in the UI —
 * PerformancePage account column stripes, MarketPulse right-grid
 * symbol cell tint, BrokerHealthBadge, NavBreakdown, derivatives, etc.
 *
 * TOTAL rows + null accounts return null (caller uses transparent /
 * no tint).
 *
 * Colour assignment — rank-based, with djb2-hash fallback:
 *   With only 5 real accounts (ZG0790/ZJ6294/DH6847/DH3747/GR87DF) a
 *   djb2 hash mod a short palette collides (verified 2026-09 audit —
 *   BOTH the 7-hue and an earlier 8-hue duplicate palette produced at
 *   least one shared colour). A hash can't be fixed by picking a
 *   different palette length short of the full account count, so the
 *   primary path is now POSITION in the canonical account order
 *   (`accountDisplayOrder` / `sortAccountsBy` in `accountSort.js`,
 *   already deterministic and already the operator-configured
 *   ordering) — guaranteed collision-free as long as the account
 *   count stays under the palette length.
 *
 *   `setAccountColorRank()` is called by `accountSort.js` once the
 *   order map loads from the API, seeding `_rankedAccounts`. Until
 *   then (first paint, or callers passing a non-account string like
 *   an underlying symbol at `derivatives/+page.svelte`, or a masked
 *   string on public/investor surfaces that never load the order
 *   map), `acctColor()` falls back to the original djb2 hash so every
 *   caller still gets *a* stable colour — just not collision-free
 *   for the small real-account edge case.
 *
 *   account.js intentionally does NOT import accountSort.js directly
 *   (that would pull `$lib/api` into this leaf module's graph) — the
 *   push comes from accountSort.js calling `setAccountColorRank()`.
 */

export const ACCT_PALETTE = [
  '#fbbf24', // amber
  '#7dd3fc', // sky
  '#a78bfa', // violet
  '#4ade80', // green
  '#f472b6', // pink
  '#a5b4fc', // indigo
  '#f0abfc', // fuchsia
];

/** @type {string[] | null} */
let _rankedAccounts = null;

/**
 * Seed the deterministic rank list `acctColor()` uses in preference to
 * the hash fallback. Called by `accountSort.js` after its order map
 * loads (position = sorted index by display_order then account_id).
 * @param {string[] | null | undefined} rankedAccounts
 */
export function setAccountColorRank(rankedAccounts) {
  _rankedAccounts = Array.isArray(rankedAccounts) ? rankedAccounts : null;
}

/** @param {string | null | undefined} account */
export function acctColor(account) {
  if (!account || account === 'TOTAL') return null;
  if (_rankedAccounts) {
    const idx = _rankedAccounts.indexOf(account);
    if (idx !== -1) return ACCT_PALETTE[idx % ACCT_PALETTE.length];
  }
  // Fallback: djb2 hash — used before the order map has loaded, for
  // non-account strings (e.g. underlying symbols), and for masked
  // account strings on surfaces that never fetch the order map.
  let h = 5381;
  for (let i = 0; i < account.length; i++) {
    h = ((h << 5) + h) ^ account.charCodeAt(i);
    h = h >>> 0; // force unsigned 32-bit
  }
  return ACCT_PALETTE[h % ACCT_PALETTE.length];
}

/**
 * Shared JS-side helper for ag-Grid `cellStyle` callbacks — every
 * dark-theme surface's account column (`.ag-col-acct`) calls this to
 * inject the ONE custom property (`--acct-color`) the shared `app.css`
 * rule reads for the left-edge stripe + 14% background tint. Replaces
 * the differently-named per-surface variables each surface used to
 * invent (`--bh-acct-color` in BrokerHealthBadge, `--acct-stripe` in
 * NavBreakdown/MarketPulse/PerformancePage).
 *
 * ag-Grid in this codebase uses vanilla-JS cellRenderers (no
 * ag-grid-Svelte bridge) — this stays a plain function, not a shared
 * Svelte component, since a mounted component would need manual
 * per-cell mount/destroy lifecycle and become a second implementation.
 *
 * PerformancePage.svelte (ag-theme-ramboq, the public cream page) is
 * intentionally NOT wired to `--acct-color` — it keeps its own
 * isolated `--acct-stripe` custom property + scoped CSS rule (see A1/A2
 * audit notes in NavBreakdown.svelte / app.css). Do not point it at
 * this helper without operator sign-off.
 *
 * @param {string | null | undefined} account
 * @returns {{ '--acct-color': string }}
 */
export function acctStyleVars(account) {
  return { '--acct-color': acctColor(account) || 'transparent' };
}

/**
 * Pick the lead account from a row's `accounts` Set / array. Used
 * when colour-coding the symbol cell — a multi-account row is rare
 * but real (same symbol held in 2 accounts), so we tint by the first
 * account and let the rendered Account column show the full list.
 *
 * @param {{accounts?: Set<string> | string[]} | null | undefined} row
 * @returns {string | null}
 */
export function leadAccount(row) {
  if (!row) return null;
  const accts = row.accounts;
  if (!accts) return null;
  if (accts instanceof Set) {
    const it = accts.values().next();
    return it.done ? null : String(it.value || '');
  }
  if (Array.isArray(accts) && accts.length > 0) return String(accts[0]);
  return null;
}
