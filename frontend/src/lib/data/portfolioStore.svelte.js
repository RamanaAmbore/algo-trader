/**
 * portfolioStore — unified SWR (stale-while-revalidating) SSOT that absorbs
 * computation from positionsDerivedStore, holdingsDayPnlStore, and funds
 * aggregation into one reactive $derived.by().
 *
 * The SWR null-guard at the top of _portfolio prevents the 14 race-prone sites
 * where positionsStore / pulseHoldingsStore / fundsStore momentarily go null
 * during a 5s poll refresh, which used to cause derived values (chg%, day_pnl,
 * todayMtm, exp_pnl) to zero out. When any required dep is null, the last
 * known snapshot is returned instead of computing with empty arrays.
 *
 * Exports:
 *   portfolioStore.positions  — { total, byKey, byRootPositions, byRootHoldings, byRoot, expiryByAcct, rows, fresh }
 *   portfolioStore.holdings   — { total, byKey, byAccount, rows, fresh }
 *   portfolioStore.funds      — { total, byAccount }
 */

import { browser } from '$app/environment';
import { untrack } from 'svelte';
import { symbolTickCount, getSnapshot, liveSnap } from '$lib/data/symbolStore.svelte.js';
import { positionsStore, pulseHoldingsStore, fundsStore } from '$lib/data/marketDataStores.svelte.js';
import { baseDayPnlForPosition, dayChangePct } from '$lib/data/nav.js';
import { getUnderlyingSpot } from '$lib/data/underlyingSpotStore.svelte.js';
import { resolveExpiryAnchor, legExtrinsicDisplay, positionExpPnl, positionExpPnlPieces, isExpiredHeldContract, expiredPositionExpPnl, expiredPositionExpPnlPieces } from '$lib/data/expiryPnl.js';
import { decomposeSymbol } from '$lib/data/decomposeSymbol.js';
import { targetsForProxy, getProxyRow } from '$lib/data/hedgeProxies.js';
import { getInstrument, instrumentsCacheVersion } from '$lib/data/instruments';
import { isFOSymbol } from '$lib/data/derivativesMath.js';
import { sumMarginAvail, sumMarginTotal, sumLiveCashTotal } from '$lib/data/fundsAggregate.js';

// ── 4 Hz throttle (250ms debounce on symbolTickCount) ───────────────────────
// Same pattern as positionsDerivedStore / holdingsDayPnlStore.
let _tick = $state(0);
/** @type {ReturnType<typeof setTimeout>|null} */
let _tickTimer = null;
if (browser) {
  symbolTickCount.subscribe(() => {
    if (_tickTimer) return;
    _tickTimer = setTimeout(() => { _tickTimer = null; _tick++; }, 250);
  });
}

// 2026-09 audit fix (should-do, Defect 1/2 follow-up): bump `_tick` when
// the instruments master (re)loads, same as the symbolTickCount subscribe
// above. Without this, `_posTier2`'s isExpiredHeldContract check only
// re-evaluates on the NEXT live-tick or 5s book-poll cadence — right after
// a cold-start cache lands, an expired-but-held leg could read the WRONG
// (spot-valued, drifting) branch for up to that whole window, while
// derivatives/+page.svelte's candidatePositions ALREADY re-derives
// immediately (`void instrumentsReady` in its own $derived). Subscribing
// here closes that gap so both surfaces re-classify at the same moment
// the cache actually becomes available, not just eventually.
if (browser) {
  instrumentsCacheVersion.subscribe(() => {
    if (_tickTimer) return;
    _tickTimer = setTimeout(() => { _tickTimer = null; _tick++; }, 250);
  });
}

// ── SWR last-known snapshot ──────────────────────────────────────────────────
/** @type {{ positions: any, holdings: any, funds: any }|null} */
let _last = null;

// ── prev_mv-null diagnostic — warn once per distinct condition ───────────────
//
// 2026-10 perf/noise fix (Defect 2 follow-up): `_posTier2` recomputes at
// up to 4Hz (on every relevant SSE tick), so a position that genuinely has
// no resolvable prev_mv (bad/missing broker data) warned on EVERY tick —
// console spam, not a diagnostic signal. Dedup key is (sym, prev_close, oq):
// if the SAME broker-reported condition recurs tick after tick, warn once;
// if the underlying values change (e.g. the position's prev_close gets
// backfilled, or oq rolls to a new session), a fresh warning fires for the
// new state.
const _prevMvWarnedKeys = new Set();
/**
 * @param {string} sym
 * @param {number | null | undefined} prevClose
 * @param {number} oq
 * @param {(...args: any[]) => void} [warner]  injectable for testing
 */
export function warnPrevMvNullOnce(sym, prevClose, oq, warner = console.warn) {
  const key = `${sym}|${prevClose}|${oq}`;
  if (_prevMvWarnedKeys.has(key)) return false;
  _prevMvWarnedKeys.add(key);
  warner('[portfolioStore] prev_mv null:', sym, 'prev_close=', prevClose, 'oq=', oq);
  return true;
}

// ── Root spot cache ──────────────────────────────────────────────────────────

/** Shared empty-cache constant — returned (not a fresh `{}`) when there are
 *  no position rows, so repeated no-position ticks also stay reference-stable. */
const _EMPTY_ROOT_SPOT_CACHE = /** @type {Record<string, number>} */ ({});

/**
 * Pure builder for the per-root underlying-spot cache. Extracted from the
 * `_rootSpotCache` $derived.by body so (a) the reference-stability logic
 * is unit-testable (portfolioStore.svelte.js has top-level $state/$derived
 * calls and can't be imported directly by this project's Vitest config —
 * see the R4a header note on the source-grep tests below; a hand-mirrored
 * copy of this function is what's actually exercised in
 * portfolioStore.test.js) and (b) it's reusable without duplicating the
 * isFOSymbol/decompose wiring.
 *
 * 2026-10 perf fix (Defect 2): `_tick` bumps at 4Hz on ANY symbol's SSE
 * tick, not just roots this cache cares about. Before this fix, every
 * bump rebuilt a BRAND-NEW `{}` object even when every resolved root's
 * spot value was unchanged — `_posTier2`/`_posAgg`/`_portfolio` all read
 * `_rootSpotCache` by reference (directly or transitively), so Svelte's
 * dependency tracking saw a "changed" value on every tick and recomputed
 * the whole positions chain at 4Hz regardless of relevance. Returning the
 * SAME `prev` reference when nothing actually changed lets Svelte's
 * equality check skip invalidating those downstream deriveds.
 *
 * @param {any[] | null | undefined} posRows
 * @param {(sym: string) => boolean} isFO
 * @param {(sym: string) => { root: string | null }} decompose
 * @param {(root: string) => number} getSpot
 * @param {Record<string, number>} prev
 * @returns {Record<string, number>}
 */
export function computeRootSpotCache(posRows, isFO, decompose, getSpot, prev) {
  if (!posRows) return _EMPTY_ROOT_SPOT_CACHE;
  const next = /** @type {Record<string, number>} */ ({});
  for (const p of posRows) {
    const sym  = String(p?.tradingsymbol || p?.symbol || '').toUpperCase();
    // 2026-09 R4 post-ship audit fix: was `FO_EXCHS.has(exch)` (the old
    // exchange-set gate Commit 7 replaced everywhere else with the shared
    // `isFOSymbol` predicate) — a Groww-sourced F&O row whose adapter
    // passes `exchange` through unchanged (e.g. reporting 'NSE' for an
    // NFO contract) was excluded from this root-spot cache even though
    // Commit 7 already fixed the SAME row's `_isFO` classification two
    // tiers downstream (`_posTier2`) to use `isFOSymbol` instead —
    // completing what Commit 7 was meant to do everywhere in this file.
    if (!isFO(sym) || !sym) continue;
    const root = (decompose(sym).root || sym).toUpperCase();
    if (root && !(root in next)) {
      const live = getSpot(root);
      next[root] = live > 0 ? live : (Number(p?.underlying_ltp) || 0);
    }
  }
  // Reference-stability check (Defect 2 fix): if `next` has exactly the
  // same keys and values as `prev`, discard `next` and return `prev` so
  // the caller's $derived.by sees an UNCHANGED reference.
  const prevKeys = Object.keys(prev || {});
  const nextKeys = Object.keys(next);
  if (prevKeys.length === nextKeys.length) {
    let identical = true;
    for (const k of nextKeys) {
      if (prev[k] !== next[k]) { identical = false; break; }
    }
    if (identical) return prev;
  }
  return next;
}

let _rootSpotCacheLast = /** @type {Record<string, number>} */ (_EMPTY_ROOT_SPOT_CACHE);
const _rootSpotCache = $derived.by(() => {
  void _tick;
  const posRows = positionsStore.value;
  _rootSpotCacheLast = computeRootSpotCache(
    posRows,
    isFOSymbol,
    decomposeSymbol,
    (root) => untrack(() => getUnderlyingSpot(root)),
    _rootSpotCacheLast,
  );
  return _rootSpotCacheLast;
});

// ── Tier 1 — raw + LTP ───────────────────────────────────────────────────────
const _posTier1 = $derived.by(() => {
  const posRows = positionsStore.value;
  if (!posRows) return null;
  return posRows.map(p => {
    const sym  = String(p?.tradingsymbol || p?.symbol || '').toUpperCase();
    return {
      ...p,
      _sym:        sym,
      // Poll-only LTP — positions/holdings Day P&L is purely poll-driven
      // (5s/30s book-poll cadence); no symbolStore/liveSnap tick read here.
      // Roots/underlyings (via _rootSpotCache below) remain tick-driven.
      _ltp:        Number(p?.last_price ?? 0),
      _prev_close: Number(p?.prev_close) || null,
      _qty:        Number(p?.quantity ?? 0),
      _avg:        Number(p?.average_price ?? 0),
      _pnl:        Number(p?.pnl ?? 0),
      _exch:       String(p?.exchange || '').toUpperCase(),
    };
  });
});

// ── Tier 2 — prev_mv, day_pnl, F&O exp_pnl ──────────────────────────────────
const _posTier2 = $derived.by(() => {
  if (!_posTier1) return null;
  return _posTier1.map(p => {
    // 2026-09 Commit 7 fix (R4 follow-up completes it — see _rootSpotCache
    // above, now also on isFOSymbol): exchange-independent symbol
    // predicate, shared with derivatives/pageLoad.js's page-level gate —
    // a Groww-sourced F&O row whose adapter passes `exchange` through
    // unchanged (e.g. reporting 'NSE' for an NFO contract) used to be
    // excluded here while the page's own regex-based gate still included
    // it.
    const isFO = isFOSymbol(p._sym);
    // Poll-only — no live-tick delta (§1: positions Day P&L is purely
    // poll-driven; see baseDayPnlForPosition's baseline-diff formula).
    const day_pnl = baseDayPnlForPosition(p);
    // prev_mv: prev_close × |qty| for overnight positions.
    // For new intraday positions (oq=0, no prior session close), fall back to
    // avg_cost so chg_pct has a valid denominator (day_pnl / avg_cost × 100).
    // Overnight positions with prev_close=0 remain null — avg_cost ≠ prior close.
    const oq = Number(p?.overnight_quantity ?? 0);
    const prev_mv =
      p._prev_close != null && p._prev_close > 0 ? p._prev_close * Math.abs(p._qty)
      : oq === 0 && p._avg > 0                    ? p._avg       * Math.abs(p._qty)
      : null;
    if (prev_mv === null && oq !== 0)
      warnPrevMvNullOnce(p._sym, p._prev_close, oq);

    let exp_pnl = null, extrinsic = null;
    // Per-piece Exp P&L (2026-09 Commit 5) — parallel to `exp_pnl`
    // (the pre-summed total), one entry per `splitClosedReopened` output
    // piece in order. Lets a page-level consumer that splits the SAME raw
    // row via the SAME pure function (derivatives/pageLoad.js's
    // buildPagePositionRows) zip its own split display rows against these
    // values by INDEX, not by (account,symbol) — Kite can report the same
    // symbol under the same account across two product types (NRML/MIS),
    // which collide on that key but split differently. null when the row
    // isn't F&O, or when a still-open piece has no anchor yet.
    let exp_pnl_pieces = null;
    if (isFO) {
      const decomp = decomposeSymbol(p._sym);
      const root   = (decomp.root || p._sym).toUpperCase();
      // Live front-month root spot is primary (tick-driven — the Exp P&L
      // SSOT for options); the polled `underlying_ltp` field is only a
      // fallback for the brief window before the first live root spot
      // lands. Reversed from the prior priority (polled-first) so Snapshot
      // Exp P&L / Legs TOTAL converge on the same source.
      const spot   = _rootSpotCache[root] || Number(p?.underlying_ltp || 0) || 0;
      const isCE   = p._sym.endsWith('CE'), isPE = p._sym.endsWith('PE');
      const isOpt  = isCE || isPE;
      const kind   = isOpt ? 'opt' : 'fut';
      if (p._qty !== 0) {
        // Expired-but-held check FIRST (2026-09 GOLD/GOLDM fix) — a
        // contract whose expiry has already passed (or has vanished from
        // the instruments master entirely) but the broker hasn't squared
        // it off yet must NOT be valued against `spot` (_rootSpotCache[root]
        // rolls FORWARD to the CURRENT front-month contract via
        // findNearestFuture — an economically unrelated instrument for an
        // MCX root like GOLD/GOLDM). Its fate is already sealed at
        // whatever it actually settled at; use the frozen basis instead.
        // Same condition derivatives/pageLoad.js's buildCandidatePositions
        // tags `_expired: true` for — this store carries no such tag of
        // its own, so it re-derives the identical condition via the SAME
        // shared predicate (expiryPnl.js) rather than a parallel check
        // that could silently drift from the page's.
        if (isExpiredHeldContract(p._sym, p._qty)) {
          exp_pnl = expiredPositionExpPnl(p);
          exp_pnl_pieces = expiredPositionExpPnlPieces(p);
          // No time value remains on an expired-and-settled contract.
          extrinsic = isOpt ? 0 : null;
        } else {
        // Options value at the front-month root spot (matches the Exp P&L
        // column tooltip / Snapshot / Legs TOTAL). Futures value at THEIR
        // OWN contract's live price — a future's Exp P&L only equals the
        // root's front-month spot when the held contract IS the front-month
        // future; a far-month future must be valued on its own tick, not
        // the root's front-month resolution. liveSnap() is called directly
        // inside this $derived — safe per CLAUDE.md's reactive-safety rule
        // (it internally tracks snapTick and untrack-reads the map).
        const futLive = isOpt ? 0 : Number(liveSnap(p._sym)?.ltp || 0);
        const anchor = resolveExpiryAnchor({ isOpt, rootSpot: spot, ownLiveLtp: futLive, ownPolledLtp: p._ltp || 0 });
        if (anchor > 0) {
          // SSOT fix (2026-09): positionExpPnl runs the SAME
          // splitClosedReopened-based derivation the derivatives Snapshot
          // grid uses (now shared in expiryPnl.js) over this RAW row before
          // valuing it, so a same-day partial/full close's realised P&L is
          // derived precisely from entry/exit price math instead of
          // trusting the broker's raw `realised` field — which Kite
          // documents (PULSE_SPEC.md) as unreliable on settlement (ships
          // `realised: 0` alongside a real `pnl`). This is what makes
          // NavStrip's Exp P&L total agree with Snapshot's TOTAL by
          // construction (previously the two surfaces derived the realised
          // component independently and could diverge — confirmed audit
          // finding).
          exp_pnl = positionExpPnl(p, kind, anchor);
          exp_pnl_pieces = positionExpPnlPieces(p, kind, anchor);
          // Extrinsic (§7 + item-2 fix): delegates to the shared
          // legExtrinsicDisplay (expiryPnl.js) — the single implementation
          // also used by derivatives/+page.svelte's Snapshot/Legs Extrinsic
          // cells, so both surfaces stay identical by construction. Not
          // split-aware (and doesn't need to be): a closed piece always
          // contributes 0 (qty=0 guard), and the open piece's qty/avg_cost/
          // ltp are identical to this unsplit row's — same as before. That
          // helper (a) evaluates BOTH terms of the subtraction on the SAME
          // poll-time snapshot (c.ltp for MTM, the underlying's poll-time
          // spot — `p?.underlying_ltp` — for the exp-P&L term), never the
          // live-tick `anchor` above, closing the tick-vs-poll skew bug
          // (confirmed: ~₹2,000 phantom extrinsic on a CRUDEOIL future from
          // an unrelated spot tick landing between polls); (b) returns
          // `null` outright for futures — extrinsic ("time value") is an
          // options-only concept, not tautologically 0 for a linear
          // instrument valued at its own price.
          const cRow = { symbol: p._sym, qty: p._qty, avg_cost: p._avg, ltp: p._ltp, kind };
          const pollAnchor = isOpt ? (Number(p?.underlying_ltp || 0) || 0) : 0;
          extrinsic = legExtrinsicDisplay(cRow, pollAnchor);
        }
        }
      } else {
        // Fully closed (no remaining qty) — positionExpPnl still runs the
        // row through splitClosedReopened (a harmless no-op when there was
        // no today's buy/sell activity) and applies the both-zero-fields-
        // fall-back-to-pnl convention on its qty=0 branch (see
        // expiryPnl.js:expiryPnlWithRealised) instead of trusting a
        // present-but-zero `realised` field.
        exp_pnl = positionExpPnl(p, kind, null);
        exp_pnl_pieces = positionExpPnlPieces(p, kind, null);
        // §7: closed futures have no time-value concept either — only
        // closed options settle to a well-defined "no time value left" 0.
        extrinsic = isOpt ? 0 : null;
      }
    }

    return { ...p, _day_pnl: day_pnl, _prev_mv: prev_mv, _isFO: isFO, _exp_pnl: exp_pnl, _exp_pnl_pieces: exp_pnl_pieces, _extrinsic: extrinsic };
  });
});

// ── Tier 3 — chg_pct, root ───────────────────────────────────────────────────
const _posTier3 = $derived(
  _posTier2?.map(p => ({
    ...p,
    _chg_pct: p._prev_mv != null && p._prev_mv > 0
      ? p._day_pnl / p._prev_mv * 100 : null,
    _root: p._isFO
      ? (decomposeSymbol(p._sym).root || p._sym).toUpperCase()
      : p._sym,
  })) ?? null
);

// ── Position aggregates ───────────────────────────────────────────────────────
const _posAgg = $derived.by(() => {
  if (!_posTier3) return null;
  const posTotal      = { day_pnl: 0, exp_pnl: 0, extrinsic: 0, prev_mv: 0, chg_pct: null };
  const posByKey      = {};
  const posByAccount  = {};
  const byRootPos     = {};
  const byRoot        = {};
  const expiryByAcct  = new Map();
  // Per-row F&O Exp P&L/Extrinsic entries — the SSOT the derivatives
  // Snapshot grid's account/strategy-filtered totals read from (2026-09 SSOT
  // fix) instead of independently recomputing exp_pnl per leg. One entry per
  // raw position row (not per split closed/open piece — positionExpPnl
  // already sums the pieces into a single per-row value, so this is the
  // same granularity `positions` (the derivatives page's own per-row array)
  // exposes to its own per-root reduction).
  const expPnlRows = [];

  for (const p of _posTier3) {
    if (!posByKey[p._sym]) posByKey[p._sym] = { day_pnl: 0, exp_pnl: null, extrinsic: null, pnl: 0, prev_mv: 0, chg_pct: null };
    const bk = posByKey[p._sym];
    bk.day_pnl += p._day_pnl;
    bk.pnl     += p._pnl;
    bk.prev_mv += p._prev_mv ?? 0;
    if (p._exp_pnl   != null) bk.exp_pnl   = (bk.exp_pnl   ?? 0) + p._exp_pnl;
    if (p._extrinsic != null) bk.extrinsic = (bk.extrinsic ?? 0) + p._extrinsic;

    posTotal.day_pnl += p._day_pnl;
    posTotal.prev_mv += p._prev_mv ?? 0;

    const _acct = String(p.account || '').toUpperCase();
    if (_acct) posByAccount[_acct] = (posByAccount[_acct] ?? 0) + p._day_pnl;
    if (p._exp_pnl   != null) posTotal.exp_pnl   += p._exp_pnl;
    if (p._extrinsic != null) posTotal.extrinsic += p._extrinsic;

    if (p._isFO) {
      const r = p._root;
      if (r) {
        if (!byRoot[r]) byRoot[r] = { spot: _rootSpotCache[r] || 0, legs: [], day_pnl: 0, exp_pnl: 0, extrinsic: 0, prev_mv: 0, chg_pct: null };
        byRoot[r].legs.push(p._sym);
        byRoot[r].day_pnl   += p._day_pnl;
        if (p._exp_pnl   != null) byRoot[r].exp_pnl   += p._exp_pnl;
        if (p._extrinsic != null) byRoot[r].extrinsic += p._extrinsic;
        byRoot[r].prev_mv   += p._prev_mv ?? 0;

        byRootPos[r] ??= { day_pnl: 0, exp_pnl: 0, extrinsic: 0, pnl: 0, prev_mv: 0, chg_pct: null };
        byRootPos[r].day_pnl   += p._day_pnl;
        byRootPos[r].pnl       += p._pnl;
        byRootPos[r].exp_pnl   += p._exp_pnl ?? 0;
        byRootPos[r].extrinsic += p._extrinsic ?? 0;
        byRootPos[r].prev_mv   += p._prev_mv ?? 0;

        if (p._exp_pnl != null) {
          // 2026-09 Commit 9 fix: uppercase, matching `_acct` (used two
          // lines below for posByAccount/expPnlRows) and every other
          // account key in this file — the raw (unuppercased) `p.account`
          // here could silently split one account's total across two Map
          // keys (e.g. "zg0790" vs "ZG0790") if the broker ever returns
          // mixed casing.
          if (_acct) expiryByAcct.set(_acct, (expiryByAcct.get(_acct) ?? 0) + p._exp_pnl);
        }

        expPnlRows.push({
          account:    _acct,
          symbol:     p._sym,
          root:       r,
          source:     'live',
          exp_pnl:    p._exp_pnl,
          extrinsic:  p._extrinsic,
        });
      }
    }
  }

  for (const bk of Object.values(posByKey)) {
    bk.chg_pct = bk.prev_mv > 0 ? dayChangePct(bk.day_pnl, bk.prev_mv) : null;
  }
  posTotal.chg_pct = posTotal.prev_mv > 0 ? dayChangePct(posTotal.day_pnl, posTotal.prev_mv) : null;
  for (const r of Object.values(byRoot)) {
    r.chg_pct = r.prev_mv > 0 ? dayChangePct(r.day_pnl, r.prev_mv) : null;
  }
  for (const r of Object.values(byRootPos)) {
    r.chg_pct = r.prev_mv > 0 ? dayChangePct(r.day_pnl, r.prev_mv) : null;
  }

  posByAccount['TOTAL'] = posTotal.day_pnl;
  // `rows` (2026-09 Commit 2 completion): the raw, UNSPLIT per-position
  // array (_posTier3) — one row per broker-consolidated position, NOT one
  // per closed/open display piece (that split is a page-level display
  // concern, done by derivatives/pageLoad.js's splitClosedReopened, not
  // here). Exposed so +page.svelte's own `positions` array can be rebuilt
  // from this single source instead of independently re-deriving from
  // positionsStore.value — see buildPagePositionRows (pageLoad.js).
  return { posTotal, posByKey, posByAccount, byRoot, byRootPos, expiryByAcct, expPnlRows, rows: _posTier3 };
});

// ── Holdings tiers ────────────────────────────────────────────────────────────
const _holdTier1 = $derived.by(() => {
  const holdRows = pulseHoldingsStore.value;
  if (!holdRows) return null;
  return holdRows.map(h => {
    const sym  = String(h?.tradingsymbol || h?.symbol || '').toUpperCase();
    return {
      ...h,
      _sym:        sym,
      _prev_close: Number(h?.prev_close) || null,
      _held_qty:   Number(h?.quantity ?? 0),
      // Poll-only LTP — see _posTier1's matching comment (§1).
      _ltp:        Number(h?.last_price ?? 0),
      _dcv:        Number(h?.day_change_val) || 0,
    };
  });
});

const _holdTier2 = $derived(
  _holdTier1?.map(h => {
    const prev_mv = h._prev_close > 0 ? h._prev_close * Math.abs(h._held_qty) : null;
    let day_pnl;
    if (!h._prev_close || h._prev_close <= 0) {
      day_pnl = h._dcv;
    } else if (h._ltp > 0 && h._held_qty !== 0 && Math.abs(h._ltp - h._prev_close) > 0.005) {
      day_pnl = (h._ltp - h._prev_close) * h._held_qty;
    } else {
      day_pnl = h._dcv;
    }
    return { ...h, _prev_mv: prev_mv, _day_pnl: day_pnl };
  }) ?? null
);

const _holdTier3 = $derived(
  _holdTier2?.map(h => ({
    ...h,
    _chg_pct: h._prev_mv != null && h._prev_mv > 0
      ? h._day_pnl / h._prev_mv * 100 : null,
  })) ?? null
);

const _holdAgg = $derived.by(() => {
  if (!_holdTier3) return null;
  let total = 0, prev_mv = 0;
  const byKey = {}, byAccount = {}, chgPctByKey = {};
  for (const h of _holdTier3) {
    total   += h._day_pnl ?? 0;
    prev_mv += h._prev_mv ?? 0;
    byKey[h._sym] = (byKey[h._sym] ?? 0) + (h._day_pnl ?? 0);
    chgPctByKey[h._sym] = h._chg_pct;
    const acct = String(h?.account || '').toUpperCase();
    if (acct) byAccount[acct] = (byAccount[acct] ?? 0) + (h._day_pnl ?? 0);
  }
  byAccount['TOTAL'] = total;
  // `rows` (2026-09 Commit 6): the raw per-holding array (_holdTier3),
  // already carrying the canonical per-holding Day P&L formula
  // ((ltp − prev_close) × qty, falling back to the broker's own
  // day_change_val when prev_close is unusable — see _holdTier2) — the
  // SSOT +page.svelte's own equity/proxy Day P&L cell now reads directly
  // instead of reimplementing the same formula independently.
  return { total, prev_mv, chg_pct: prev_mv > 0 ? dayChangePct(total, prev_mv) : null, byKey, chgPctByKey, byAccount, rows: _holdTier3 };
});

// ── Cross-hedge byRootHoldings ─────────────────────────────────────────────
const _byRootHoldings = $derived.by(() => {
  if (!_holdTier1) return {};
  const map = {};
  for (const h of _holdTier1) {
    if (!h._held_qty || h._held_qty === 0) continue;
    const targets = targetsForProxy(h._sym);
    const credits = targets.length ? targets : [h._sym];
    for (const target of credits) {
      let exp_pnl = null;
      if (targets.length && h._ltp > 0) {
        const proxyRow   = getProxyRow(h._sym, target);
        const beta       = proxyRow?.beta ?? 1;
        // untrack: getUnderlyingSpot() now reads symbolStore via getSnapshot()
        // internally (front-month resolution) — CLAUDE.md's reactive-safety
        // rule requires wrapping any getSnapshot()-backed read inside a
        // $derived, same as the sibling read at line ~67 already does.
        const targetSpot = untrack(() => getUnderlyingSpot(target));
        if (targetSpot > 0) {
          const effQty = (beta * h._ltp * h._held_qty) / targetSpot;
          exp_pnl = (targetSpot - h._ltp / (beta || 1)) * effQty;
        }
      } else if (!targets.length && h._ltp > 0) {
        exp_pnl = (h._ltp - Number(h?.average_price ?? h?.avg_cost ?? 0)) * h._held_qty;
      }
      const r = map[target] ??= { day_pnl: 0, exp_pnl: 0, extrinsic: 0, pnl: 0 };
      r.pnl += Number(h?.pnl ?? 0);
      if (exp_pnl != null) r.exp_pnl += exp_pnl;
    }
  }
  return map;
});

// ── Funds aggregate ────────────────────────────────────────────────────────
const _fundsAgg = $derived.by(() => {
  const fundRows = fundsStore.value;
  if (!fundRows) return null;
  const fundsResult = {
    total: { live_cash: 0, avail_margin: 0, used_margin: 0, totalMargin: 0, utilPct: 0, collateral: 0 },
    byAccount: /** @type {Record<string,any>} */ ({}),
  };
  for (const f of fundRows) {
    const acct = String(f?.account || '').toUpperCase();
    if (!acct || acct === 'TOTAL') continue;
    const live_cash    = Number(f?.live_cash    ?? f?.cash       ?? 0);
    const avail_margin = Number(f?.avail_margin ?? 0);
    const used_margin  = Number(f?.used_margin  ?? 0);
    const collateral   = Number(f?.collateral   ?? 0);
    const totalMargin  = used_margin + avail_margin;
    const utilPct      = totalMargin > 0 ? (used_margin / totalMargin) * 100 : 0;
    fundsResult.byAccount[acct] = { live_cash, avail_margin, used_margin, collateral, totalMargin, utilPct };
    fundsResult.total.live_cash    += live_cash;
    fundsResult.total.avail_margin += avail_margin;
    fundsResult.total.used_margin  += used_margin;
    fundsResult.total.collateral   += collateral;
  }
  fundsResult.total.totalMargin = fundsResult.total.used_margin + fundsResult.total.avail_margin;
  fundsResult.total.utilPct = fundsResult.total.totalMargin > 0
    ? (fundsResult.total.used_margin / fundsResult.total.totalMargin) * 100 : 0;
  return fundsResult;
});

// ── Default shapes returned when deps are null (SWR fallback) ────────────────
const _EMPTY_POSITIONS = {
  total:          { day_pnl: 0, exp_pnl: 0, extrinsic: 0, prev_mv: 0, chg_pct: null },
  byKey:          {},
  byAccount:      {},
  byRootPositions:{},
  byRootHoldings: {},
  byRoot:         {},
  expiryByAcct:   new Map(),
  expPnlRows:     [],
  rows:           [],
  fresh:          false,
};
const _EMPTY_HOLDINGS = { total: 0, byKey: {}, byAccount: {}, chg_pct: null, chgPctByKey: {}, rows: [], fresh: false };
const _EMPTY_FUNDS    = { total: { live_cash: 0, avail_margin: 0, used_margin: 0, totalMargin: 0, utilPct: 0, collateral: 0 }, byAccount: {} };

// ── Final collector ────────────────────────────────────────────────────────
// Partial fallback: as soon as ANY of positions/holdings/funds has landed,
// build a snapshot using that fresh slice + the last-known (or empty) value
// for whichever slice(s) haven't arrived yet — rather than blocking P:1/H:1
// display on the slowest of three independent fetches. Only when NONE has
// ever landed do we fall through to the previous full snapshot (or null on
// first paint, handled by the exported getters' `?? _EMPTY_*` fallback).
//
// Real-money guard (2026-09): a slice is ALSO treated as "not landed" when
// its source store is tagged degraded (positionsStore.meta.degraded /
// pulseHoldingsStore.meta.degraded / fundsStore.meta.degraded — see
// marketDataStores.svelte.js's _bookStaleMeta + dataStore.svelte.js's
// extractStaleMeta). Without this, a partially-degraded response (some
// accounts substituted, others empty) would still produce a non-null
// _posAgg/_holdAgg/_fundsAgg computed off the DEGRADED rows — silently
// under-counting the stale account's contribution instead of freezing at
// the last known-good full snapshot. Each slice degrades independently:
// a degraded positions fetch doesn't hold back a healthy holdings/funds
// update.
const _portfolio = $derived.by(() => {
  const posDegraded   = positionsStore.meta?.degraded === true;
  const holdDegraded  = pulseHoldingsStore.meta?.degraded === true;
  const fundsDegraded = fundsStore.meta?.degraded === true;
  const posFresh   = _posAgg   && !posDegraded;
  const holdFresh  = _holdAgg  && !holdDegraded;
  const fundsFresh = _fundsAgg && !fundsDegraded;
  if (!posFresh && !holdFresh && !fundsFresh) {
    // 2026-09 Commit 2 completion (advisor-reviewed fix): this is the
    // simultaneous-outage path (all three degraded at once — the common
    // case when the conn-service itself goes down, not a rare edge case)
    // — `_last` is returned WITHOUT a rebuild, so `_last.positions.fresh`
    // would otherwise still carry whatever `true` was baked in during the
    // last live cycle, forever, since this branch never touches it again.
    // `fresh` is documented public API (portfolioStore.positions.fresh)
    // that consumers gate real decisions on (+page.svelte's
    // `_positionsRefreshedAt` stamp) — it must never report stale-true on
    // a confirmed-degraded read. Force it false here explicitly rather
    // than relying on some earlier cycle having already done so.
    // (2026-09 Commit 6): holdings.fresh gets the identical treatment.
    if (_last?.positions?.fresh || _last?.holdings?.fresh) {
      _last = {
        ..._last,
        positions: _last.positions ? { ..._last.positions, fresh: false } : _last.positions,
        holdings:  _last.holdings  ? { ..._last.holdings,  fresh: false } : _last.holdings,
      };
    }
    return _last;
  }
  _last = {
    positions: {
      // Spread either the freshly-recomputed slice OR the previous frozen
      // one — either way `fresh` below is overwritten to reflect THIS
      // cycle's actual posFresh, never whatever was baked in when the
      // frozen object was last (re)built. Baking `fresh` INSIDE the
      // freshly-computed branch only (the first version of this fix) was
      // itself a bug: the frozen copy would keep echoing `fresh: true`
      // from its last live cycle forever, since it's never rebuilt while
      // frozen.
      ...(posFresh ? {
        total:           _posAgg.posTotal,
        byKey:           _posAgg.posByKey,
        byAccount:       _posAgg.posByAccount,
        byRoot:          _posAgg.byRoot,
        byRootPositions: _posAgg.byRootPos,
        byRootHoldings:  _byRootHoldings,
        expiryByAcct:    _posAgg.expiryByAcct,
        expPnlRows:      _posAgg.expPnlRows,
        rows:            _posAgg.rows,
      } : (_last?.positions ?? _EMPTY_POSITIONS)),
      // `fresh` (2026-09 Commit 2 completion): true only on the cycle that
      // actually recomputed `rows` from a live, non-degraded fetch — false
      // on every frozen (degraded / not-yet-landed) cycle, even though
      // `rows` itself still returns the last-known-good array. +page.svelte's
      // book-poller propagation effect gates its `_positionsRefreshedAt`
      // stamp on this flag (NOT on `rows` being non-empty) so a frozen
      // read is never mistaken for "confirmed fresh" — the exact hazard
      // the page's own now-removed `_hasLiveRows`/masked-empty guard used
      // to cover locally.
      fresh: posFresh,
    },
    // holdings.fresh (2026-09 Commit 6): same spread + explicit-override
    // pattern as positions.fresh above, for the identical reason — a
    // frozen (degraded) holdings read must never echo a stale `fresh:
    // true` from its last live cycle.
    holdings: {
      ...(holdFresh ? _holdAgg : (_last?.holdings ?? _EMPTY_HOLDINGS)),
      fresh: holdFresh,
    },
    funds:    fundsFresh ? _fundsAgg  : (_last?.funds    ?? _EMPTY_FUNDS),
  };
  return _last;
});

/**
 * Unified portfolio store.
 *
 * All three sections are derived from a chained $derived tier structure with a
 * stale-while-revalidating (SWR) null-guard so consumers never see momentary
 * zero-out during a 5-second poll refresh.
 */
export const portfolioStore = {
  // ── Positions ────────────────────────────────────────────────────────────
  /**
   * { total: {day_pnl,exp_pnl,extrinsic,prev_mv,chg_pct}, byKey, byRootPositions, byRootHoldings, byRoot, expiryByAcct, expPnlRows, rows, fresh }
   *
   * `expPnlRows` (2026-09 SSOT fix): one entry per LIVE F&O position row —
   * { account, symbol, root, source:'live', exp_pnl, extrinsic } — each
   * `exp_pnl` already derived via the split-aware `positionExpPnl` helper
   * (expiryPnl.js), the SAME precise partial/full-close realised-P&L
   * derivation the derivatives Snapshot grid uses. Consumers that need a
   * FILTERED (account/strategy/search) Exp P&L rollup — e.g. the Snapshot
   * grid — should reduce over this array (matchAccount/matchStrategy +
   * group by `root`) instead of recomputing exp_pnl per leg themselves.
   * `total.exp_pnl` (unfiltered, whole book) is the sum of every row here.
   * Sim positions are NOT included (this store only reflects the live
   * broker book) — sim-mode consumers must compute their own via the same
   * `positionExpPnl` pure function.
   *
   * `rows` (2026-09 Commit 2 completion — single row-source unification):
   * the raw, UNSPLIT per-position array (spread of the raw broker fields
   * PLUS portfolioStore's own `_`-prefixed derived fields) — one row per
   * broker-consolidated position, F&O AND equity mixed, NOT one per
   * closed/open display piece. This is the SSOT the derivatives page's own
   * `positions` $state is now built from (via
   * `derivatives/pageLoad.js:buildPagePositionRows`, which applies the
   * F&O filter + `splitClosedReopened` display-split on top), replacing
   * the page's former independent `positionsStore.value` +
   * `pulsePositionsStore` fallback.
   *
   * `fresh` — true only on a cycle that actually recomputed `rows`/`total`/
   * etc. from a live, non-degraded fetch; false on every frozen
   * (degraded / not-yet-landed) cycle, even though every other field above
   * still returns the last-known-good snapshot rather than collapsing to
   * empty. Consumers that need to distinguish "these rows are frozen, do
   * not treat this poll as confirmed-fresh" (e.g. a `_positionsRefreshedAt`
   * stamp gating a strategy-wipe decision) should read this flag instead
   * of inferring freshness from `rows.length`.
   */
  get positions() { return _portfolio?.positions ?? _EMPTY_POSITIONS; },

  // ── Holdings ──────────────────────────────────────────────────────────────
  /**
   * { total: number, byKey: Record<string,number>, byAccount: Record<string,number>, chg_pct: number|null, chgPctByKey: Record<string,number|null>, rows, fresh }
   *
   * `rows` (2026-09 Commit 6): the raw per-holding array (`_holdTier3`),
   * one entry per holding, already carrying the canonical per-holding Day
   * P&L formula ((ltp − prev_close) × qty, `_dcv` fallback when
   * prev_close is unusable) as `_day_pnl` — the SSOT
   * derivatives/pageLoad.js's page-level equity/proxy Day P&L consumers
   * read directly instead of reimplementing the formula.
   * `fresh` — same freeze semantics as `positions.fresh`: true only on a
   * cycle that recomputed from a live, non-degraded fetch.
   *
   * 2026-09 Commit 9: the pulse-override mechanism (`setHoldingsFromPulse`,
   * called by MarketPulse after each buildUnified pass) was removed — grep
   * confirmed MarketPulse.svelte had ALREADY stopped calling it in an
   * earlier session ("setFromPulse() override is removed — NavStrip P
   * reads the store directly" — its own comment), leaving the override
   * state permanently null/dead weight here. NavStrip now always reads
   * this base computation directly, unconditionally.
   */
  get holdings() { return _portfolio?.holdings ?? _EMPTY_HOLDINGS; },

  // ── Funds ─────────────────────────────────────────────────────────────────
  /** { total: {live_cash,avail_margin,used_margin,...}, byAccount: Record<string,{...}> } */
  get funds() { return _portfolio?.funds ?? _EMPTY_FUNDS; },
};

// ── Cross-page portfolio aggregates (moved from PositionStrip) ────────────────
// Pre-computed totals that PositionStrip reads as $derived reflectors instead
// of re-deriving inline. Each does a TRACKED (non-untrack) read of the
// underlying store's .value (positionsStore/pulseHoldingsStore/fundsStore),
// so the derived re-runs whenever the poll/fill reload actually lands — the
// store's own .value assignment already IS the poll/fill/bookChanged signal,
// so a separate `void bookPollerTick.value; void _bookChangedTick;` pair was
// redundant (round-3 audit, cleaned up round 4) — removed. `void _tick;`
// (the SSE-tick throttle) was also removed — matches §1's poll-only
// unification for positions/holdings (Day P&L already went poll-only; these
// lifetime/value aggregates now consistently follow the same cadence rather
// than being the one remaining tick-reactive exception). Two of these
// (_liveHoldingsTotal/_liveHoldingsValue) still read the CURRENT live
// getSnapshot()/liveSnap() value at each poll-triggered recompute — they
// just no longer force a re-render on every intermediate SSE tick between
// polls, matching every sibling position/holding aggregate. Only
// `_rootSpotCache` above (root/underlying spot, intentionally still
// tick-driven per §1's carve-out) keeps its own `void _tick;`. Only
// per-symbol getSnapshot()/liveSnap() reads (raw Map lookups, not $state
// themselves) are wrapped in untrack(), per CLAUDE.md's reactive-safety
// rule.

// Real-money guard (2026-09) — last-good scalar snapshots for the
// portfolioAggregates getters below. Mirrors the `_last` object pattern
// already used by `_portfolio` above: a plain module-level variable
// (not $state) mutated inside each $derived.by, so a null/degraded read
// freezes at the last successfully-computed value instead of collapsing
// to 0. Addresses A4/R3: dataStore's softInvalidate()/invalidate() (and
// any transient null window between polls) used to null the underlying
// store value, and every one of these getters returned a bare 0 with no
// stale-while-revalidate guard — lifetime P&L, holdings value, cash,
// margin would all flash to 0 until the next poll landed.
let _lastLivePositionsPnl    = 0;
let _lastLiveHoldingsTotal   = 0;
let _lastLiveHoldingsValue   = 0;
let _lastLongOptionsCashPaid = 0;
// _lastLiveCashTotal / _lastMarginAvail / _lastMarginTotal (real-money
// fix, 2026-09 NavStrip "₹0 margin for an extended period" incident):
// REMOVED. The old freeze-to-last-remembered-scalar pattern held the
// ENTIRE cross-account total hostage to ONE degraded account (common —
// a single flaky Dhan/Groww account), and on a fresh page load these
// scalars reset to 0, so the FIRST poll(s) landing while degraded
// produced a flat 0 that never recovered until a non-degraded poll
// arrived. _marginAvail / _marginTotal / _liveCashTotal below now sum
// whatever fundRows actually holds (via fundsAggregate.js's pure
// helpers) — including stale accounts' own backend-substituted
// last-known-good values — every read, and return null only when
// fundRows itself is null/empty (no successful poll has EVER landed).
// See fundsAggregate.js's file header for the full incident writeup.

// Sum of lifetime pnl across all position rows (P pill slot 2 in NavStrip).
// Reads raw broker pnl — no live-LTP delta — matching the MarketPulse TOTAL row
// which uses _broker_pnl (= Σ r.pnl) without an SSE delta.
const _livePositionsPnl = $derived.by(() => {
  // Tracked read — positionsStore.value is a $state getter, so this
  // derived re-runs whenever the poll/fill reload actually lands (the
  // store's own .set() IS the poll/fill signal — see the file-header
  // comment above this block). Wrapping this in untrack() was the root
  // cause of stale-until-next-poll aggregates in an earlier iteration.
  const posRows = positionsStore.value;
  // Degraded (backend-tagged substituted/stale accounts) or not-yet-
  // populated (null, e.g. mid softInvalidate) — freeze at last-good
  // rather than showing 0.
  if (!posRows || positionsStore.meta?.degraded) return _lastLivePositionsPnl;
  let s = 0;
  for (const p of posRows) s += Number(p?.pnl || 0);
  _lastLivePositionsPnl = s;
  return s;
});

// Live holdings total P&L: (liveHold − avg) × qty using symbolStore LTP.
// Matches MarketPulse mergeHoldingRows which uses live-LTP when available.
// Falls back to broker h.pnl when no live LTP is present.
const _liveHoldingsTotal = $derived.by(() => {
  // Tracked read — see _livePositionsPnl's comment above.
  const holdRows = pulseHoldingsStore.value;
  if (!holdRows || pulseHoldingsStore.meta?.degraded) return _lastLiveHoldingsTotal;
  let s = 0;
  for (const h of holdRows) {
    const sym      = String(h?.tradingsymbol || '').toUpperCase();
    const liveHold = untrack(() => getSnapshot(sym)?.ltp);
    const avgCost  = Number(h?.average_price || 0);
    const qty      = Number(h?.quantity || 0);
    if (liveHold != null && liveHold > 0 && avgCost > 0 && qty !== 0) {
      s += (liveHold - avgCost) * qty;
    } else {
      s += Number(h?.pnl || 0);
    }
  }
  _lastLiveHoldingsTotal = s;
  return s;
});

// Live holdings market value: ltp × qty (with fallback tiers matching PositionStrip).
// Tier 1: symbolStore ltp × qty. Tier 2: h.last_price × qty (avoids cur_val=inv_val trap).
// Tier 3: h.cur_val (broker computed, may equal inv_val when last_price=0).
const _liveHoldingsValue = $derived.by(() => {
  // Tracked read — see _livePositionsPnl's comment above.
  const holdRows = pulseHoldingsStore.value;
  if (!holdRows || pulseHoldingsStore.meta?.degraded) return _lastLiveHoldingsValue;
  let s = 0;
  for (const h of holdRows) {
    const sym    = String(h?.tradingsymbol || '').toUpperCase();
    const ltp    = untrack(() => getSnapshot(sym)?.ltp);
    const qty    = Number(h?.quantity || 0);
    const lastPx = Number(h?.last_price || 0);
    if (ltp != null && ltp > 0 && qty !== 0) {
      s += ltp * qty;
    } else if (lastPx > 0 && qty !== 0) {
      s += lastPx * qty;
    } else {
      s += Number(h?.cur_val || 0);
    }
  }
  _lastLiveHoldingsValue = s;
  return s;
});

// Live cash: Kite avail.cash (direct funds only — NOT Kite's avail.live_balance,
// a different field that also includes collateral) summed across all accounts.
// Falls back to f.cash if live_cash is not yet surfaced by the backend.
//
// Real-money fix (2026-09): no longer freezes the WHOLE total when
// fundsStore.meta.degraded is true (one flaky account used to hold every
// healthy account's cash hostage). Sums whatever fundRows actually holds
// — see fundsAggregate.js's sumLiveCashTotal + its file-header incident
// writeup. Returns null (genuinely unknown) only when no poll has ever
// landed; PositionStrip renders that as `—`, never `₹0`.
const _liveCashTotal = $derived.by(() => {
  // Tracked read — see _livePositionsPnl's comment above.
  const fundRows = fundsStore.value;
  return sumLiveCashTotal(fundRows);
});

// Cash debited on currently-held long options.
// For each long CE/PE row: avg × lot_size × (qty / lot_size) = avg × qty.
// Using num_lots path for clarity; falls back to avg × qty if lot_size unavailable.
const _longOptionsCashPaid = $derived.by(() => {
  // Tracked read — see _livePositionsPnl's comment above.
  const posRows = positionsStore.value;
  if (!posRows || positionsStore.meta?.degraded) return _lastLongOptionsCashPaid;
  let s = 0;
  for (const p of posRows) {
    const sym = String(p?.tradingsymbol || '').toUpperCase();
    const isOpt = sym.endsWith('CE') || sym.endsWith('PE');
    const qty   = Math.abs(Number(p?.quantity) || 0);
    const avg   = Number(p?.average_price) || 0;
    if (!isOpt || Number(p?.quantity) <= 0) continue;
    const inst    = getInstrument(sym);
    const lotSize = Number(inst?.ls) || 0;
    if (lotSize > 0) {
      const numLots = qty / lotSize;
      s += avg * lotSize * numLots;
    } else {
      s += avg * qty;
    }
  }
  _lastLongOptionsCashPaid = s;
  return s;
});

// Margin available (deployable) across all accounts.
//
// Real-money fix (2026-09): see _liveCashTotal's comment above — sums
// whatever fundRows actually holds (fundsAggregate.js's sumMarginAvail)
// instead of freezing the whole total on any single degraded account.
const _marginAvail = $derived.by(() => {
  // Tracked read — see _livePositionsPnl's comment above.
  const fundRows = fundsStore.value;
  return sumMarginAvail(fundRows);
});

// Margin total (used + available = full capacity) across all accounts.
// Real-money fix (2026-09): same shape as _marginAvail above.
const _marginTotal = $derived.by(() => {
  // Tracked read — see _livePositionsPnl's comment above.
  const fundRows = fundsStore.value;
  return sumMarginTotal(fundRows);
});

/**
 * Pre-computed cross-page portfolio aggregates.
 * PositionStrip reads these as $derived reflectors instead of re-deriving inline.
 * All values gate on _tick (4Hz) — same reactive cadence as portfolioStore.
 */
export const portfolioAggregates = {
  /** Σ position.pnl — lifetime P&L total (NO live-LTP delta), matching MarketPulse TOTAL. */
  get livePositionsPnl()   { return _livePositionsPnl;    },
  /** Live holdings P&L: (ltp − avg) × qty per holding; falls back to broker h.pnl. */
  get liveHoldingsTotal()  { return _liveHoldingsTotal;   },
  /** Live holdings market value: ltp × qty (three-tier fallback). */
  get liveHoldingsValue()  { return _liveHoldingsValue;   },
  /** Available cash across all accounts (Kite avail.cash, fallback to cash).
   *  null when no funds poll has ever landed (genuinely unknown — render `—`,
   *  never `₹0`). Sums every row present, including stale/degraded accounts
   *  carrying their own last-known-good substituted values — see
   *  fundsAggregate.js's file header for the real-money fix this replaced. */
  get liveCashTotal()      { return _liveCashTotal;       },
  /** Cash paid for currently-held long options (avg × qty via lot_size path). */
  get longOptionsCashPaid(){ return _longOptionsCashPaid; },
  /** Available margin across all accounts. null when no funds poll has ever
   *  landed — see liveCashTotal's doc above for the null/degraded semantics. */
  get marginAvail()        { return _marginAvail;         },
  /** Total margin capacity (used + available) across all accounts. null when
   *  no funds poll has ever landed — see liveCashTotal's doc above. */
  get marginTotal()        { return _marginTotal;         },
};
