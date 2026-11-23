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

  // Global pair counter — shared across every group so labels never
  // restart at "P1" for a second root/option-type bucket (operator,
  // 2026-10-17: "every group in legs and positions has p1... each group
  // should have a different value"). Groups are visited in sorted key
  // order (not Map insertion order, which follows row arrival order from
  // the API) so the numbering is reproducible — mirrors the backend's
  // identical fix in `_auto_pair_positions()` (positions.py).
  let _pairN = 1;
  const _sortedGroupKeys = [...groups.keys()].sort();
  for (const _groupKey of _sortedGroupKeys) {
    const indices = groups.get(_groupKey);
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
    ranked.forEach((e) => {
      const label = `P${_pairN++}`;
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

/**
 * consolidateLotSplitPieces(rows) — the exact inverse of `splitPairedLots`:
 * collapses every lot-waterfall-split DISPLAY piece
 * (`row._isLotSplit === true`) belonging to the same original source row
 * back into ONE row carrying the full quantity + every `_PRORATE_FIELDS`
 * entry summed back together (a plain sum is exact here — these pieces
 * were produced by exact remainder-based pro-rating on the way out).
 *
 * Used ONLY by the derivatives page's 'legs' (unpaired, account-grouped)
 * view — `candidateRowComposition.js`'s `composeDisplayedCandidates()` —
 * where showing each lot-waterfall piece as its own row is wrong: the
 * operator expects "one row per account+symbol", not one row per
 * counterparty-matched slice. The 'paired'/'expiry' views are UNCHANGED —
 * they are the surfaces that actually need the split pieces, to show each
 * piece's own `pair_group_key`.
 *
 * Grouping key is `(source, account, symbol, _splitTag, _storeKey)` — NOT
 * `lot_piece_label` (exactly what must collapse) and NOT `pair_group_key`
 * (same reason). `_splitTag` ('closed'/'open', see expiryPnl.js's
 * `splitClosedReopened`) is included so a position's CLOSED piece and its
 * OPEN (reopened) piece — each independently lot-split against different
 * counterparties — never merge into each other.
 *
 * `_storeKey` (`storeRowKey()` in `derivatives/pageLoad.js`, includes
 * `product` — NRML vs MIS) is a DELIBERATE DEVIATION from the originally
 * approved grouping key, which named only `(source, account, symbol,
 * _splitTag)`. Every lot-split piece from the live pipeline
 * (`buildPagePositionRows`) already carries the SAME `_storeKey` as every
 * other piece of its own group (copied verbatim, never pro-rated) — so
 * adding it here is a no-op for the overwhelmingly common case and only
 * changes behaviour in the rare case where the SAME account+symbol holds
 * two different broker positions (e.g. NRML and MIS) that both happen to
 * land in a multi-counterparty lot split. Without this extra dimension,
 * such a case would silently merge two DIFFERENT broker positions into one
 * display row and — worse — `liveStoreExpPnl()` (pageLoad.js) would then
 * report only ONE product's live Exp P&L for the merged row, since
 * `_storeKey`/`_pieceIndex`/`_pieceCount` (carried from the first piece)
 * would point at only one of the two source rows.
 *
 * A row with no `row._isLotSplit` flag (never lot-split at all — the
 * common case, or a draft/proxy/provisional row that didn't go through
 * `splitPairedLots`) passes through COMPLETELY UNCHANGED, at its original
 * position in the array.
 *
 * A merged row carries a new `_mergedPieceKeys: string[]` — the
 * `lot_piece_label` of every constituent piece it represents — consumed
 * by `mergedLegPieces()` below so the page's checkbox state (keyed by
 * `lot_piece_label` via the page's own `enKey`) can still be read/written
 * per constituent piece even though only one row is rendered.
 *
 * Pure, synchronous, no mutation of the input array or its row objects.
 *
 * @param {any[]} rows
 * @returns {any[]}
 */
export function consolidateLotSplitPieces(rows) {
  if (!Array.isArray(rows) || rows.length === 0) return rows || [];

  /** @param {any} r @returns {string} */
  const groupKeyOf = (r) =>
    `${r.source ?? ''}|${r.account ?? ''}|${_symOf(r)}|${r._splitTag ?? ''}|${r._storeKey ?? ''}`;

  /** @type {Map<string, any[]>} groupKey -> constituent pieces, in array order */
  const groups = new Map();
  /** @type {Map<string, number>} groupKey -> index of the FIRST piece seen (merge point) */
  const firstIndexByKey = new Map();
  /** @type {Map<number, string>} row index -> its own groupKey (split pieces only) */
  const keyByIndex = new Map();

  rows.forEach((r, i) => {
    if (!r || r._isLotSplit !== true) return;
    const key = groupKeyOf(r);
    keyByIndex.set(i, key);
    if (!groups.has(key)) {
      groups.set(key, []);
      firstIndexByKey.set(key, i);
    }
    groups.get(key).push(r);
  });

  /** @type {Map<string, any>} groupKey -> merged row */
  const mergedByKey = new Map();
  for (const [key, pieces] of groups) mergedByKey.set(key, _mergeLotSplitGroup(pieces));

  const out = [];
  rows.forEach((r, i) => {
    const key = keyByIndex.get(i);
    if (key == null) {
      out.push(r); // never lot-split — pass through unchanged, original position
      return;
    }
    if (firstIndexByKey.get(key) === i) out.push(mergedByKey.get(key));
    // else: a later piece of an already-merged group — omit (folded into
    // the merged row emitted at the group's first position).
  });
  return out;
}

/**
 * @param {any[]} pieces - 2+ lot-split display pieces of ONE original row
 * @returns {any} the merged row
 */
function _mergeLotSplitGroup(pieces) {
  const merged = { ...pieces[0] };

  const qtyField = _qtyField(pieces[0]);
  merged[qtyField] = pieces.reduce((s, p) => s + (Number(p[qtyField]) || 0), 0);

  for (const f of _PRORATE_FIELDS) {
    if (pieces[0][f] == null) continue;
    merged[f] = pieces.reduce((s, p) => s + (Number(p[f]) || 0), 0);
  }

  // Restore the pre-split `key` by stripping the `::<label>` suffix
  // `splitPairedLots` appended (`${row.key || sym}::${label}`) — the
  // label itself never contains '::', so the LAST occurrence is always
  // the one the split added.
  if (typeof merged.key === 'string') {
    const i = merged.key.lastIndexOf('::');
    if (i >= 0) merged.key = merged.key.slice(0, i);
  }

  delete merged.pair_group_key;
  delete merged.paired_qty;
  delete merged.orphan_qty;
  delete merged.is_orphan;
  delete merged._isLotSplit;
  delete merged.lot_piece_label;
  delete merged._lotFraction;

  merged._mergedPieceKeys = pieces.map((p) => p.lot_piece_label);

  return merged;
}

/**
 * For a merged Legs-view row produced by `consolidateLotSplitPieces`
 * (carrying `_mergedPieceKeys`), rebuilds each constituent piece's own row
 * shape — same `account`/`symbol`/`_splitTag` as the merged row (spread
 * straight through), with `lot_piece_label` swapped back to each
 * constituent's own label — so a caller's EXISTING `enKey(row)` function
 * computes the exact same composite key the original un-merged piece had.
 * No second, independently-typed key-building logic to drift out of sync
 * with `enKey`'s own `[row._splitTag, row.lot_piece_label].filter(Boolean)
 * .join('|')` shape.
 *
 * `_mergedPieceKeys` itself is stripped from each rebuilt piece — a
 * caller that recurses through its own checked-state function on a
 * rebuilt piece must see a genuinely unmerged row, not loop forever.
 *
 * Returns `[]` for a row that isn't merged (no `_mergedPieceKeys`), so a
 * caller can write `const parts = mergedLegPieces(c); if (parts.length) …`
 * uniformly for both the merged and unmerged case.
 *
 * @param {{_mergedPieceKeys?: string[]}} c
 * @returns {any[]}
 */
export function mergedLegPieces(c) {
  if (!Array.isArray(c?._mergedPieceKeys) || c._mergedPieceKeys.length === 0) return [];
  return c._mergedPieceKeys.map((label) => {
    const piece = { ...c, lot_piece_label: label };
    delete piece._mergedPieceKeys;
    return piece;
  });
}
