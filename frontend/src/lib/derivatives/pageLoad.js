/**
 * pageLoad.js — Pure data-transform helpers for the /admin/derivatives page.
 *
 * All functions are pure (no Svelte reactive state, no DOM access).
 * The async functions (loadPositions / loadStrategy) in +page.svelte call
 * these with plain values extracted from reactive state, keeping cc low in
 * the .svelte while keeping logic testable.
 *
 * Extracted from frontend/src/routes/(algo)/admin/derivatives/+page.svelte
 * to reduce cyclomatic complexity in three hotspots:
 *   loadPositions (cc=76), loadStrategy (cc=50), candidatePositions (cc=43).
 */

import { todayIST } from '$lib/dateFormat.js';
import { buildAcctMatcher, isFOSymbol } from '$lib/data/derivativesMath.js';
import { buildPositionRowFromBroker, splitClosedReopened, isExpiredHeldContract, expiredLegFrozenPnl, expiryPnlWithRealised } from '$lib/data/expiryPnl.js';

// splitClosedReopened + buildPositionRowFromBroker moved to
// $lib/data/expiryPnl.js (2026-09 SSOT fix) so portfolioStore.svelte.js can
// reuse the same precise partial/full-close realised-P&L derivation without
// a derivatives-page dependency. Re-exported here unchanged so existing
// call sites (+page.svelte, pageLoad.test.js) are unaffected.
export { buildPositionRowFromBroker, splitClosedReopened };

// ─────────────────────────────────────────────────────────────────────────────
// Shared predicates
// ─────────────────────────────────────────────────────────────────────────────

// isFOSymbol moved to $lib/data/derivativesMath.js (2026-09 Commit 7 fix)
// so portfolioStore.svelte.js can share the SAME exchange-independent
// classification predicate instead of gating on `exchange ∈ {NFO,MCX,CDS,
// BFO}` (which could exclude a Groww-sourced F&O row whose adapter passes
// `exchange` through unchanged). Re-exported here unchanged so existing
// call sites (+page.svelte) are unaffected.
export { isFOSymbol };

/**
 * Build an expiry-match predicate.
 * Empty selectedExpiries = all expiries pass (fail-open / no filter).
 *
 * @param {string[]} selectedExpiries  - YYYY-MM-DD list; empty = no filter
 * @param {(sym: string) => {x?: string} | null} getInstrument
 * @returns {(sym: string) => boolean}
 */
export function buildExpiryMatcher(selectedExpiries, getInstrument) {
  if (!selectedExpiries.length) return () => true;
  return (sym) => {
    const inst = getInstrument(String(sym || '').toUpperCase());
    return selectedExpiries.includes(inst?.x);
  };
}

// ─────────────────────────────────────────────────────────────────────────────
// Position / holding row builders
// ─────────────────────────────────────────────────────────────────────────────
// buildPositionRowFromBroker moved to $lib/data/expiryPnl.js — re-exported
// above unchanged.

/**
 * Map a raw broker holding row into the internal holdings shape.
 * Returns null when both qty and opening_qty are zero (row should be skipped).
 *
 * @param {any} h  - raw broker holding object
 * @returns {object|null}
 */
export function buildHoldingRowFromBroker(h) {
  const sym = String(h?.tradingsymbol || h?.symbol || '').toUpperCase();
  if (!sym) return null;
  const qty = Number(h?.quantity || 0);
  const openingQty = Number(h?.opening_quantity || 0);
  if (!qty && !openingQty) return null;
  return {
    symbol:     sym,
    account:    String(h?.account || ''),
    qty,
    opening_qty: openingQty,
    avg_cost:   h?.average_price != null ? Number(h.average_price) : null,
    ltp:        h?.last_price    != null ? Number(h.last_price)    : null,
    prev_close: Number(h?.prev_close) || null,
    pnl:        h?.pnl != null ? Number(h.pnl) : 0,
    day_change_val: h?.day_change_val != null ? Number(h.day_change_val) : 0,
  };
}

/**
 * Build the derivatives page's own equity-holding rows from
 * `portfolioStore.holdings.rows` (2026-09 Commit 6 — single holdings
 * source). Threads the store's own per-holding Day P&L (`h._day_pnl`,
 * portfolioStore.svelte.js's `_holdTier2` — the canonical `(ltp −
 * prev_close) × qty` formula, `_dcv` fallback when prev_close is
 * unusable) onto each built row as `_storeDayPnl`, so the page never has
 * to reimplement the formula: `buildHoldingRowFromBroker` carries no
 * `realised`/`unrealised`/`prev_settlement_pnl`, so
 * `baseDayPnlForPosition(holdingRow)` — the F&O-oriented formula — would
 * silently fall back to lifetime `pnl` instead of a real Day P&L for a
 * holding row (confirmed bug this fixes).
 *
 * @param {any[]} storeRows - portfolioStore.holdings.rows (raw broker
 *   fields spread + portfolioStore's own `_`-prefixed derived fields,
 *   including `_day_pnl`)
 * @returns {any[]}
 */
export function buildPageHoldingRows(storeRows) {
  const rows = [];
  for (const h of (storeRows || [])) {
    const sym = h?.tradingsymbol || h?.symbol;
    // F&O-shaped rows are picked up by positions, not here — same
    // exclusion the page's own pre-Commit-6 holdings loop applied
    // (portfolioStore.holdings.rows itself is not F&O-filtered: it's a
    // raw reflection of pulseHoldingsStore.value).
    if (sym && isFOSymbol(sym)) continue;
    const row = buildHoldingRowFromBroker(h);
    if (!row) continue;
    if (h?._day_pnl != null && isFinite(Number(h._day_pnl))) {
      row._storeDayPnl = Number(h._day_pnl);
    }
    rows.push(row);
  }
  return rows;
}

/**
 * Increment the excluded-account P&L totals map in-place.
 * Equity intraday positions / derivative holdings are excluded from the
 * F&O panel but must still reconcile against the navbar PositionStrip.
 *
 * @param {Record<string, {pos_pnl:number,pos_day:number,hold_pnl:number,hold_day:number}>} excluded
 * @param {string} acct
 * @param {Partial<{pos_pnl:number,pos_day:number,hold_pnl:number,hold_day:number}>} delta
 */
export function bumpExcluded(excluded, acct, delta) {
  const a = String(acct || '').toUpperCase();
  if (!excluded[a]) {
    excluded[a] = { pos_pnl: 0, pos_day: 0, hold_pnl: 0, hold_day: 0 };
  }
  excluded[a].pos_pnl  += Number(delta.pos_pnl  || 0);
  excluded[a].pos_day  += Number(delta.pos_day  || 0);
  excluded[a].hold_pnl += Number(delta.hold_pnl || 0);
  excluded[a].hold_day += Number(delta.hold_day || 0);
}

// ─────────────────────────────────────────────────────────────────────────────
// Single row-source unification (2026-09, Commit 2 completion)
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Stable per-raw-row identity key (2026-09, D3 post-ship audit fix) —
 * `ACCOUNT|EXCHANGE|TRADINGSYMBOL|PRODUCT`. Kite reports the SAME
 * (account, symbol) pair under two different `product` codes (NRML vs
 * MIS) as genuinely distinct rows — (account, symbol) alone is NOT unique
 * (the same collision Commit 5 already ruled out for a different reason).
 * Including `product` (a real `PositionRow` field) closes that gap.
 *
 * Used to re-look-up a display row's SOURCE raw row inside
 * `portfolioStore.positions.rows` at RENDER time (not baked at build
 * time), so a per-piece value read through it tracks the store's own ~4Hz
 * recompute cadence instead of freezing at the page's ~5s poll rebuild.
 *
 * @param {{account?:string, exchange?:string, tradingsymbol?:string, symbol?:string, product?:string}} p
 * @returns {string}
 */
export function storeRowKey(p) {
  const account  = String(p?.account || '').toUpperCase();
  const exchange = String(p?.exchange || '').toUpperCase();
  const sym      = String(p?.tradingsymbol || p?.symbol || '').toUpperCase();
  const product  = String(p?.product || '').toUpperCase();
  return `${account}|${exchange}|${sym}|${product}`;
}

/**
 * Live per-piece Exp P&L lookup (2026-09, D3 post-ship audit fix) — reads
 * `piecesByKey[c._storeKey]` (a live, render-time map built from the
 * CURRENT `portfolioStore.positions.rows`, e.g. by the caller's own
 * `$derived.by`) instead of the value `buildPagePositionRows` baked onto
 * the row at its last (~5s poll cadence) build.
 *
 * Falls back to `c._storeExpPnl` (the baked value) when the live pieces
 * array is missing, or its length no longer matches `c._pieceCount` — this
 * covers the one render frame where the store has already recomputed
 * (e.g. a same-day partial-close event just landed, changing the split)
 * but the page's own `positions` rebuild (which re-derives `_pieceCount`
 * to match) hasn't run yet; using a piece index against a
 * differently-shaped pieces array would silently read the wrong piece.
 *
 * @param {{_storeKey?:string, _pieceIndex?:number, _pieceCount?:number, _storeExpPnl?:number|null}} c
 * @param {Record<string, Array<number|null>|null>} piecesByKey
 * @returns {number|null}
 */
export function liveStoreExpPnl(c, piecesByKey) {
  const pieces = piecesByKey?.[c?._storeKey];
  if (Array.isArray(pieces) && pieces.length === c?._pieceCount) {
    return pieces[c._pieceIndex] ?? null;
  }
  return c?._storeExpPnl ?? null;
}

/**
 * Build the derivatives page's own per-row F&O position rows from raw
 * broker/store position rows, splitting each into closed/open display
 * pieces. Consolidates the identical `isFOSymbol` filter +
 * `buildPositionRowFromBroker` + `splitClosedReopened` loop that used to be
 * duplicated in BOTH the book-poller propagation `$effect` and
 * `loadPositions()` in +page.svelte.
 *
 * `storeRows` is expected to be `portfolioStore.positions.rows` — the SAME
 * unsplit per-row F&O+equity array the Snapshot grid's `expPnlRows`/`byKey`
 * rollups are built from (portfolioStore.svelte.js's `_posTier3`), already
 * threaded through the store's own SWR/degraded-freeze guard. Rows carry
 * both the raw broker field names (`tradingsymbol`, `quantity`,
 * `average_price`, ...) AND portfolioStore's derived `_`-prefixed fields
 * (spread, not replaced) — `buildPositionRowFromBroker` only reads the raw
 * fields, so it works unchanged on either a raw `positionsStore.value` row
 * or a `portfolioStore.positions.rows` row.
 *
 * `simRows` are passed through VERBATIM (already built + split by the
 * caller, e.g. via `buildSimPositionRows` below) — sim positions aren't
 * part of `storeRows` (portfolioStore only reflects the live broker book).
 *
 * @param {any[]} storeRows  - live broker rows (raw fields required: tradingsymbol/symbol, quantity, ...)
 * @param {any[]} [simRows]  - already-built + split sim rows (source:'sim'), appended as-is
 * @returns {any[]}
 */
export function buildPagePositionRows(storeRows, simRows = []) {
  const merged = [];
  for (const p of storeRows || []) {
    const sym = p?.tradingsymbol || p?.symbol;
    if (!sym) continue;
    if (!isFOSymbol(sym)) continue; // Equity intraday — excluded from F&O panel
    const baseRow = buildPositionRowFromBroker(p, 'live');
    const pieces = splitClosedReopened(baseRow);
    // Store-side per-piece Exp P&L (2026-09 Commit 5, revised D3 post-ship
    // audit fix): `p._exp_pnl_pieces` (portfolioStore.svelte.js's own
    // _posTier2 output) is the SAME splitClosedReopened applied to the
    // SAME raw row, so piece `i` here always corresponds to
    // `p._exp_pnl_pieces[i]`. `_storeExpPnl` (baked at THIS build) is kept
    // as a fallback value only — the primary read path is now a LIVE
    // lookup (`liveStoreExpPnl`, below) via `_storeKey`/`_pieceIndex`/
    // `_pieceCount`, re-resolved against the CURRENT
    // `portfolioStore.positions.rows` at render time instead of frozen at
    // this (~5s poll cadence) build. Without this, the Legs Exp P&L cell
    // was frozen at poll cadence while the store itself (and Snapshot,
    // which reads it directly) recomputes at ~4Hz via `_rootSpotCache`/
    // `_tick` — a visible Legs-vs-Snapshot mismatch between polls.
    const storePieces = Array.isArray(p?._exp_pnl_pieces) ? p._exp_pnl_pieces : null;
    const key = storeRowKey(p);
    pieces.forEach((row, i) => {
      if (storePieces) row._storeExpPnl = storePieces[i] ?? null;
      row._storeKey     = key;
      row._pieceIndex   = i;
      row._pieceCount   = pieces.length;
      merged.push(row);
    });
  }
  return [...merged, ...simRows];
}

/**
 * Build + split sim-mode position rows from the simulator's raw position
 * list (`fetchSimStatus()`'s `.positions` array) — same per-row transform
 * as `buildPagePositionRows`' live loop, tagged `source:'sim'`. Kept as a
 * separate function (not folded into `buildPagePositionRows`) because the
 * two inputs need genuinely different treatment at their call sites: fresh
 * raw sim positions here vs. already-split sim rows reused verbatim from
 * the page's current `positions` state in the book-poller propagation path.
 *
 * @param {any[]} simPositions  - raw sim position rows (symbol, quantity/qty, ...)
 * @returns {any[]}
 */
export function buildSimPositionRows(simPositions) {
  const merged = [];
  for (const p of (simPositions || [])) {
    const sym = p?.symbol;
    if (!sym || !isFOSymbol(sym)) continue;
    const baseRow = buildPositionRowFromBroker(p, 'sim');
    for (const row of splitClosedReopened(baseRow)) merged.push(row);
  }
  return merged;
}

// ─────────────────────────────────────────────────────────────────────────────
// candidatePositions body
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Derive the candidate position rows for the selected underlying.
 *
 * Mirrors the $derived.by body in +page.svelte so the pure logic is
 * testable without reactive wiring. The $derived shell in the page
 * passes all reactive state as plain values here.
 *
 * Three position sources appear in this order:
 *   1. Real positions (live/sim) + equity holdings + proxy hedges + local drafts
 *   2. Provisional positions (~) — fills received before broker book refreshes
 *   3. Draft store positions (D) — operator-added hypothetical positions from store
 *
 * @param {{
 *   positions: any[],
 *   holdings: any[],
 *   drafts: any[],
 *   target: string,
 *   selectedExpiries: string[],
 *   selectedAccounts: string[],
 *   simActive: boolean,
 *   proxiesForTarget: (t: string) => string[],
 *   getInstrument: (sym: string) => {x?: string} | null,
 *   hasFNO?: (root: string) => boolean,
 *   provisionalPositions?: Map<string, any>,
 *   draftStorePositions?: Map<string, any>,
 * }} params
 * @returns {any[]}
 */
export function buildCandidatePositions({
  positions, holdings, drafts,
  target, selectedExpiries, selectedAccounts, simActive,
  proxiesForTarget, getInstrument, hasFNO,
  provisionalPositions,
  draftStorePositions,
}) {
  const prefixRe = new RegExp(`^${target}\\d`, 'i');
  const wantedSource = simActive ? 'sim' : 'live';
  const matchExpiry  = buildExpiryMatcher(selectedExpiries, getInstrument);

  // 2026-09 fix: the old inline matcher compared the raw row account
  // against `selectedAccounts` with NO normalisation on either side
  // (exact match only) — any case/whitespace difference between the
  // operator's selection and the broker-reported account code silently
  // never matched. buildAcctMatcher (derivativesMath.js) trim+uppercases
  // BOTH sides and is the same matcher every other $derived.by in
  // +page.svelte already uses (lines 705/821/870/1180/1695 there) — this
  // closes the last divergent copy.
  const matchAccount = buildAcctMatcher(selectedAccounts);

  /** @type {any[]} */
  const real = [];
  /** @type {any[]} */
  const provisional = [];
  /** @type {any[]} */
  const draftStore = [];

  // F&O positions
  for (const p of positions) {
    if (p.source !== wantedSource) continue;
    if (!matchAccount(p.account)) continue;
    const sym = p.symbol;
    if (!prefixRe.test(sym)) continue;
    const isFut = /FUT$/i.test(sym);
    const isOpt = /(CE|PE)$/i.test(sym);
    if (!isFut && !isOpt) continue;
    const qty = Number(p?.qty || 0);
    const _inst = getInstrument(sym);
    // A HELD row (qty!==0) whose contract can't be resolved against the
    // live instruments master — either missing entirely (expired and
    // removed by Kite) or carrying an `x` (expiry) date that's already
    // passed — is tagged `_expired: true` and stays VISIBLE and counted,
    // instead of being silently dropped (2026-09 GOLDM regression: a
    // fully-held expired contract vanished from the Legs/Payoff/candidates
    // pipeline the day after expiry, while Snapshot — whose portfolioStore
    // pipeline has no such exclusion — kept showing 17 legs / real P&L for
    // the same underlying). See buildCleanLegs below for why this row is
    // still excluded from the /strategy-analytics REQUEST payload only.
    const isUnresolvable = qty !== 0 && (!_inst || (_inst.x && _inst.x < todayIST()));
    // 2026-09 audit fix (Defect 2 follow-up): `_expired` above stays the
    // BROAD, cache-resolvability signal — it ONLY gates the
    // /strategy-analytics REQUEST payload exclusion below (buildCleanLegs),
    // since an unresolvable-in-cache leg genuinely can't be sent there
    // regardless of whether it's economically expired. `_expiredFrozen` is
    // the NARROWER, shared `isExpiredHeldContract` predicate (same one
    // portfolioStore.svelte.js's Snapshot pipeline calls) — it's what the
    // four chart VALUATION sites in +page.svelte key on to decide whether
    // to use the frozen basis. Keeping these as two separate flags (rather
    // than narrowing `_expired` itself) means a leg that's unresolvable-
    // but-NOT-confidently-expired (BFO, cold start) still gets excluded
    // from the backend payload (correct — the backend genuinely can't
    // price it) while ALSO staying excluded (not frozen, not spot-valued)
    // from the chart's valuation, matching pre-session (D4(a)) behaviour
    // for that specific case, and matching the store's own treatment of
    // the SAME symbol (which won't tag it expired either, from the SAME
    // predicate) — no NEW divergence between the two surfaces.
    const isExpiredFrozen = qty !== 0 && isExpiredHeldContract(sym, qty, getInstrument, hasFNO);
    // The operator's expiry SELECTOR (selectedExpiries) is a genuine UI
    // filter for KNOWN, unexpired contracts that simply aren't in the
    // chosen selection — it must still apply to those. Only bypass it for
    // a row we can't resolve at all (isUnresolvable) or a flat (qty=0)
    // historical row (matchExpiry itself already no-ops on those via the
    // qty!==0 guard below, preserved from the original code).
    if (!isUnresolvable && qty !== 0 && !matchExpiry(sym)) continue;
    real.push({
      ...p,
      kind: isFut ? 'fut' : 'opt',
      ...(isUnresolvable ? { _expired: true } : {}),
      ...(isExpiredFrozen ? { _expiredFrozen: true } : {}),
    });
  }

  // Direct equity holdings of the underlying
  for (const h of holdings) {
    const sym = String(h.symbol || '').toUpperCase();
    if (sym !== target) continue;
    if (!matchAccount(h.account)) continue;
    if (!Number(h.qty || 0)) continue;
    real.push({ ...h, source: 'live', kind: 'eq' });
  }

  // Proxy hedges (GOLDBEES → GOLD etc.)
  const _allowedProxies = new Set(proxiesForTarget(target));
  if (_allowedProxies.size) {
    for (const h of holdings) {
      const sym = String(h.symbol || '').toUpperCase();
      if (!_allowedProxies.has(sym)) continue;
      if (!matchAccount(h.account)) continue;
      if (!Number(h.qty || 0)) continue;
      real.push({ ...h, source: 'live', kind: 'eq', proxy_for: target });
    }
  }

  // Local drafts — no account filter (drafts are not tied to a broker account)
  for (const d of drafts) {
    const sym = String(d.symbol || '').toUpperCase();
    if (!sym || !prefixRe.test(sym)) continue;
    const isFut = /FUT$/i.test(sym);
    const isOpt = /(CE|PE)$/i.test(sym);
    if (!isFut && !isOpt) continue;
    if (!matchExpiry(sym)) continue;
    const _dInst = getInstrument(sym);
    if (!_dInst) continue;
    if (_dInst.x && _dInst.x < todayIST()) continue;
    const qty  = d.qty      === '' || d.qty      == null ? 0    : Number(d.qty);
    const cost = d.avg_cost === '' || d.avg_cost == null ? null : Number(d.avg_cost);
    const ltp  = d.ltp      === '' || d.ltp      == null ? null : Number(d.ltp);
    real.push({
      symbol: sym, account: '', qty, avg_cost: cost, ltp,
      source: 'draft', kind: isFut ? 'fut' : 'opt', draftId: d.id,
    });
  }

  // Provisional positions (~) — post-fill, pre-broker-refresh
  if (provisionalPositions) {
    for (const entry of provisionalPositions.values()) {
      const sym = String(entry.tradingsymbol || '').toUpperCase();
      if (!sym || !prefixRe.test(sym)) continue;
      const isFut = /FUT$/i.test(sym);
      const isOpt = /(CE|PE)$/i.test(sym);
      if (!isFut && !isOpt) continue;
      if (!matchExpiry(sym)) continue;
      if (!matchAccount(entry.account)) continue;
      provisional.push({
        symbol:    sym,
        account:   String(entry.account || ''),
        qty:       Number(entry.quantity || 0),
        lots:      entry.lots     != null ? Number(entry.lots)     : null,
        lot_size:  entry.lot_size != null ? Number(entry.lot_size) : null,
        avg_cost:  entry.average_price != null ? Number(entry.average_price) : null,
        ltp:       entry.last_price    != null ? Number(entry.last_price)    : null,
        prev_close: 0,
        pnl:       Number(entry.pnl || 0),
        realised:  Number(entry.realised || 0),
        day_change_val: Number(entry.day_change_val || 0),
        source:    'provisional',
        kind:      isFut ? 'fut' : 'opt',
        _provisional: true,
      });
    }
  }

  // Draft store positions (D) — operator-added hypothetical store entries
  if (draftStorePositions) {
    for (const entry of draftStorePositions.values()) {
      const sym = String(entry.tradingsymbol || '').toUpperCase();
      if (!sym || !prefixRe.test(sym)) continue;
      const isFut = /FUT$/i.test(sym);
      const isOpt = /(CE|PE)$/i.test(sym);
      if (!isFut && !isOpt) continue;
      if (!matchExpiry(sym)) continue;
      if (!matchAccount(entry.account)) continue;
      draftStore.push({
        symbol:    sym,
        account:   String(entry.account || ''),
        qty:       Number(entry.quantity || 0),
        lots:      entry.lots     != null ? Number(entry.lots)     : null,
        lot_size:  entry.lot_size != null ? Number(entry.lot_size) : null,
        avg_cost:  entry.average_price != null ? Number(entry.average_price) : null,
        ltp:       entry.last_price    != null ? Number(entry.last_price)    : null,
        prev_close: 0,
        pnl:       Number(entry.pnl || 0),
        realised:  Number(entry.realised || 0),
        day_change_val: Number(entry.day_change_val || 0),
        source:    'draft_store',
        kind:      isFut ? 'fut' : 'opt',
        _draft_store: true,
      });
    }
  }

  // Sort each group: closed (qty=0) to end within group, stable otherwise.
  const closedLast = (a, b) => {
    const ac = (Number(a?.qty || 0) === 0) ? 1 : 0;
    const bc = (Number(b?.qty || 0) === 0) ? 1 : 0;
    return ac - bc;
  };
  real.sort(closedLast);
  provisional.sort(closedLast);
  draftStore.sort(closedLast);

  // Ordering: real first, then provisional (~), then draft store (D).
  return [...real, ...provisional, ...draftStore];
}

// ─────────────────────────────────────────────────────────────────────────────
// loadStrategy helpers
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Filter and normalise the legs array for the strategy analytics endpoint.
 * Equity (kind='eq') legs are excluded — the backend only accepts F&O.
 * Ltp is inlined only for sim / draft sources.
 *
 * `_expired`-tagged legs (buildCandidatePositions above, 2026-09 GOLDM fix)
 * are excluded here too — but ONLY from this REQUEST payload, not from the
 * Legs grid / candidates list / Day-Exp-P&L totals, which all read
 * candidatePositions directly and still show the row. Confirmed by reading
 * backend/api/routes/options.py:_strategy_collect_leg_metadata — it raises
 * HTTPException(400) for the WHOLE `/strategy-analytics` request the moment
 * any single leg fails to parse/resolve an expiry, which would 500 the
 * payoff curve for every OTHER live leg too if an unresolvable expired leg
 * were sent through. The payoff curve itself legitimately has no forward
 * shape for an expired contract anyway — its current MTM/Exp P&L still
 * counts in the totals shown alongside the chart, computed client-side.
 *
 * @param {any[]} legs
 * @param {(sym: string) => {x?: string} | null} getInstrument
 * @returns {{symbol:string, qty:number, avg_cost:number|null, ltp:number|null, expiry:string|null}[]}
 */
export function buildCleanLegs(legs, getInstrument) {
  return legs
    .filter(l => l.kind !== 'eq')
    .filter(l => !l._expired)
    .map(l => {
      const sym    = String(l.symbol || '').trim().toUpperCase();
      const inst   = sym ? getInstrument(sym) : null;
      const expiry = inst?.x || null;
      return {
        symbol:   sym,
        qty:      l.qty === '' || l.qty == null ? 0 : Number(l.qty),
        avg_cost: l.avg_cost === '' || l.avg_cost == null ? null : Number(l.avg_cost),
        ltp: (l.source === 'sim' || l.source === 'draft' ||
              l.source === 'provisional' || l.source === 'draft_store')
          ? (l.ltp === '' || l.ltp == null ? null : Number(l.ltp))
          : null,
        expiry,
      };
    })
    .filter(l => l.symbol && l.qty && !(l.expiry && l.expiry < todayIST()));
}

/**
 * True when at least one non-equity leg is held (nonzero qty) AND
 * resolvable (not `_expired`-tagged) — 2026-09 post-ship audit fix (D2).
 *
 * `loadStrategy`'s equity-only-shell branch (+page.svelte) wipes `strategy`
 * to null when this is false, so the Payoff card falls through to
 * `_clientPayoffStub` instead of leaking a stale/previous root's chart.
 * The naive check (`legs.some(l => l.kind !== 'eq' && Number(l.qty) !== 0)`)
 * reads the PRE-filter `legs` array, which still includes `_expired`-
 * tagged rows with real nonzero qty (Commit 2's GOLDM fix keeps them
 * visible) — so a root whose ENTIRE F&O book is expired-but-held (no eq
 * legs either) read `true` from those still-nonzero-qty expired rows,
 * `strategy` was never wiped, and the Payoff card got stuck in its
 * loading/placeholder state forever — the exact scenario this whole plan
 * exists to fix.
 *
 * @param {any[]} legs
 * @returns {boolean}
 */
export function hasEnabledFOLegs(legs) {
  return (legs || []).some(l => l.kind !== 'eq' && Number(l.qty) !== 0 && !l._expired);
}

/**
 * Build +page.svelte's `legs` array (the `$state` array `_clientPayoffStub`
 * / `buildCleanLegs` / `hasEnabledFOLegs` all read) from `candidatePositions`.
 *
 * Extracted as a pure function (2026-09 audit fix, Defect 1) so it can be
 * unit-tested directly against real candidate-row shapes — this exact
 * mapping previously lived only inline inside a Svelte `$effect` in
 * +page.svelte, where a change that stripped `pnl`/`realised`/`unrealised`
 * (needed by `expiredLegFrozenPnl`/`currentTotalProfit` to value an
 * `_expiredFrozen` leg) shipped completely undetected — nothing exercised
 * this exact mapping in isolation.
 *
 * @param {any[]} candidatePositions
 * @param {(c: any) => boolean} isLegEnabled
 * @param {boolean} showDraftInPayoff
 * @returns {any[]}
 */
export function buildPageLegs(candidatePositions, isLegEnabled, showDraftInPayoff) {
  return candidatePositions
    .filter(c => {
      if (!isLegEnabled(c)) return false;
      if (!showDraftInPayoff &&
          (c.source === 'provisional' || c.source === 'draft_store' || c.source === 'draft')) return false;
      return true;
    })
    .map(c => ({
      symbol:   c.symbol,
      qty:      c.qty,
      avg_cost: c.avg_cost ?? '',
      ltp:      c.ltp ?? '',
      source:   c.source,
      kind:     c.kind,
      // `_expired` (buildCleanLegs' payload-exclusion signal) MUST survive
      // this mapping — see buildCandidatePositions' own comment on why
      // it's kept separate from `_expiredFrozen` below.
      _expired: c._expired,
      // `_expiredFrozen` (2026-09 Defect 1/2 fix) — the NARROWER, shared
      // isExpiredHeldContract signal the chart valuation sites key on.
      _expiredFrozen: c._expiredFrozen,
      // 2026-09 Defect 1 fix: pnl/realised/unrealised MUST survive this
      // mapping — expiredLegFrozenPnl (currentTotalProfit) reads them to
      // value an `_expiredFrozen` leg; without them it silently fell back
      // to 0 (the exact bug this fix closes — the fields were previously
      // stripped here with nothing to catch it).
      pnl:        c.pnl,
      realised:   c.realised,
      unrealised: c.unrealised,
    }));
}

/**
 * Sum of the frozen Exp P&L contribution from every `_expiredFrozen`-
 * tagged leg in a `buildPageLegs`-shaped array — the constant
 * `_clientPayoffStub` (+page.svelte) folds into every grid point for a
 * root whose ENTIRE F&O book is expired-but-held (GOLD/GOLDM). Also folds
 * in the realised portion of any TODAY-closed (qty=0) leg via
 * `expiryPnlWithRealised(l, null)`'s qty=0 branch — without this, a root
 * mixing an expired-held leg with a leg closed earlier today would still
 * diverge from Snapshot (which sums both) by the closed leg's realised
 * amount, since the stub curve has no other path for a qty=0 leg's value.
 * @param {any[]} legs
 * @returns {number}
 */
export function sumExpiredFrozenLegsPnl(legs) {
  let s = 0;
  for (const l of (legs || [])) {
    if (l._expiredFrozen) {
      s += expiredLegFrozenPnl(l);
      continue;
    }
    // qty=0 rows never carry _expired/_expiredFrozen (both tags are
    // gated on qty!==0 in buildCandidatePositions) — a today-closed leg
    // reaches here untagged; fold in its realised portion too.
    if (Number(l.qty || 0) === 0) {
      const v = expiryPnlWithRealised(l, null);
      if (v != null && isFinite(Number(v))) s += Number(v);
    }
  }
  return s;
}

function _normAcct(a) {
  return String(a || '').trim().toUpperCase();
}

/**
 * Root-scoped positions TRUST check (2026-09 R5 redesign — replaces a
 * WHOLE-BOOK freshness gate that was too broad). "Trusted" here means
 * "none of the accounts relevant to this root is the one currently
 * degraded" — NOT "this root's rows are guaranteed current." Note the
 * distinction: portfolioStore.svelte.js's SWR guard freezes the ENTIRE
 * positions slice (every account's rows, not just the degraded one's)
 * whenever ANY account is degraded — so during a partial outage, even a
 * perfectly healthy account's rows are frozen at whatever they were just
 * before the outage started, same as the degraded account's. This
 * function only answers "is it safe to ACT on that frozen data for this
 * root" (i.e. wipe the strategy), not "is that data fresher than it
 * looks." Keeping healthy accounts' rows genuinely live during a partial
 * outage would require per-account (not whole-slice) freezing in
 * portfolioStore — a separate, larger change, not done here.
 *
 * Original R5 fix gated the strategy-wipe decision on
 * `portfolioStore.positions.fresh` — a single aggregate flag that is
 * `false` whenever ANY account anywhere in the book is degraded, even an
 * account with zero relationship to the currently selected underlying.
 * Operator-reported regression: a sustained partial outage on one
 * account (e.g. a Dhan circuit breaker) could permanently block the
 * strategy-wipe gate for every OTHER root, re-sticking the Payoff card
 * in "loading" for symbols that have nothing to do with the degraded
 * account.
 *
 * This function narrows the check to only the accounts relevant to the
 * CURRENTLY SELECTED root:
 *
 *  - Account filter active (`selectedAccountFilter` non-empty): the
 *    relevant set IS the filter — the operator has explicitly scoped to
 *    those accounts, so we can answer definitively even if
 *    `candidateAccounts` is empty (e.g. the filtered accounts happen to
 *    hold nothing for this root right now).
 *  - No filter, `candidateAccounts` non-empty: intersect those accounts
 *    against `staleAccounts` (from `positionsStore.meta.staleAccounts`).
 *    A `fetchFailed` (whole-read exception, see markFetchFailedMeta in
 *    dataStore.svelte.js) makes every relevant account untrustworthy
 *    unconditionally — a thrown exception isn't attributable to specific
 *    accounts the way a partial `stale_accounts` substitution is.
 *  - No filter, `candidateAccounts` EMPTY (no account anywhere currently
 *    shows a position for this root): this is ambiguous on its own — it
 *    could mean "verified: nobody holds this root" OR "we've never
 *    successfully fetched the book this session, so we don't actually
 *    know." Resolved by `hasHadFreshSnapshot` — the frozen SWR guard in
 *    portfolioStore.svelte.js means `candidatePositions` reflects
 *    whatever the LAST truly fresh (non-degraded) full read produced,
 *    held frozen through any subsequent degraded reads. If a fresh
 *    snapshot has landed at least once this session, an empty relevant
 *    set was VERIFIED empty as of that read and stays trustworthy even
 *    if the CURRENT read is degraded/failed — a subsequent failure
 *    doesn't retroactively invalidate a fact already established. If no
 *    fresh snapshot has EVER landed (e.g. a cold start whose first fetch
 *    throws), empty means unknown, not verified-empty — untrusted. This
 *    specifically prevents a cold-start regression: a strategy cached in
 *    sessionStorage getting wiped to null because the very first
 *    positions fetch happened to fail, with no legs by which to even
 *    guess an account.
 *
 * @param {{
 *   candidateAccounts: Array<string|null|undefined>,
 *   selectedAccountFilter?: Array<string|null|undefined>,
 *   staleAccounts?: Array<string|null|undefined>,
 *   fetchFailed?: boolean,
 *   hasHadFreshSnapshot?: boolean,
 * }} args
 * @returns {boolean}
 */
export function isRootPositionsTrusted({
  candidateAccounts,
  selectedAccountFilter = [],
  staleAccounts = [],
  fetchFailed = false,
  hasHadFreshSnapshot = false,
}) {
  const filterActive = Array.isArray(selectedAccountFilter) && selectedAccountFilter.length > 0;
  const relevantSource = filterActive ? selectedAccountFilter : (candidateAccounts || []);
  const relevant = new Set(relevantSource.map(_normAcct).filter(Boolean));

  if (relevant.size === 0) {
    // Only reachable when no account filter is active AND no candidate
    // account was found for this root — see the "EMPTY" branch in the
    // doc comment above.
    return !!hasHadFreshSnapshot;
  }

  // A whole-read exception can't be attributed to specific accounts —
  // every relevant account is suspect, no exceptions, regardless of
  // whatever staleAccounts a PRIOR successful fetch happened to report.
  if (fetchFailed) return false;

  const stale = new Set((staleAccounts || []).map(_normAcct).filter(Boolean));
  for (const acct of relevant) {
    if (stale.has(acct)) return false;
  }
  return true;
}

/**
 * Build a stable string key from the cleanLegs array for memoisation.
 * Two calls with identical legs produce identical keys.
 *
 * @param {{symbol:string, qty:number, avg_cost:number|null, ltp:number|null, expiry:string|null}[]} cleanLegs
 * @returns {string}
 */
export function computeLegsKey(cleanLegs) {
  return cleanLegs.map(l =>
    `${l.symbol}:${l.qty}:${l.avg_cost ?? ''}:${l.ltp ?? ''}:${l.expiry ?? ''}`
  ).join('|');
}

/**
 * Detect whether the first clean leg's underlying root has changed
 * vs the currently-displayed strategy.
 *
 * @param {{symbol:string}[]} cleanLegs
 * @param {any|null} currentStrategy  - the strategy object currently rendered
 * @param {(sym:string) => {root:string}} decomposeSymbol
 * @returns {boolean}
 */
export function didUnderlyingChange(cleanLegs, currentStrategy, decomposeSymbol) {
  if (!cleanLegs.length) return false;
  const newU = decomposeSymbol(cleanLegs[0].symbol).root;
  const prevLegs = currentStrategy?.legs;
  if (!prevLegs?.length) return false;
  const oldU = decomposeSymbol(prevLegs[0].symbol).root;
  return !!(newU && oldU && newU !== oldU);
}

/**
 * Build a cache key for the equity-only synth strategy.
 * Encodes (underlying, per-leg symbol+qty+cost+ltp, target spot/prev-close)
 * so a re-derive only fires when inputs actually changed.
 *
 * `targetSpot`/`targetPrevClose` (D2026-09 proxy-hedge fix) are included so
 * a proxy-hedged root's shell re-derives when the HEDGED root's own live
 * price ticks — not just when the proxy leg's own ltp/qty/cost changes.
 * Without this, a GOLDBEES proxy leg whose own price is flat would freeze
 * the GOLDM shell's spot at whatever GOLDM price was live on the first
 * derive, even as GOLDM itself keeps ticking.
 *
 * @param {string} underlying  - selectedUnderlying value
 * @param {any[]} eqs          - equity leg rows
 * @param {number} [targetSpot]      - target root's own live LTP (proxy case)
 * @param {number} [targetPrevClose] - target root's own live prev-close
 * @returns {string}
 */
export function synthCacheKey(underlying, eqs, targetSpot, targetPrevClose) {
  const parts = [underlying || '', Number(targetSpot) || 0, Number(targetPrevClose) || 0];
  for (const e of eqs) {
    parts.push(
      `${e.symbol || ''}:${Number(e.qty) || 0}:${Number(e.avg_cost) || 0}:${Number(e.ltp) || 0}`
    );
  }
  return parts.join('|');
}

/**
 * Build a strategy-shaped stub for an equity-only basket so the payoff
 * card renders a linear long-stock curve when no options/futures are present.
 * Returns null when no eq leg has a usable spot anchor.
 *
 * Proxy-hedge aware (2026-09 fix, incident: GOLDM Exp P&L inflated ~1211×
 * when its options settled to qty=0, leaving only GOLDBEES beta-hedge
 * legs — `proxy_for: 'GOLDM'`). When any passed eq leg is a proxy for
 * `underlying`, the shell MUST price itself in the HEDGED root's own price
 * space (`targetSpot`/`targetPrevClose`, sourced by the caller from
 * `_undLive[underlying]` — the same SSOT the Snapshot grid and payoffSpot
 * use) — NOT the proxy ETF's own `primary.ltp`/`primary.prev_close`, which
 * can differ from the hedged root's spot by three orders of magnitude
 * (e.g. GOLDBEES ₹124.43 vs GOLDM ₹1,50,736). Returns null rather than
 * falling back to the proxy's own price when no target spot is available
 * yet (cold start) — showing a wrong-scale shell is worse than showing none.
 *
 * When no leg carries `proxy_for` (the eq legs genuinely ARE the plotted
 * underlying, e.g. holding GOLDBEES under its own "GOLDBEES" tab), behavior
 * is UNCHANGED — `primary.ltp`/`primary.prev_close` remain the basis, as
 * before this fix.
 *
 * @param {any[]} eqs       - equity leg rows (kind='eq')
 * @param {string} underlying - selectedUnderlying value
 * @param {number} [targetSpot]      - target root's own live LTP, required
 *   when any leg proxies `underlying` (from `_undLive[underlying]?.ltp`)
 * @param {number} [targetPrevClose] - target root's own live prev-close
 *   (from `_undLive[underlying]?.close`)
 * @returns {object|null}
 */
export function synthEquityOnlyStrategy(eqs, underlying, targetSpot, targetPrevClose) {
  if (!Array.isArray(eqs) || eqs.length === 0) return null;
  const primary = eqs.find(e => Number(e.ltp) > 0) || eqs[0];

  const _und = String(underlying || '').toUpperCase();
  const isProxyHedge = eqs.some(e => String(e.proxy_for || '').toUpperCase() === _und && _und);
  const _targetSpot = Number(targetSpot) || 0;

  let spot;
  if (isProxyHedge) {
    // Proxy-hedge case (D1 fix): price the shell in the HEDGED root's own
    // space, never the proxy ETF's own ltp/avg_cost.
    if (_targetSpot <= 0) return null;
    spot = _targetSpot;
  } else {
    spot = Number(primary.ltp) || Number(primary.avg_cost) || 0;
  }
  if (spot <= 0) return null;

  const _targetPrevClose = Number(targetPrevClose) || 0;
  const prevClose = isProxyHedge
    ? (_targetPrevClose > 0 ? _targetPrevClose : spot)
    : (Number(primary.prev_close) || spot);
  const spanPct   = 0.15;
  const N = 41;
  const lo = spot * (1 - spanPct);
  const hi = spot * (1 + spanPct);

  const payoff = [];
  for (let i = 0; i < N; i++) {
    const s = lo + (i / (N - 1)) * (hi - lo);
    payoff.push({ spot: s, today_value: 0, expiry_value: 0 });
  }

  let netCost = 0;
  for (const e of eqs) {
    const qty  = Number(e.qty) || Number(e.opening_qty) || 0;
    const cost = Number(e.avg_cost) || 0;
    netCost += qty * cost;
  }

  return {
    payoff,
    spot,
    spot_prev_close:      prevClose,
    spot_source:          'live',
    spot_anchor_contract: null,
    underlying:           underlying || '',
    legs:                 [],
    multi_expiry:         false,
    expiry:               null,
    days_to_expiry:       0,
    span_sigmas:          0,
    span_pct:             spanPct,
    iv_proxy:             0,
    net_cost:             netCost,
    intermediate_curves:  [],
    risk: {
      max_profit: 0, max_loss: 0, breakevens: [],
      rr_ratio: null, ev: null, ev_pct: null, pop: null,
    },
    aggregate_greeks: { delta: 0, gamma: 0, theta: 0, vega: 0, rho: 0 },
  };
}
