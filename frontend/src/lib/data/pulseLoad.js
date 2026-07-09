// pulseLoad.js — Pure helper functions extracted from MarketPulse.svelte's
// loadPulse function.
//
// All functions here are side-effect-free and take explicit arguments —
// no Svelte reactive reads, no module-level state. The component passes
// explicit data (rows, stores, resolvers) and receives plain return values
// that it assigns back to reactive $state variables.
//
// Extracted seams (mirrors loadPulse body order):
//   1. parseDerivSymbol        — regex-only derivative symbol parser
//   2. resolveUnderlyingForOption — option → same-month future resolver
//   3. collectUnderlyings      — build underlyingInfos + contractKeys from rows
//   4. assembleQuoteKeys       — union all batchQuote key sources into a Set
//   5. buildQuoteMaps          — map batchQuote results to { underlyings, contracts }
//   6. planAccountSeeding      — compute two-stage account seeding state (pure)

import { resolveUnderlying, MCX_COMMODITIES } from './resolveUnderlying.js';

// ── 1. parseDerivSymbol ──────────────────────────────────────────────────────

const _MONTH_MAP = {
  JAN:'01', FEB:'02', MAR:'03', APR:'04', MAY:'05', JUN:'06',
  JUL:'07', AUG:'08', SEP:'09', OCT:'10', NOV:'11', DEC:'12',
};

/**
 * Parse a Kite derivative tradingsymbol into underlying / expiry / kind
 * without consulting the instruments cache. Handles both
 * monthly-options (NIFTY25APR22000CE), weekly-options
 * (NIFTY2540422000CE), monthly-futures (NIFTY25APRFUT), and commodity
 * variants (CRUDEOIL26JUNFUT, GOLDM26MAY152000PE).
 * Returns null for equity tradingsymbols.
 * Used as a fallback path for the anchor-creation loop when the
 * instruments cache hasn't loaded a contract yet (race on cold start).
 *
 * @param {string} sym
 * @returns {{ underlying: string, expiry: string, kind: string } | null}
 */
export function parseDerivSymbol(sym) {
  const s = String(sym || '').toUpperCase();
  // Monthly opt:  PREFIX + YYMMM + STRIKE + CE|PE
  let m = s.match(/^([A-Z]+)(\d{2})([A-Z]{3})\d+(CE|PE)$/);
  if (m) return { underlying: m[1], expiry: `20${m[2]}-${_MONTH_MAP[m[3]] || '01'}-01`, kind: m[4] };
  // Weekly opt: PREFIX + YY + MM + DD + STRIKE + CE|PE
  m = s.match(/^([A-Z]+)(\d{2})(\d{1,2})(\d{1,2})\d+(CE|PE)$/);
  if (m) return { underlying: m[1], expiry: `20${m[2]}-${String(m[3]).padStart(2,'0')}-${String(m[4]).padStart(2,'0')}`, kind: m[5] };
  // Monthly fut:  PREFIX + YYMMM + FUT
  m = s.match(/^([A-Z]+)(\d{2})([A-Z]{3})FUT$/);
  if (m) return { underlying: m[1], expiry: `20${m[2]}-${_MONTH_MAP[m[3]] || '01'}-01`, kind: 'FUT' };
  return null;
}

// ── 2. resolveUnderlyingForOption ────────────────────────────────────────────

/**
 * Resolve the underlying for an OPTION position.
 * For MCX commodities the option's underlying is the same-month future
 * (CRUDEOIL26JUN10500CE settles to CRUDEOIL26JUNFUT, not the front-month).
 * For indices/stocks the spot is shared across all expiries so we delegate
 * to resolveUnderlying.
 *
 * @param {string} name
 * @param {string | null} optionExpiryISO
 * @param {((u: string) => any) | null} findNearestFut
 * @param {((u: string) => any[]) | null} listFuts
 * @returns {object | null}
 */
export function resolveUnderlyingForOption(name, optionExpiryISO, findNearestFut, listFuts) {
  const n = String(name || '').toUpperCase();
  if (!n) return null;
  if (MCX_COMMODITIES.has(n) && optionExpiryISO && listFuts) {
    const ym = String(optionExpiryISO).slice(0, 7);  // YYYY-MM
    const futs = listFuts(n) || [];
    const same = futs.find(f => String(f.x || '').slice(0, 7) === ym);
    if (same?.s && same?.e) {
      return {
        tradingsymbol: same.s,
        exchange: same.e,
        quoteKey: `${same.e}:${same.s}`,
        // underlying_group keeps the month suffix so multiple option months
        // for the same commodity each map to their own future.
        underlying_group: `${n}_${ym}`,
        // displayUnderlying is the BARE commodity name so the anchor groups
        // with its option positions (which carry underlying='CRUDEOIL').
        displayUnderlying: n,
        kind: 'fut',
      };
    }
  }
  // Non-MCX or no same-month match → fall back to nearest-month.
  return resolveUnderlying(n, findNearestFut);
}

// ── 3. collectUnderlyings ────────────────────────────────────────────────────

/**
 * Walk positions, holdings, and activeLists to collect:
 *   - underlyingInfos: Map<underlying_group, info & { _major }>
 *   - contractKeys: Set<"EXCH:SYM"> for all known contracts
 *
 * Derivative-grouping rule: anchors are injected ONLY for options (CE/PE).
 * Futures stand alone — each future is its own row; no underlying-anchor is
 * pulled in alongside (the future IS the tradable instrument).
 *
 * The anchor row carries the major group of its trigger (positions vs
 * holdings) so buildUnified can land the anchor in the same major.
 * First-trigger-wins: positions > holdings > watchlist priority.
 *
 * @param {{
 *   pRows: any[],
 *   hRows: any[],
 *   activeLists: any[],
 *   findNearestFut: ((u: string) => any) | null,
 *   listFuts: ((u: string) => any[]) | null,
 * }} opts
 * @returns {{ underlyingInfos: Map<string, any>, contractKeys: Set<string> }}
 */
export function collectUnderlyings({ pRows, hRows, activeLists, findNearestFut, listFuts }) {
  /** @type {Map<string, any>} */
  const underlyingInfos = new Map();
  const contractKeys = new Set();

  /**
   * Parse sym and register an anchor row in underlyingInfos if applicable.
   * @param {string} sym
   * @param {string} triggerMajor
   */
  const addUnderlying = (sym, triggerMajor) => {
    const parsed = parseDerivSymbol(sym);
    if (!parsed) return;
    const { underlying: u, expiry, kind } = parsed;
    const isOpt = (kind === 'CE' || kind === 'PE');
    const isFut = (kind === 'FUT');
    if (!isOpt && !isFut) return;
    const info = isOpt
      ? resolveUnderlyingForOption(u, expiry, findNearestFut, listFuts)
      : resolveUnderlying(u, findNearestFut);
    if (info && !underlyingInfos.has(info.underlying_group)) {
      underlyingInfos.set(info.underlying_group, { ...info, _major: triggerMajor });
    }
  };

  for (const r of (pRows || [])) {
    const sym = String(r.symbol || r.tradingsymbol || '').toUpperCase();
    const exch = r.exchange || 'NFO';
    if (sym) {
      addUnderlying(sym, 'positions');
      contractKeys.add(`${exch}:${sym}`);
    }
  }
  for (const r of (hRows || [])) {
    const sym = String(r.symbol || r.tradingsymbol || '').toUpperCase();
    const exch = r.exchange || 'NSE';
    if (sym) {
      addUnderlying(sym, 'holdings');
      contractKeys.add(`${exch}:${sym}`);
    }
  }
  // Watchlist option-anchor pass — when the operator has an option (CE/PE)
  // in any active watchlist, synthesise an anchor row for that option's
  // underlying so the option sits under a parent anchor instead of orphaning.
  // First-trigger-wins: positions anchors already in the map are NOT overwritten.
  for (const list of (activeLists || [])) {
    const major = list?.is_pinned ? 'pinned' : 'watchlist';
    for (const it of (list?.items || [])) {
      const sym = String(it.tradingsymbol || '').toUpperCase();
      if (sym) addUnderlying(sym, major);
    }
  }

  return { underlyingInfos, contractKeys };
}

// ── 4. assembleQuoteKeys ─────────────────────────────────────────────────────

/**
 * Union all batchQuote key sources into one Set of "EXCH:SYM" strings.
 *
 * Sources (in order, Set semantics deduplicate):
 *   1. contractKeys  — positions + holdings from collectUnderlyings
 *   2. underlyingInfos quoteKeys — underlying futures/spots
 *   3. activeLists watchlist items — pinned indices and user watchlists
 *   4. movers — winners + losers (capped contribution; Set size stays bounded)
 *
 * @param {{
 *   contractKeys: Set<string>,
 *   underlyingInfos: Map<string, any>,
 *   activeLists: any[],
 *   movers: any[],
 * }} opts
 * @returns {Set<string>}
 */
export function assembleQuoteKeys({ contractKeys, underlyingInfos, activeLists, movers }) {
  const allKeys = new Set(contractKeys);

  for (const info of underlyingInfos.values()) allKeys.add(info.quoteKey);

  // Add every active watchlist item (pinned + user lists) to the batchQuote
  // universe. Without this, pinned indices (NIFTY 50, SENSEX) and
  // watchlist-only symbols are excluded from publishPulseQuotes and only get
  // their symbolStore entry refreshed via the loadQuotes path (30s when SSE
  // is up). Positions/holdings already land in contractKeys above, so the
  // incremental cost is just the ~15-20 pinned symbols.
  for (const list of (activeLists || [])) {
    for (const it of (list?.items || [])) {
      const wSym  = String(it.tradingsymbol || '').toUpperCase();
      const wExch = String(it.exchange || 'NSE').toUpperCase();
      if (wSym && wExch) allKeys.add(`${wExch}:${wSym}`);
    }
  }

  // Add mover rows (winners/losers) to the batchQuote universe.
  // Without this, mover-only symbols never enter symbolStore with
  // open/high/low/volume/oi — the MoverRow schema only carries
  // last_price/previous_close/change_pct.
  for (const m of (movers || [])) {
    const mSym  = String(m?.tradingsymbol || '').toUpperCase();
    const mExch = String(m?.exchange || 'NSE').toUpperCase();
    if (mSym && mExch) allKeys.add(`${mExch}:${mSym}`);
  }

  return allKeys;
}

// ── 5. buildQuoteMaps ────────────────────────────────────────────────────────

/**
 * Convert a batchQuote items array into the pulseQuotes shape:
 *   { underlyings: Record<underlying_group, quote & { _resolved }>,
 *     contracts:   Record<"EXCH:SYM", quote> }
 *
 * Always creates anchor entries for every underlying in underlyingInfos,
 * even when the broker quote endpoint didn't return a row. Quote-less anchors
 * render with an em-dash LTP — still better than a missing parent row.
 *
 * @param {any[]} items           — batchQuote result items
 * @param {Map<string, any>} underlyingInfos
 * @returns {{ underlyings: Record<string, any>, contracts: Record<string, any> }}
 */
export function buildQuoteMaps(items, underlyingInfos) {
  /** @type {Record<string, any>} */
  const cMap = {};
  for (const q of (items || [])) {
    cMap[`${q.exchange}:${q.tradingsymbol}`] = q;
  }
  /** @type {Record<string, any>} */
  const uMap = {};
  for (const [name, info] of underlyingInfos.entries()) {
    const q = cMap[info.quoteKey];
    // Always create the anchor — even when the broker quote endpoint didn't
    // return a row for info.quoteKey. Without this, INDIGO (whose NSE quote
    // can silently fail) and GOLDM (whose MCX future may not be in the
    // instruments cache) lost their anchor rows entirely, leaving option
    // positions orphaned in the grid.
    uMap[name] = q ? { ...q, _resolved: info } : { _resolved: info };
  }
  return { underlyings: uMap, contracts: cMap };
}

// ── 6. planAccountSeeding ────────────────────────────────────────────────────

/**
 * Compute the next account-picker state from the two-stage seeding algorithm.
 * Returns a plain object — no reactive assignments, no sessionStorage writes
 * happen inside this function. The caller applies the returned plan.
 *
 * Two-stage rules (preserved verbatim from the original inline block):
 *
 * Stage (b) — late-arrival union (runs every poll):
 *   Accounts in `sorted` but NOT in `seenAccounts` are "new arrivals".
 *   When EITHER (i) we've seen accounts before (seenAny), OR (ii) there's
 *   persisted state (hasPersistedP / hasPersistedH), union the new arrivals
 *   into BOTH pickers. Fixes the Dhan-not-visible bug.
 *
 * Stage (a) — first-load latch:
 *   When `seededFromBrokers` is false and knownBrokers are available,
 *   mark every account as seen and set seededFromBrokers = true.
 *   The picker intentionally starts empty ("All accounts" default), so
 *   no pre-filling is done here — stage (b) above handles late arrivals.
 *
 * @param {{
 *   sorted: string[],
 *   seenAccounts: Set<string>,
 *   positionsAccounts: string[],
 *   holdingsAccounts: string[],
 *   seededFromBrokers: boolean,
 *   hasKnownBrokers: boolean,
 *   orderMap: Record<string, number>,
 *   readPersisted: (key: string) => boolean,
 * }} opts
 * @returns {{
 *   positionsAccounts: string[],
 *   holdingsAccounts: string[],
 *   seenAccounts: Set<string>,
 *   seededFromBrokers: boolean,
 *   persistSeen: string[] | null,
 * }}
 */
export function planAccountSeeding({
  sorted,
  seenAccounts,
  positionsAccounts,
  holdingsAccounts,
  seededFromBrokers,
  hasKnownBrokers,
  orderMap,
  readPersisted,
}) {
  // sortAccountsBy is a pure function in accountSort.js; import it here
  // so this module stays self-contained.
  // We re-implement the sort inline using orderMap to avoid a circular dep
  // on accountSort.js (which itself doesn't import MarketPulse). The sort
  // is stable: unknown accounts get 999, ties break by account_id lexically.
  const sortByOrder = (arr) =>
    [...arr].sort((a, b) => {
      const oa = orderMap[a] ?? 999;
      const ob = orderMap[b] ?? 999;
      if (oa !== ob) return oa - ob;
      return a < b ? -1 : a > b ? 1 : 0;
    });

  // Work on mutable copies so the original caller state is unchanged
  // until the plan is applied.
  const nextSeen = new Set(seenAccounts);
  let nextPositionsAccounts = positionsAccounts;
  let nextHoldingsAccounts  = holdingsAccounts;
  let nextSeededFromBrokers = seededFromBrokers;
  /** @type {string[] | null} */
  let persistSeen = null;

  if (sorted.length === 0) {
    return {
      positionsAccounts: nextPositionsAccounts,
      holdingsAccounts: nextHoldingsAccounts,
      seenAccounts: nextSeen,
      seededFromBrokers: nextSeededFromBrokers,
      persistSeen,
    };
  }

  // Stage (b): late-arrival union.
  const newAccts = sorted.filter(a => !nextSeen.has(a));
  if (newAccts.length > 0) {
    // Skip stage (b) only on the very first sighting (when seenAccounts is
    // empty AND no persisted state exists) — stage (a) below handles that
    // case with the same union.
    const hasPersistedP = readPersisted('mp.positionsAccounts');
    const hasPersistedH = readPersisted('mp.holdingsAccounts');
    const seenAny = seenAccounts.size > 0;
    // Run the union when EITHER (i) we've seen accounts before (genuine
    // late-arrival), OR (ii) there's persisted state (operator's session is
    // mid-stream and a new account is joining).
    if (seenAny || hasPersistedP || hasPersistedH) {
      if (hasPersistedP || positionsAccounts.length > 0) {
        const cur = new Set(positionsAccounts);
        for (const a of newAccts) cur.add(a);
        nextPositionsAccounts = sortByOrder([...cur]);
      }
      if (hasPersistedH || holdingsAccounts.length > 0) {
        const cur = new Set(holdingsAccounts);
        for (const a of newAccts) cur.add(a);
        nextHoldingsAccounts = sortByOrder([...cur]);
      }
    }
    // Mark every account in sorted (including new arrivals) as seen.
    // Done BEFORE stage (a) so stage (a)'s first-load path doesn't treat
    // the same accounts as "new" again.
    for (const a of sorted) nextSeen.add(a);
    persistSeen = [...nextSeen];
  }

  // Stage (a): first-load latch.
  // Operator: "all accounts should be default for positions and holdings in
  // pulse." The picker intentionally STARTS EMPTY (= "All accounts" filter),
  // so first-load just marks the ledger and latches without pre-filling.
  // Stage (b) above handles late-arriving brokers when the operator has
  // explicitly narrowed (non-empty selection).
  if (!nextSeededFromBrokers) {
    for (const a of sorted) nextSeen.add(a);
    persistSeen = [...nextSeen];
    if (hasKnownBrokers) nextSeededFromBrokers = true;
  }

  return {
    positionsAccounts: nextPositionsAccounts,
    holdingsAccounts: nextHoldingsAccounts,
    seenAccounts: nextSeen,
    seededFromBrokers: nextSeededFromBrokers,
    persistSeen,
  };
}
