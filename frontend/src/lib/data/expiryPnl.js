import { decomposeSymbol } from './decomposeSymbol.js';

// Structural tail regexes mirroring decomposeSymbol's own _OPT_MONTHLY /
// _OPT_WEEKLY shapes (YY+MON+strike+CE/PE, or YY+month-code+DD+strike+
// CE/PE) but deliberately NOT anchored to a pure-letters root (unlike
// decomposeSymbol's strict regexes) — only anchored at the CE/PE suffix —
// so they still recognise real Kite symbols whose root contains digits or
// punctuation (e.g. a monthly on M&M / BAJAJ-AUTO) that decomposeSymbol's
// strict root pattern rejects.
//
// Monthly is UNAMBIGUOUS regardless of root shape: the 3-letter month code
// always separates the year from the strike, so the strike capture group
// here is safe to use directly even when decomposeSymbol itself failed.
const _OPT_MONTHLY_TAIL = /\d{2}[A-Z]{3}(\d+(?:\.\d+)?)(CE|PE)$/i;
// Weekly has NO letter separator (year+month-code+day+strike are all
// digits run together) — genuinely ambiguous on a digit-bearing root
// (see _isValidParsedDay below), so this is used only to DETECT the shape,
// never to extract a strike from it directly.
const _OPT_WEEKLY_TAIL  = /\d{2}[1-9OND]\d{2}(\d+(?:\.\d+)?)(CE|PE)$/i;

/**
 * decomposeSymbol's weekly regex has no way to know where a digit-bearing
 * root (e.g. NIFTYNXT50) ends, so on such roots it still "succeeds"
 * structurally but silently mis-splits the digit run — e.g.
 * NIFTYNXT5025624400CE parses as root="NIFTYNXT", yy="50", monCode="2",
 * dd="56" (an impossible day-of-month), strike="24400", instead of the
 * true root="NIFTYNXT50", yy="25", monCode="6", dd="24", strike="400".
 * A day outside 1-31 is a reliable tripwire for this misparse — reject
 * the primary decomposeSymbol result so the caller falls through to the
 * ambiguity check below instead of trusting the garbage split.
 * @param {{ kind: string, month: string|null }} d
 * @returns {boolean}
 */
function _isValidParsedDay(d) {
  if (d?.kind !== 'opt') return true;
  const isMonthly = /^\d{2}[A-Z]{3}$/.test(d?.month || '');
  if (isMonthly) return true; // monthly form has no day component
  const dd = Number(String(d?.month || '').slice(-2));
  return Number.isFinite(dd) && dd >= 1 && dd <= 31;
}

/**
 * Shared expiry-day P&L helper — single source of truth used by the
 * derivatives-page Snapshot Exp P&L column, the payoff overlay legs
 * TOTAL row, AND the NavStrip P pill slot 3.
 *
 * Operator 2026-07-01: "use the same number to update p 3 values in
 * navstrip." Formerly each surface had its own inline math; small
 * divergences (strike-parse regex, sign convention on shorts) drifted
 * the numbers between the three views. Consolidating the compute keeps
 * them locked.
 *
 * Contract:
 *   - Options: `(intrinsic − avg_cost) × qty` where intrinsic is the
 *     option's payoff at spot (max(0, spot − strike) for CE, mirror for PE).
 *     qty preserves its signed sense from Kite (positive long / negative short)
 *     so writing a call yields negative-qty × negative-intrinsic-delta
 *     surfaces the credit correctly.
 *   - Futures + equity: `(spot − avg_cost) × qty` — no time value to strip,
 *     P&L tracks spot 1:1.
 *
 * Returns null when the input is unusable (spot missing / non-positive,
 * qty=0, strike/opt_type unparseable for options). The caller decides how
 * to render "—" for null rows. avg_cost=0 is valid (Kite returns 0 for
 * fresh intraday fills) — full intrinsic value is profit when cost=0.
 *
 * Strike/opt_type parsing: prefers backend leg analytics when supplied,
 * else falls back to `decomposeSymbol` (the shared weekly/monthly Kite
 * tradingsymbol parser — NOT an inline regex, which misparsed weekly
 * symbols like NIFTY2592324000CE by greedily capturing the whole numeric
 * run as the strike).
 *
 * @param {{ symbol: string, qty?: number|string, quantity?: number|string, avg_cost?: number|string, average_price?: number|string, kind: 'opt'|'fut'|'eq'|string }} c
 * @param {number|null|undefined} spot   underlying spot for intrinsic calculation
 * @param {Record<string, {strike?: number, opt_type?: string}>} [legAnalyticsBySymbol]
 *   optional map of symbol → backend leg analytics (strike + opt_type
 *   from strategy-analytics response) — preferred over decomposeSymbol
 *   when available.
 * @returns {number|null}
 */
export function expiryPnl(c, spot, legAnalyticsBySymbol = {}) {
  if (spot == null || !isFinite(Number(spot)) || Number(spot) <= 0) return null;
  const qty  = Number(c?.qty  ?? c?.quantity       ?? 0);
  const cost = Number(c?.avg_cost ?? c?.average_price ?? 0);
  if (!qty) return null;
  const S = Number(spot);
  if (c?.kind === 'opt') {
    const sym = String(c?.symbol || '');
    const lg = legAnalyticsBySymbol?.[sym];
    let K = lg?.strike ?? null;
    let opt = lg?.opt_type ?? null;
    if (K == null || !opt) {
      // Primary: decomposeSymbol — the shared weekly/monthly Kite
      // tradingsymbol parser (correctly handles NIFTY2592324000CE-style
      // weekly symbols, unlike the old inline regex which greedily
      // captured the whole numeric run as the strike). _isValidParsedDay
      // rejects weekly results with an impossible day-of-month, which
      // signals a digit-bearing-root misparse (see NIFTYNXT50 above).
      const d = decomposeSymbol(sym);
      if (d.kind === 'opt' && d.strike != null && d.optType && _isValidParsedDay(d)) {
        K = d.strike; opt = d.optType;
      } else {
        // decomposeSymbol couldn't parse this symbol against its strict
        // pure-letters-root regexes (or produced a semantically-invalid
        // weekly day). Three remaining cases, tried in order:
        const monthlyTail = _OPT_MONTHLY_TAIL.exec(sym);
        const weeklyTail  = _OPT_WEEKLY_TAIL.exec(sym);
        if (monthlyTail) {
          // 1. Monthly-shaped tail (YY+3-letter-month+strike+CE/PE) — the
          //    3-letter month unambiguously separates year from strike
          //    regardless of what precedes it, so the root can contain
          //    digits/punctuation (M&M, BAJAJ-AUTO) and this is still
          //    safe to use directly.
          K = Number(monthlyTail[1]); opt = monthlyTail[2].toUpperCase();
        } else if (weeklyTail) {
          // 2. Weekly-shaped tail with no letter separator between the
          //    year/day-code and the strike — genuinely ambiguous on a
          //    digit-bearing root (can't tell where root digits end and
          //    year/day/strike digits begin). Do not guess: K/opt stay
          //    null and the function returns null below rather than risk
          //    merging the day-code and strike into one garbage number.
        } else {
          // 3. No Kite year/month encoding at all — a short synthetic /
          //    simplified symbol (unit-test fixtures, ad-hoc draft rows).
          //    Safe to use the original unconstrained bare-suffix regex.
          const m = /(\d+(?:\.\d+)?)(CE|PE)$/i.exec(sym);
          if (m) { K = Number(m[1]); opt = m[2].toUpperCase(); }
        }
      }
    }
    if (K == null || !opt) return null;
    const intrinsic = opt === 'CE' ? Math.max(0, S - K) : Math.max(0, K - S);
    return (intrinsic - cost) * qty;
  }
  // futures + equity: P&L tracks spot 1:1.
  return (S - cost) * qty;
}

/**
 * Whether `average_price` (after a partial close) is assumed to be
 * cost-basis (unchanged entry cost) rather than breakeven-folded
 * (Kite re-averaging the remaining qty against realised P&L on the
 * closed portion). Flagged decision (plan default): cost-basis = true.
 *
 * Centralised here as the SINGLE flip point — per the operator's
 * pending empirical live-account check (buy+partial-sell same symbol,
 * inspect Kite's reported `average_price` on the remainder), change
 * this one constant if the check finds breakeven-folded instead.
 */
export const AVG_PRICE_IS_COST_BASIS = true;

/**
 * Unified expiry-projected P&L for a position, including the realised
 * portion from any same-day partial/full close — the single shared
 * implementation for both the Pulse/NavStrip/Snapshot path
 * (`portfolioStore.svelte.js`) and the Legs/Expiry-tab path
 * (`derivatives/pageLoad.js`'s `splitClosedReopened`-based split).
 *
 * Cost-basis assumption (`AVG_PRICE_IS_COST_BASIS = true`, the default):
 * `average_price` already reflects the original entry cost, unaffected
 * by any same-day partial close, so the realised P&L on the closed
 * portion must be added on top of the unrealised expiry value computed
 * from the *remaining* qty at `avg_cost`:
 *   result = expiryPnl(remaining qty @ avg_cost, spot) + realised
 *
 * Breakeven-folded assumption (flip `AVG_PRICE_IS_COST_BASIS` to false):
 * Kite has already folded the realised gain/loss into `average_price` for
 * the remainder, so adding `realised` again would double-count:
 *   result = expiryPnl(remaining qty @ avg_cost, spot)
 *
 * `realised` is read directly from the backend-provided field (reliable
 * per-row on every broker, including Groww which hardcodes
 * `overnight_quantity=0` and MCX/intraday partial closes) rather than
 * being reconstructed from `overnight_quantity`/day-buy/day-sell legs —
 * this is what makes the two formerly-diverging implementations unify
 * without re-deriving realised P&L per call site.
 *
 * The `pnl` (lifetime P&L) fallback applies ONLY on the qty===0 (fully
 * closed) branch, where pnl legitimately equals the realised total. On an
 * open leg (qty!==0), falling back to lifetime `pnl` when `realised` is
 * absent would double-count against the unrealised component already
 * baked into `expiryPnl`'s intrinsic-value calculation — callers must NOT
 * pre-merge `pnl` into `realised` before calling this function.
 *
 * @param {{ symbol: string, qty?: number|string, quantity?: number|string, avg_cost?: number|string, average_price?: number|string, kind: 'opt'|'fut'|'eq'|string, realised?: number|string|null, pnl?: number|string|null }} c
 * @param {number|null|undefined} spot
 * @param {Record<string, {strike?: number, opt_type?: string}>} [legAnalyticsBySymbol]
 * @returns {number|null}
 */
export function expiryPnlWithRealised(c, spot, legAnalyticsBySymbol = {}) {
  const qty = Number(c?.qty ?? c?.quantity ?? 0);
  const realisedField = c?.realised;
  const realised = (realisedField != null && isFinite(Number(realisedField))) ? Number(realisedField) : 0;
  if (!qty) {
    // Fully closed today (no remaining qty) — the whole expiry-day value
    // IS the realised P&L; nothing left to mark at spot. Falls back to
    // lifetime pnl ONLY here (qty=0 makes pnl == realised by construction).
    // Null only when the row carries neither field (unusable, not "flat zero").
    if (realisedField != null && isFinite(Number(realisedField))) return realised;
    const pnlField = c?.pnl;
    return (pnlField != null && isFinite(Number(pnlField))) ? Number(pnlField) : null;
  }
  const ev = expiryPnl(c, spot, legAnalyticsBySymbol);
  if (ev == null) return null;
  return AVG_PRICE_IS_COST_BASIS ? ev + realised : ev;
}

/**
 * Anchor-price selection for Exp P&L / extrinsic valuation (§4 — futures
 * own-price valuation fix). Shared decision tree used by BOTH:
 *   - portfolioStore.svelte.js's _posTier2 (exp_pnl/extrinsic block)
 *   - derivatives/+page.svelte's _legExpPnlDisplay(c, spot)
 *
 * Options always value at the front-month root spot (the Exp P&L SSOT for
 * options — matches Snapshot / Legs TOTAL / the column tooltip). Futures
 * value at THEIR OWN contract's price — a future's Exp P&L only equals the
 * root's front-month spot when the held contract IS the front-month
 * future; a far-month future (e.g. CRUDEOIL in contango/backwardation)
 * must be valued on its own price, not the root's front-month resolution.
 *
 * Futures priority: own live tick > own polled LTP > root spot (last
 * resort — e.g. cold MCX cache with no tick and no polled LTP yet).
 *
 * @param {{ isOpt: boolean, rootSpot: number, ownLiveLtp?: number, ownPolledLtp?: number }} p
 * @returns {number}
 */
export function resolveExpiryAnchor({ isOpt, rootSpot, ownLiveLtp = 0, ownPolledLtp = 0 }) {
  if (isOpt) return rootSpot;
  if (ownLiveLtp > 0) return ownLiveLtp;
  if (ownPolledLtp > 0) return ownPolledLtp;
  return rootSpot;
}

/**
 * Per-leg Extrinsic value — `Exp P&L − MTM`, both terms evaluated on the
 * SAME poll-time snapshot so the subtraction isn't contaminated by a
 * tick-vs-poll skew (confirmed regression: ~₹2,000 phantom extrinsic on a
 * CRUDEOIL future from an unrelated spot tick landing between polls).
 * Single shared implementation used by BOTH:
 *   - portfolioStore.svelte.js's _posTier2 (aggregate + per-symbol/root)
 *   - derivatives/+page.svelte's _filteredExtrinsicByRoot / per-leg
 *     Extrinsic cell (Snapshot grid + Legs grid)
 * so a multi-account or multi-surface view can never double-count or
 * diverge on the same underlying data.
 *
 * §7 (operator-approved): extrinsic ("time value remaining") is an
 * options-only concept — futures and equity/proxy legs track spot 1:1
 * with no time-decay component, so `Exp P&L − MTM` is either
 * tautologically 0 (a future valued at its own price, by construction —
 * see resolveExpiryAnchor) or a meaningless root-spot-vs-own-price
 * artifact (equity/proxy hedges). Returns `null` (not-applicable, not
 * zero) for every non-option leg — same convention the EV column uses
 * for rows it can't compute, rendered as '—' by callers.
 *
 * `pollAnchor` MUST be the underlying's poll-time spot (backend's
 * `underlying_ltp` field, stamped by the same enrichment pass on both
 * the live and closed-hours-snapshot paths) — NOT a live/tick-driven
 * root spot, which is what `spot` means everywhere else in this file.
 * Passing a live spot here silently reintroduces the tick-vs-poll bug
 * this function was written to close.
 *
 * @param {{ symbol?: string, kind?: string, qty?: number|string, quantity?: number|string, avg_cost?: number|string, average_price?: number|string, ltp?: number|string|null }} c
 * @param {number|null|undefined} pollAnchor  underlying's poll-time spot (c's OWN poll-time ltp for the exp-P&L term's spot input)
 * @returns {number|null}
 */
export function legExtrinsicDisplay(c, pollAnchor) {
  if (c?.kind !== 'opt') return null;
  const qty = Number(c?.qty ?? c?.quantity ?? 0);
  if (!qty) return 0; // fully closed today — no time value remaining
  const ltp = Number(c?.ltp ?? 0);
  // Draft / provisional rows carry no real market price (ltp null/0) —
  // without this guard `expiryPnl(c, pollAnchor) - (0 - avg_cost)*qty`
  // would print a fabricated "extrinsic" number instead of '—'.
  if (!(ltp > 0)) return null;
  if (pollAnchor == null || !isFinite(Number(pollAnchor)) || Number(pollAnchor) <= 0) return null;
  const cost = Number(c?.avg_cost ?? c?.average_price ?? 0);
  const ev = expiryPnl({ symbol: String(c?.symbol || ''), qty, avg_cost: cost, kind: 'opt' }, pollAnchor);
  if (ev == null) return null;
  return ev - (ltp - cost) * qty;
}
