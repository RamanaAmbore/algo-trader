/**
 * lotPairSplit.js — frontend re-derivation of the backend's lot-waterfall
 * auto-pairing (`backend/api/routes/positions.py:_auto_pair_positions`),
 * fixing a backend bug this module does NOT touch (frontend-only task):
 * when one position row's quantity waterfall-matches against TWO OR MORE
 * different counterparty rows, the backend's loop OVERWRITES that row's
 * `pair_group_key`/`paired_qty`/`orphan_qty` on each iteration instead of
 * accumulating — the final state only remembers the LAST group, so a
 * genuinely multi-group-paired row displays as if it belonged to only one
 * group.
 *
 * `splitPairedLots(rows)` re-runs the SAME waterfall match client-side from
 * `quantity`/`qty` (whichever field the row carries — this module is called
 * both on raw `/api/positions` rows, which use `quantity`/`tradingsymbol`,
 * and on `buildPositionRowFromBroker`/`splitClosedReopened` output, which
 * uses `qty`/`symbol` — see derivatives/pageLoad.js's `buildPagePositionRows`),
 * with proper multi-group accumulation, then — ONLY for a row that spans
 * 2+ groups or has leftover unmatched quantity — splits it into multiple
 * pro-rated DISPLAY pieces so each piece can carry exactly one
 * `pair_group_key`.
 *
 * DEVIATIONS FROM THE ORIGINAL BRIEF (found + corrected during
 * implementation — see SubagentHandback report for the full reasoning):
 *
 *   1. Root-symbol grouping uses `decomposeSymbol(sym).root`
 *      (`./decomposeSymbol.js`), NOT `rootOf()` (`./rootOf.js`). `rootOf()`
 *      is a DISPLAY helper that only remaps MCX/CDS futures to a virtual
 *      front/back-month root and passes every option/equity through
 *      UNCHANGED — using it here would put every NFO option in its own
 *      one-row "group", so nothing would ever match and ~every leg would
 *      show as orphaned. `decomposeSymbol().root` strips expiry/strike/
 *      CE-PE exactly like the backend's `_root_symbol()` (e.g.
 *      "NIFTY25JUN24000CE" → "NIFTY", "CRUDEOIL26OCTFUT" and
 *      "CRUDEOIL26NOVFUT" → "CRUDEOIL" — the calendar-spread case the
 *      backend groups together), and needs no seeded instruments cache.
 *
 *   1b. Grouping key is now `(account, root, optType)`, not `(account,
 *      root)` — mirrors `backend/api/routes/positions.py:_auto_pair_positions`'s
 *      own 2026-10 fix for the identical bug (same change, same root cause,
 *      kept in parity on both sides): without the option-type dimension, a
 *      long CE and a short PE of the same root/account/magnitude waterfall-
 *      matched as if they were a real pair, even though a call and a put
 *      are economically unrelated instruments. `optType` comes from
 *      `decomposeSymbol(sym).optType` when it successfully parses the
 *      symbol as a real option/future (`kind === 'opt'|'fut'`) — which also
 *      yields the correctly-stripped `root` for that same symbol. For a
 *      symbol `decomposeSymbol` can't parse as F&O at all (no expiry month
 *      token — synthetic/abbreviated symbols only; every real Kite
 *      tradingsymbol always carries one), `_rootAndOptType()` falls back to
 *      a standalone port of the backend's own `_EXPIRY_RE`/`_root_symbol`/
 *      `_option_type` suffix-stripping regexes so the two algorithms still
 *      agree even on a symbol neither parser can fully decompose. Futures/
 *      equity rows (`optType` null either way) are unaffected — the new
 *      dimension is a no-op for them, same as the backend fix.
 *
 *   2. A row that cleanly matches into exactly ONE group, with no leftover,
 *      is NOT always returned as the same reference. This module's own
 *      rank-by-size labelling can legitimately assign a different "Pn"
 *      number than whatever the row arrived with (e.g. a 3-party group
 *      where the backend's loop-order numbering and a qty-rank numbering
 *      disagree on which pair is "P1" vs "P2") — returning such a row
 *      "unchanged" would show ITS OWN label out of sync with its matched
 *      counterpart's freshly-split label, silently re-creating a milder
 *      version of the exact bug being fixed. The identity/no-wrap fast
 *      path is therefore conditioned on the row's EXISTING `pair_group_key`
 *      / `paired_qty` / `orphan_qty` / `is_orphan` already equalling the
 *      freshly-recomputed values, not merely on "exactly one piece".
 *
 *   3. Labels are ranked PER (account, root) GROUP (qty desc, ties broken
 *      by the two matched symbols sorted alphabetically) — not globally —
 *      so numbering stays small ("P1", "P2", …) and matches the backend's
 *      per-group reset, while still being fully independent of the input
 *      array's iteration order (unlike the backend's own loop-order
 *      numbering, which this module deliberately does not reproduce).
 *
 *   4. The pro-rated field list is wider than the brief's four fields
 *      (`pnl`/`realised`/`unrealised`/`day_change_val`): also prorates
 *      `day_pnl`, `prev_settlement_pnl`, `overnight_quantity`, and `lots`
 *      — `prev_settlement_pnl` is REQUIRED (`baseDayPnlForPosition` in
 *      `nav.js` computes `currentTotalProfit(row) − prev_settlement_pnl`;
 *      leaving it unprorated would make split pieces' Day P&L sum to the
 *      wrong total), and `overnight_quantity`/`lots` are both summed or
 *      read directly by downstream consumers (`mergePositionRows`,
 *      `lotsForRow`'s fast path) that would otherwise double/over-count
 *      once a row is represented by multiple pieces.
 *
 * Flat (quantity === 0) rows are always passed through completely
 * unchanged — mirrors the backend's own stated rule ("q == 0: no action —
 * defaults are already correct") and avoids re-deriving pairing state for
 * rows (e.g. `splitClosedReopened`'s CLOSED pieces) that carry stale
 * pairing metadata inherited from a now-irrelevant pre-split quantity.
 *
 * Pure, synchronous, no mutation of the input array or its row objects.
 */

import { decomposeSymbol } from './decomposeSymbol.js';

/**
 * Amount-shaped fields that must be pro-rated (proportionally, remainder-
 * safe) across a row's split pieces so they sum back exactly to the
 * original row's values. Only prorated when present (`!= null`) on the
 * original row — absent fields stay absent on every piece.
 */
const _PRORATE_FIELDS = [
  'pnl', 'realised', 'unrealised', 'day_change_val', 'day_pnl',
  'prev_settlement_pnl', 'overnight_quantity', 'lots',
];

/** @param {any} row @returns {string} */
function _symOf(row) {
  return String(row?.tradingsymbol ?? row?.symbol ?? '').toUpperCase();
}

/** @param {any} row @returns {'quantity'|'qty'} whichever qty field the row actually carries */
function _qtyField(row) {
  return row?.quantity != null ? 'quantity' : 'qty';
}

/** @param {any} row @returns {number} */
function _qtyOf(row) {
  const v = row?.[_qtyField(row)];
  return Number(v) || 0;
}

// Fallback port of the backend's own suffix-stripping regexes
// (`backend/api/routes/positions.py:_EXPIRY_RE` / `_root_symbol` /
// `_option_type`) — used ONLY when `decomposeSymbol` can't parse `sym` as a
// real option/future (i.e. it has no expiry month token at all). Every real
// Kite tradingsymbol carries one, so this path exists purely to keep the
// two algorithms in agreement on synthetic/abbreviated test symbols too.
const _EXPIRY_FALLBACK_RE = /\d{2}[A-Z]{3,}\d*(CE|PE|FUT|OPT|BE|BEF)?$|\d+(CE|PE|FUT|OPT|BE|BEF)$/i;

/** @param {string} sym @returns {string} */
function _rootFallback(sym) {
  const s = sym.toUpperCase().replace(_EXPIRY_FALLBACK_RE, '').replace(/\d+$/, '');
  return s || sym.toUpperCase();
}

/** @param {string} sym @returns {'CE'|'PE'|null} */
function _optTypeFallback(sym) {
  const s = sym.toUpperCase();
  if (!/\d(CE|PE)$/.test(s)) return null; // requires a digit before the suffix — "BAJFINANCE" must not match
  return s.endsWith('CE') ? 'CE' : 'PE';
}

/**
 * @param {string} sym
 * @returns {{root: string, optType: 'CE'|'PE'|null}}
 */
function _rootAndOptType(sym) {
  const d = decomposeSymbol(sym);
  if (d && (d.kind === 'opt' || d.kind === 'fut')) {
    return { root: d.root || sym, optType: d.optType ?? null };
  }
  return { root: _rootFallback(sym), optType: _optTypeFallback(sym) };
}

/**
 * @param {any[]} rows
 * @returns {any[]}
 */
export function splitPairedLots(rows) {
  if (!Array.isArray(rows) || rows.length === 0) return rows || [];

  /** @type {Map<string, number[]>} group key -> row indices */
  const groups = new Map();
  rows.forEach((r, i) => {
    const sym = _symOf(r);
    if (!sym) return;
    if (_qtyOf(r) === 0) return; // flat rows never participate in matching
    const { root, optType } = _rootAndOptType(sym);
    const key = `${r?.account ?? ''}|${root}|${optType ?? ''}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(i);
  });

  // Per-row accumulated result, built group-by-group.
  /** @type {Map<number, {label: string, qty: number}[]>} idx -> matched pieces (unsigned qty) */
  const matchesByIdx = new Map();
  /** @type {Map<number, number>} idx -> leftover unsigned qty */
  const leftoverByIdx = new Map();

  for (const indices of groups.values()) {
    const longs = [];
    const shorts = [];
    for (const i of indices) {
      const q = _qtyOf(rows[i]);
      if (q > 0) longs.push({ i, q });
      else shorts.push({ i, q: -q }); // store as positive magnitude
    }
    // Largest-first; ties broken by the row's own symbol so the match
    // STRUCTURE itself (not just the labels) doesn't depend on input order.
    const _cmp = (a, b) => (b.q - a.q) || (_symOf(rows[a.i]) < _symOf(rows[b.i]) ? -1 : 1);
    longs.sort(_cmp);
    shorts.sort(_cmp);

    const longsQ = longs.map(x => ({ ...x }));
    const shortsQ = shorts.map(x => ({ ...x }));

    /** @type {{longIdx: number, shortIdx: number, qty: number}[]} */
    const events = [];
    while (longsQ.length && shortsQ.length) {
      const L = longsQ[0];
      const S = shortsQ[0];
      const matchQty = Math.min(L.q, S.q);
      events.push({ longIdx: L.i, shortIdx: S.i, qty: matchQty });
      L.q -= matchQty;
      S.q -= matchQty;
      if (L.q === 0) longsQ.shift();
      if (S.q === 0) shortsQ.shift();
    }
    for (const L of longsQ) if (L.q > 0) leftoverByIdx.set(L.i, L.q);
    for (const S of shortsQ) if (S.q > 0) leftoverByIdx.set(S.i, S.q);

    // Deterministic per-group labelling: rank this group's own match
    // events by qty desc, ties broken by the two matched symbols sorted
    // alphabetically — independent of loop/insertion order.
    const ranked = [...events].sort((a, b) => {
      if (b.qty !== a.qty) return b.qty - a.qty;
      const aKey = [_symOf(rows[a.longIdx]), _symOf(rows[a.shortIdx])].sort().join('|');
      const bKey = [_symOf(rows[b.longIdx]), _symOf(rows[b.shortIdx])].sort().join('|');
      return aKey < bKey ? -1 : (aKey > bKey ? 1 : 0);
    });
    ranked.forEach((e, rank) => {
      const label = `P${rank + 1}`;
      if (!matchesByIdx.has(e.longIdx)) matchesByIdx.set(e.longIdx, []);
      if (!matchesByIdx.has(e.shortIdx)) matchesByIdx.set(e.shortIdx, []);
      matchesByIdx.get(e.longIdx).push({ label, qty: e.qty });
      matchesByIdx.get(e.shortIdx).push({ label, qty: e.qty });
    });
  }

  return rows.map((row, idx) => {
    const matches = matchesByIdx.get(idx) || [];
    const leftover = leftoverByIdx.get(idx) || 0;
    if (matches.length === 0 && leftover === 0) return row; // flat / unparticipating — untouched

    const sign = _qtyOf(row) > 0 ? 1 : -1;
    // Matched pieces first (ascending by numeric label), orphan/leftover last —
    // deterministic order so remainder-based pro-rating is reproducible.
    const matchedSorted = [...matches].sort(
      (a, b) => Number(a.label.slice(1)) - Number(b.label.slice(1))
    );
    /** @type {{label: string|null, qty: number, isOrphan: boolean}[]} */
    const specs = matchedSorted.map(m => ({ label: m.label, qty: sign * m.qty, isOrphan: false }));
    if (leftover > 0) specs.push({ label: null, qty: sign * leftover, isOrphan: true });

    if (specs.length === 1) {
      const spec = specs[0];
      const pairedQty = spec.isOrphan ? 0 : Math.abs(spec.qty);
      const orphanQty = spec.isOrphan ? Math.abs(spec.qty) : 0;
      const unchanged =
        row.pair_group_key === spec.label &&
        row.paired_qty === pairedQty &&
        row.orphan_qty === orphanQty &&
        row.is_orphan === spec.isOrphan;
      if (unchanged) return row;
      return {
        ...row,
        pair_group_key: spec.label,
        paired_qty: pairedQty,
        orphan_qty: orphanQty,
        is_orphan: spec.isOrphan,
      };
    }

    // Genuine multi-piece split — pro-rate amount-shaped fields,
    // remainder-based (last piece gets the exact remainder, never
    // independently rounded) so Σ pieces === original exactly.
    const qtyField = _qtyField(row);
    const totalAbsQty = specs.reduce((s, p) => s + Math.abs(p.qty), 0) || 1;
    const assigned = {};
    for (const f of _PRORATE_FIELDS) assigned[f] = 0;

    return specs.map((spec, i) => {
      const isLast = i === specs.length - 1;
      const frac = Math.abs(spec.qty) / totalAbsQty;
      const label = spec.label ?? 'Unmatched';
      const piece = { ...row };
      piece[qtyField] = spec.qty;
      piece.pair_group_key = spec.label;
      piece.paired_qty = spec.isOrphan ? 0 : Math.abs(spec.qty);
      piece.orphan_qty = spec.isOrphan ? Math.abs(spec.qty) : 0;
      piece.is_orphan = spec.isOrphan;
      piece._isLotSplit = true;
      piece.lot_piece_label = label;
      piece._lotFraction = frac;
      piece.key = `${row.key || _symOf(row)}::${label}`;
      for (const f of _PRORATE_FIELDS) {
        if (row[f] == null) continue;
        const total = Number(row[f]) || 0;
        const val = isLast ? (total - assigned[f]) : total * frac;
        if (!isLast) assigned[f] += val;
        piece[f] = val;
      }
      return piece;
    });
  }).flat();
}
