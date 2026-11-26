/**
 * realizedPnlHistory.js — pure helpers for the "Realized P&L by Expiry"
 * section on /admin/history (new 4th tab, alongside Orders/Trades/Funds).
 *
 * Backend: GET /api/admin/history/realized-pnl, sourced from the new
 * kite_console_pnl_sync table (Kite Console P&L-statement export — see
 * backend/api/models.py:KiteConsolePnlSync and
 * scripts/kite_console_pnl_sync.py). Built against the AGREED contract
 * while the backend route lands in a parallel pass — see the module
 * header comment in +page.svelte for the exact field list and the
 * reconciliation note if the real route ships differently.
 *
 * Kept as a plain .js file with NO import of api.js or any .svelte.js
 * rune file — this project's Vitest setup has no Svelte compiler, so a
 * transitive rune import breaks every test that imports this module
 * (see CLAUDE.md "Frontend SWR extension" invariant for the same rule
 * applied elsewhere).
 *
 * Five quality dimensions (standing project convention):
 *  1. SSOT   — one param-builder and one row-normalizer, reused by both
 *              the real fetch call and this file's own tests.
 *  2. Perf   — pure synchronous functions, no I/O.
 *  3. Stale  — buildRealizedPnlParams drops undefined/null/'' keys the
 *              same way api.js's own _toQs does, so an unset filter can
 *              never silently become the literal string "undefined".
 *  4. Reuse  — normalizeRealizedPnlRows accepts either a bare array or
 *              an {rows:[...]} envelope, covering the single most likely
 *              shape drift between the agreed contract and what ships.
 *  5. UX     — sumRealizedPnl skips null/NaN rows rather than poisoning
 *              the whole total, matching the "missing vs zero" convention
 *              documented in CLAUDE.md's Critical math guards.
 */

/**
 * @typedef {{
 *   account: string, segment: string,
 *   instrument_type: 'equity'|'future'|'option',
 *   symbol_root: string, expiry: string|null, realized_pnl: number,
 * }} RealizedPnlRow
 */

/**
 * Build the query-param object for GET /admin/history/realized-pnl from
 * the page's own filter state. Every param is optional on the backend;
 * an empty/undefined/null value is simply omitted (never sent as the
 * literal string "undefined" or an empty string).
 *
 * @param {{
 *   account?: string, segment?: string, instrumentType?: string,
 *   symbolRoot?: string, expiryFrom?: string, expiryTo?: string,
 * }} filters
 * @returns {Record<string, string>}
 */
export function buildRealizedPnlParams(filters = {}) {
  const {
    account, segment, instrumentType, symbolRoot, expiryFrom, expiryTo,
  } = filters;
  /** @type {Record<string, string>} */
  const out = {};
  if (account     && String(account).trim())     out.account         = String(account).trim();
  if (segment     && String(segment).trim())     out.segment         = String(segment).trim();
  if (instrumentType && String(instrumentType).trim()) out.instrument_type = String(instrumentType).trim();
  if (symbolRoot  && String(symbolRoot).trim())  out.symbol_root     = String(symbolRoot).trim().toUpperCase();
  if (expiryFrom  && String(expiryFrom).trim())  out.expiry_from     = String(expiryFrom).trim();
  if (expiryTo    && String(expiryTo).trim())    out.expiry_to       = String(expiryTo).trim();
  return out;
}

/**
 * Normalize the realized-pnl endpoint's response into a flat row array.
 * Accepts either a bare array (what the agreed contract specifies) or an
 * {rows:[...]} envelope (the shape every OTHER /admin/history/* endpoint
 * on this page actually uses) — covers the single most likely drift
 * between the agreed contract and whatever the backend agent ships.
 * Never throws on a malformed/absent response — returns [].
 *
 * @param {unknown} response
 * @returns {RealizedPnlRow[]}
 */
export function normalizeRealizedPnlRows(response) {
  if (Array.isArray(response)) return response;
  if (response && typeof response === 'object' && Array.isArray(/** @type {any} */ (response).rows)) {
    return /** @type {any} */ (response).rows;
  }
  return [];
}

/**
 * Sum realized_pnl across a row set, skipping null/undefined/NaN values
 * rather than letting one bad row poison the whole total (missing vs
 * zero — see CLAUDE.md Critical math guards). Returns 0 for an empty or
 * all-missing set — a genuine zero total, not "unknown", since this is a
 * sum over rows that WERE returned (as opposed to e.g. fundsAggregate.js's
 * null-on-no-poll-yet case, which has no analogue here: an empty filtered
 * result set really does sum to zero).
 *
 * @param {Array<{realized_pnl?: unknown}>|null|undefined} rows
 * @returns {number}
 */
export function sumRealizedPnl(rows) {
  if (!Array.isArray(rows) || rows.length === 0) return 0;
  let total = 0;
  for (const r of rows) {
    const v = Number(r?.realized_pnl);
    if (Number.isFinite(v)) total += v;
  }
  return total;
}
