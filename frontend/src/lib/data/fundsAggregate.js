/**
 * fundsAggregate.js — pure cross-account funds aggregation helpers.
 *
 * Hoisted out of portfolioStore.svelte.js's `_marginAvail` / `_marginTotal`
 * / `_liveCashTotal` `$derived.by` bodies into plain (no Svelte runes)
 * functions so Vitest can import and exercise the REAL shipped logic
 * directly — portfolioStore.svelte.js itself can't be imported under
 * Vitest (top-level `$derived.by` calls require the svelte-compiler
 * plugin, which isn't registered in vitest.config.js; see that file's
 * existing `?raw` source-grep tests for the established workaround).
 *
 * Real-money fix (2026-09, NavStrip "₹0 margin for an extended period,
 * then jumps to correct value" incident): the PREVIOUS design froze the
 * ENTIRE cross-account total to a remembered scalar (`_lastMarginAvail`
 * et al.) the moment ANY single account went degraded (backend-tagged
 * `stale_accounts` substitution — common, e.g. one flaky Dhan/Groww
 * account). On a fresh page load / hard refresh, that remembered scalar
 * resets to 0 — if the first poll(s) after reset happened to land while
 * degraded (very plausible), the getter returned 0 and KEPT returning 0
 * on every subsequent degraded poll, because the scalar never got a
 * chance to update to a real value. Meanwhile the real, correct numbers
 * for every HEALTHY account (including the operator's actual capital)
 * were sitting right there in `fundRows`, held hostage by one flaky
 * account.
 *
 * Fixed design: SUM every row actually present in `fundRows` on every
 * read, including stale/degraded accounts — the backend already
 * substitutes each stale account's own last-known-good values before
 * any masking is applied (`_stale_substitute_frame` in
 * `backend/brokers/broker_apis.py`), so a stale account's row already
 * carries a genuine number whenever a prior successful fetch exists.
 * These helpers never exclude a stale account from the total and never
 * freeze the whole cross-account sum to a stale scalar — only individual
 * missing/malformed fields fall back to 0 (missing-vs-zero convention,
 * see CLAUDE.md's "Alert evaluation and latching" section: a genuinely
 * absent numeric field is `0` here by construction of `Number(x || 0)`,
 * which is the existing, unchanged convention for these three sums).
 *
 * `null` is returned ONLY when `fundRows` itself is null/empty — i.e. no
 * successful poll has EVER landed for this store. That is a genuinely
 * UNKNOWN state (never contact the broker yet), distinct from a
 * confirmed real 0 (e.g. an account with literally no margin). Callers
 * (PositionStrip's M/C pills) must render this as `—`, not `₹0` — see
 * `fmtMoney`'s explicit null guard in PositionStrip.svelte.
 */

/** @param {any} f */
function _isTotalRow(f) {
  return String(f?.account || '').toUpperCase() === 'TOTAL';
}

/**
 * Σ avail_margin across every non-TOTAL row in `fundRows`.
 * @param {any[] | null | undefined} fundRows
 * @returns {number | null} null when fundRows is null/empty (no poll yet)
 */
export function sumMarginAvail(fundRows) {
  if (!fundRows?.length) return null;
  let s = 0;
  for (const f of fundRows) {
    if (_isTotalRow(f)) continue;
    s += Number(f?.avail_margin || 0);
  }
  return s;
}

/**
 * Σ (used_margin + avail_margin) across every non-TOTAL row — full
 * margin capacity (deployable + already-blocked).
 * @param {any[] | null | undefined} fundRows
 * @returns {number | null} null when fundRows is null/empty (no poll yet)
 */
export function sumMarginTotal(fundRows) {
  if (!fundRows?.length) return null;
  let s = 0;
  for (const f of fundRows) {
    if (_isTotalRow(f)) continue;
    s += Number(f?.used_margin || 0);
    s += Number(f?.avail_margin || 0);
  }
  return s;
}

/**
 * Σ live_cash (falling back to `cash` when live_cash is 0/unset) across
 * every non-TOTAL row. Mirrors Kite's `avail.cash` (direct funds only,
 * NOT `avail.live_balance` which also includes collateral).
 * @param {any[] | null | undefined} fundRows
 * @returns {number | null} null when fundRows is null/empty (no poll yet)
 */
export function sumLiveCashTotal(fundRows) {
  if (!fundRows?.length) return null;
  let s = 0;
  for (const f of fundRows) {
    if (_isTotalRow(f)) continue;
    const lc = Number(f?.live_cash ?? 0);
    s += lc !== 0 ? lc : Number(f?.cash || 0);
  }
  return s;
}
