import { decomposeSymbol } from './decomposeSymbol.js';
import { baseDayPnlForPosition, currentTotalProfit } from './nav.js';

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
    // IS the realised P&L; nothing left to mark at spot.
    //
    // Trust `realised` only when it's non-zero — mirrors nav.js's
    // currentTotalProfit()/the backend's resolve_realised_unrealised
    // both-zero-fields-fall-back-to-`pnl` convention exactly. Kite is
    // documented (PULSE_SPEC.md) to ship `realised: 0` ALONGSIDE a real
    // non-zero `pnl` on settlement/full-close — treating a present-but-zero
    // `realised` as authoritative (the old behavior) silently dropped that
    // pnl and under-reported the closed leg's Exp P&L (confirmed root cause
    // of the NavStrip-vs-Snapshot divergence audit, worked example: overnight
    // short 150 NIFTY CE bought back 75 today, realised=0/pnl=3750 → old
    // code returned 0 instead of 3750).
    if (realised) return realised;
    const pnlField = c?.pnl;
    if (pnlField != null && isFinite(Number(pnlField))) return Number(pnlField);
    // Neither field usable for a real value — 0 is a legitimate answer when
    // `realised` was explicitly supplied (even as 0); null only when the row
    // carries NEITHER field at all (genuinely unusable, not "flat zero").
    return (realisedField != null && isFinite(Number(realisedField))) ? realised : null;
  }
  const ev = expiryPnl(c, spot, legAnalyticsBySymbol);
  if (ev == null) return null;
  return AVG_PRICE_IS_COST_BASIS ? ev + realised : ev;
}

// ─────────────────────────────────────────────────────────────────────────────
// splitClosedReopened + buildPositionRowFromBroker — moved here from
// derivatives/pageLoad.js (2026-09 SSOT fix) so portfolioStore.svelte.js can
// import the SAME precise partial/full-close realised-P&L derivation the
// derivatives Snapshot grid already used, without a derivatives-page
// dependency. pageLoad.js re-exports both names unchanged so existing call
// sites (+page.svelte, pageLoad.test.js) are unaffected. Logic below is
// UNCHANGED from the original — only the file it lives in moved.
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Map a raw broker position row into the internal position shape.
 * Does NOT split closed-reopened rows — callers pass the result through
 * splitClosedReopened if needed.
 *
 * @param {any} p  - raw broker position object
 * @param {'live'|'sim'} source
 * @returns {object}
 */
export function buildPositionRowFromBroker(p, source) {
  const sym = String(p?.tradingsymbol || p?.symbol || '').toUpperCase();
  return {
    symbol:   sym,
    account:  String(p?.account || ''),
    qty:      Number(p?.quantity || 0),
    // lots / lot_size — new backend fields (quantity is now always contracts).
    // lots = integer lot count for display; lot_size = contracts per lot.
    // Preserved as-is (null when absent) so lotsForRow can use the fast path.
    lots:     p?.lots     != null ? Number(p.lots)     : null,
    lot_size: p?.lot_size != null ? Number(p.lot_size) : null,
    source,
    avg_cost: p?.average_price != null ? Number(p.average_price) : null,
    ltp:      p?.last_price    != null ? Number(p.last_price)    : null,
    prev_close: Number(p?.prev_close) || null,
    // Poll-time underlying spot, stamped by the backend's option-Greeks
    // enrichment pass (_enrich_position_greeks, positions.py) on BOTH the
    // live-fetch and closed-hours-snapshot paths — 0.0 default when unset
    // (qty=0 rows, or futures, which the enrichment loop skips). Required
    // by the extrinsic formula's poll-consistent anchor (legExtrinsicDisplay
    // above) so the exp-P&L term and the MTM term share the same point in
    // time, not a live tick vs a stale poll.
    underlying_ltp: p?.underlying_ltp != null ? Number(p.underlying_ltp) : 0,
    pnl:      p?.pnl != null ? Number(p.pnl) : 0,
    realised: p?.realised != null ? Number(p.realised) : 0,
    // unrealised is left undefined (not defaulted to 0) when the backend
    // doesn't ship it — currentTotalProfit()/baseDayPnlForPosition() in
    // nav.js require BOTH realised and unrealised to be present+finite
    // before preferring realised+unrealised over the pnl fallback; a
    // premature 0 default here would silently switch that formula on.
    unrealised: p?.unrealised != null ? Number(p.unrealised) : undefined,
    day_change_val: p?.day_change_val != null ? Number(p.day_change_val) : 0,
    day_pnl: p?.day_pnl != null ? Number(p.day_pnl) : null,
    chg_pct: p?.day_change_percentage != null ? Number(p.day_change_percentage) : null,
    overnight_quantity: Number(p?.overnight_quantity || 0),
    day_buy_quantity:   Number(p?.day_buy_quantity || 0),
    day_sell_quantity:  Number(p?.day_sell_quantity || 0),
    day_buy_value:      Number(p?.day_buy_value || 0),
    day_sell_value:     Number(p?.day_sell_value || 0),
    prev_settlement_pnl: p?.prev_settlement_pnl != null ? Number(p.prev_settlement_pnl) : null,
  };
}

/**
 * Split a broker-consolidated position into separate display rows
 * when it had intraday close/reopen activity.
 *
 * Trigger (Variant 1, with partial-reduction): `overnight ≠ 0` AND
 * (`day_buy > 0` OR `day_sell > 0`).
 *
 * The split produces:
 *   - Closed row  — qty = 0, P&L = realised on the closed portion.
 *   - Open row    — qty = current_qty, P&L = unrealised on what remains.
 *
 * Sum of the two rows' Day P&L equals the original total day change.
 *
 * @param {any} p  - normalised position row (buildPositionRowFromBroker output)
 * @returns {any[]}
 */
/**
 * Compute the weighted average exit price for the closed portion.
 * Long positions exit via sells; short positions exit via buys.
 * @param {number} oq   overnight_quantity
 * @param {number} dbq  day_buy_quantity
 * @param {number} dsq  day_sell_quantity
 * @param {number} dbv  day_buy_value
 * @param {number} dsv  day_sell_value
 * @returns {number}
 */
function _exitPrice(oq, dbq, dsq, dbv, dsv) {
  if (oq > 0) return dsq > 0 ? dsv / dsq : 0;
  return dbq > 0 ? dbv / dbq : 0;
}

/**
 * Day P&L on the closed portion.
 * Long: (exit − prev_close) × closedQty. Short: (prev_close − exit) × closedQty.
 * @param {number} oq          overnight_quantity
 * @param {number} exitPrice
 * @param {number} close       prev_close
 * @param {number} closedQty
 * @returns {number}
 */
function _closedDayPnl(oq, exitPrice, close, closedQty) {
  return oq > 0
    ? (exitPrice - close) * closedQty
    : (close - exitPrice) * closedQty;
}

/**
 * Lifetime P&L attributable to the closed portion.
 * When broker already closed the whole position (brokerQty=0) use p.pnl;
 * otherwise compute from cost basis. Arithmetic is identical to original.
 * @param {number} brokerQty   Math.abs(p.qty)
 * @param {number} pnl         p.pnl
 * @param {number} oq          overnight_quantity
 * @param {number} exitPrice
 * @param {number} avgCost     p.avg_cost
 * @param {number} closedQty
 * @returns {number}
 */
function _closedLifetimePnl(brokerQty, pnl, oq, exitPrice, avgCost, closedQty) {
  if (brokerQty === 0) return pnl;
  return oq > 0
    ? (exitPrice - avgCost) * closedQty
    : (avgCost - exitPrice) * closedQty;
}

/**
 * Force `row.prev_settlement_pnl` so that `baseDayPnlForPosition(row) ===
 * targetDayPnl` exactly, regardless of whether `currentTotalProfit(row)`
 * resolves via `realised+unrealised` or the `pnl` fallback. Used for the
 * "open" half of a closed/reopened split, whose Day P&L is computed
 * independently (`open_dcv` / `baseDayPnlForPosition(p) - closed_day_pnl`)
 * and must not silently diverge depending on which total-profit field pair
 * the backend has populated on the pre-split row.
 * @param {any} row
 * @param {number} targetDayPnl
 * @returns {any} the same row, mutated
 */
function _forceBaseline(row, targetDayPnl) {
  row.prev_settlement_pnl = currentTotalProfit(row) - targetDayPnl;
  return row;
}

/**
 * Entry/exit price + direction for an intraday round-trip (overnight_quantity
 * === 0, both day_buy_quantity and day_sell_quantity > 0 — the position was
 * opened AND partially/fully closed within today's session). Also the path
 * for every Groww row, which hardcodes overnight_quantity=0 regardless of
 * whether the position is actually overnight.
 * @param {number} dbq
 * @param {number} dsq
 * @param {number} dbv
 * @param {number} dsv
 * @returns {{ entry: number, exit: number, closedQty: number, dir: 1|-1 }|null}
 */
function _intradayEntryExit(dbq, dsq, dbv, dsv) {
  const closedQty = Math.min(dbq, dsq);
  if (closedQty <= 0) return null;
  const buyPrice  = dbq > 0 ? dbv / dbq : 0;
  const sellPrice = dsq > 0 ? dsv / dsq : 0;
  // dir=1: net addition was long (bought more than sold) — opened via buys,
  // closed portion exited via sells. dir=-1: net addition was short.
  const dir = /** @type {1|-1} */ (dbq >= dsq ? 1 : -1);
  return {
    entry: dir === 1 ? buyPrice  : sellPrice,
    exit:  dir === 1 ? sellPrice : buyPrice,
    closedQty,
    dir,
  };
}

export function splitClosedReopened(p) {
  const oq  = Number(p.overnight_quantity || 0);
  const dbq = Number(p.day_buy_quantity   || 0);
  const dsq = Number(p.day_sell_quantity  || 0);
  const dbv = Number(p.day_buy_value      || 0);
  const dsv = Number(p.day_sell_value     || 0);
  const close = Number(p.prev_close ?? 0);

  if (dbq === 0 && dsq === 0) return [p];

  if (oq === 0) {
    // Intraday round-trip with no overnight carry — opened and (partially
    // or fully) closed today. Covers intraday partial closes AND every
    // Groww row (overnight_quantity hardcoded to 0 by that broker). No
    // prior-session close exists, so the closed portion's day P&L IS its
    // lifetime P&L (mirrors the "new position" Case-1 convention).
    const trip = _intradayEntryExit(dbq, dsq, dbv, dsv);
    if (!trip) return [p];
    const { entry, exit, closedQty, dir } = trip;
    const closed_lifetime_pnl = dir === 1
      ? (exit - entry) * closedQty
      : (entry - exit) * closedQty;

    const brokerQty = Math.abs(Number(p.qty || 0));
    if (brokerQty === 0) {
      // Fully closed today. Do NOT size the closed row on closedQty
      // (min(dbq,dsq)) alone — Groww hardcodes overnight_quantity=0 even
      // when a position genuinely carried overnight, so a position that
      // closed BOTH a hidden overnight portion AND an intraday round-trip
      // would have the overnight portion's P&L silently dropped if we only
      // counted the round-trip-sized closedQty (min(dbq,dsq) undersizes the
      // true realized amount whenever dbq !== dsq). `currentTotalProfit(p)`
      // (realised+unrealised when both present, else the pnl fallback — the
      // same SSOT baseDayPnlForPosition itself builds on) is the
      // authoritative total realized P&L for the whole (now-flat) position
      // — safe to use directly regardless of Groww's oq mislabeling, and
      // correct even when Groww ships realised+unrealised without a
      // top-level `pnl` field. `baseDayPnlForPosition(p)` on the ORIGINAL
      // unsplit row likewise already resolves Day P&L correctly against
      // whatever prev_settlement_pnl the backend supplied for this
      // (account,symbol) — the backend baseline join is keyed by
      // account+symbol, not by Groww's (untrustworthy) overnight_quantity
      // flag — so it correctly captures any real overnight carry that oq=0
      // hides.
      const wholeLifetimePnl = currentTotalProfit(p);
      const wholeDayPnl = baseDayPnlForPosition(p);
      return [_forceBaseline({
        ...p,
        qty: 0,
        pnl: wholeLifetimePnl,
        realised: wholeLifetimePnl,
        unrealised: 0,
        day_change_val: wholeDayPnl,
        _splitTag: 'closed',
      }, wholeDayPnl)];
    }
    const closedRow = {
      ...p,
      qty: 0,
      pnl: closed_lifetime_pnl,
      // realised/unrealised forced consistent with pnl on the closed row so
      // currentTotalProfit()/baseDayPnlForPosition() agree regardless of
      // which field pair the backend has populated (realised+unrealised vs
      // pnl-only) — otherwise the whole-position realised/unrealised
      // inherited via the spread above (attributable to the FULL position,
      // not just today's closed portion) would double-count now that the
      // backend reliably populates `unrealised` on every row.
      realised: closed_lifetime_pnl,
      unrealised: 0,
      // No prior-session baseline for the closed portion — it was opened
      // AND closed today, so Day P&L IS its lifetime P&L (base=0).
      prev_settlement_pnl: 0,
      day_change_val: closed_lifetime_pnl,
      _splitTag: 'closed',
    };

    const open_dcv_intraday = baseDayPnlForPosition(p) - closed_lifetime_pnl;
    const openRow = _forceBaseline({
      ...p,
      pnl: Number(p.pnl || 0) - closed_lifetime_pnl,
      realised: 0,
      day_change_val: open_dcv_intraday,
      _splitTag: 'open',
    }, open_dcv_intraday);
    return [closedRow, openRow];
  }

  const closed_qty = oq > 0 ? Math.min(oq, dsq) : Math.min(-oq, dbq);
  if (closed_qty <= 0) return [p];

  const exit_price       = _exitPrice(oq, dbq, dsq, dbv, dsv);
  const closed_day_pnl   = _closedDayPnl(oq, exit_price, close, closed_qty);

  const brokerQty        = Math.abs(Number(p.qty || 0));
  const avg_cost         = Number(p.avg_cost || 0);
  const closed_lifetime_pnl = _closedLifetimePnl(
    brokerQty, Number(p.pnl || 0), oq, exit_price, avg_cost, closed_qty
  );

  const open_dcv = baseDayPnlForPosition(p) - closed_day_pnl;

  // Forcing realised/unrealised consistent with pnl (closed portion has no
  // remaining unrealised — qty=0) and forcing prev_settlement_pnl via
  // _forceBaseline means baseDayPnlForPosition(closedRow) = closed_day_pnl
  // regardless of which total-profit field pair the backend has populated.
  const closedRow = _forceBaseline({
    ...p,
    qty: 0,
    pnl: closed_lifetime_pnl,
    realised: closed_lifetime_pnl,
    unrealised: 0,
    day_change_val: closed_day_pnl,
    _splitTag: 'closed',
  }, closed_day_pnl);

  if (brokerQty === 0) return [closedRow];

  const openRow = _forceBaseline({
    ...p,
    pnl: Number(p.pnl || 0) - closed_lifetime_pnl,
    realised: 0,
    day_change_val: open_dcv,
    _splitTag: 'open',
  }, open_dcv);
  return [closedRow, openRow];
}

/**
 * Store-side SSOT wrapper: derive one raw broker position row's Exp P&L
 * using the SAME split-aware realised derivation the derivatives Snapshot
 * grid uses (splitClosedReopened → per-piece expiryPnlWithRealised, summed).
 *
 * This is the fix for the NavStrip-vs-Snapshot Exp P&L divergence: NavStrip
 * (portfolioStore.svelte.js) used to pass the raw, unsplit position straight
 * into expiryPnlWithRealised, trusting the broker's raw `realised` field —
 * which Kite documents as unreliable on same-day partial/full closes
 * (`realised: 0` alongside a real settlement `pnl`). Snapshot instead ran
 * every position through splitClosedReopened first, producing a precise
 * closed/open realised split. This function makes that same derivation
 * available to portfolioStore, computed once, so both surfaces agree.
 *
 * @param {any} rawRow  - raw broker position row (portfolioStore's `p`,
 *   still carrying its ORIGINAL Kite field names — quantity/average_price/
 *   overnight_quantity/day_buy_quantity/... — normalised internally via
 *   buildPositionRowFromBroker).
 * @param {'opt'|'fut'} kind
 * @param {number|null|undefined} anchor  underlying spot (options) or the
 *   contract's own price (futures) — see resolveExpiryAnchor. Only required
 *   when at least one split piece still carries a non-zero qty; ignored
 *   entirely for a row that split into an all-closed (qty=0) piece.
 * @returns {number|null}
 */
export function positionExpPnl(rawRow, kind, anchor) {
  if (kind !== 'opt' && kind !== 'fut') return null;
  const normRow = buildPositionRowFromBroker(rawRow, 'live');
  normRow.kind = kind;
  const pieces = splitClosedReopened(normRow);
  const needsAnchor = pieces.some(pc => Number(pc?.qty || 0) !== 0);
  if (needsAnchor && !(Number(anchor) > 0)) return null;
  let sum = null;
  for (const piece of pieces) {
    const v = expiryPnlWithRealised(piece, anchor);
    if (v != null && isFinite(Number(v))) sum = (sum ?? 0) + Number(v);
  }
  return sum;
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
