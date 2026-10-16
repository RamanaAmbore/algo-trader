/**
 * pinPickerCandidates.js — pure helper for MarketPulse.svelte's Positions
 * bucket: derives the candidate list PositionPinPicker.svelte shows when
 * the operator clicks the `.pos-state-badge-btn` orphan badge (or picks
 * "Pair with…" from a context menu) on a row.
 *
 * Input is a flat array of RAW per-account position rows — the shape
 * `positionsStore`/`pulsePositionsStore` parse from `/api/positions`
 * (`account`, `symbol`/`tradingsymbol`, `quantity`, `pin_group_key`, …) —
 * NOT MarketPulse's own per-symbol-aggregated `unifiedRows` (those merge
 * every account holding a symbol into one row, so a single row can no
 * longer answer "what else is open in THIS account").
 *
 * Mirrors the shape of the derivatives page's own inline
 * `_pinPickerCandidates` $derived (frontend/src/routes/(algo)/admin/
 * derivatives/+page.svelte), but extracted here for Vitest coverage and
 * hardened against a defect that $derived never hit in practice: Kite can
 * report the SAME tradingsymbol twice under one account across product
 * types (NRML + MIS). PositionPinPicker's `{#each candidates as cand
 * (cand.symbol + '|' + cand.account)}` keys on that pair — an
 * un-deduplicated duplicate throws a Svelte "duplicate key" runtime error
 * at render time, not merely a cosmetic double row. (The derivatives
 * page's own copy shares this exposure and was not touched — out of
 * scope for this change.)
 *
 * @param {Array<any>|null|undefined} rows
 * @param {string} account
 * @param {string} sourceSymbol
 * @returns {Array<{symbol: string, account: string, qty: number}>}
 */
export function pinPickerCandidatesFromRows(rows, account, sourceSymbol) {
  const acct = String(account || '').toUpperCase();
  const sym  = String(sourceSymbol || '').toUpperCase();
  if (!acct || !Array.isArray(rows)) return [];

  const seen = new Set();
  const out = [];
  for (const p of rows) {
    const pAcct = String(p?.account || '').toUpperCase();
    if (pAcct !== acct) continue;
    const pSym = String(p?.tradingsymbol || p?.symbol || '').toUpperCase();
    if (!pSym || pSym === sym) continue;
    const qty = Number(p?.quantity ?? p?.qty ?? 0);
    if (qty === 0) continue;
    if (seen.has(pSym)) continue;
    seen.add(pSym);
    out.push({ symbol: pSym, account: p.account, qty });
  }
  return out;
}
