/**
 * Pure helpers for updating the _underlyingQuotes map on the derivatives page.
 * Kept separate so they can be unit-tested without importing the Svelte component.
 */

/**
 * Returns a new quotes map with `root`'s ltp updated to `ltp`.
 * No-ops if root not in quotes or ltp is not a finite positive number.
 * @param {Record<string, {ltp: number, day_pct: number|null, prev_close: number}>} quotes
 * @param {string} root
 * @param {number|null|undefined} ltp
 * @returns {Record<string, {ltp: number, day_pct: number|null, prev_close: number}>}
 */
export function applyUnderlyingTickLtp(quotes, root, ltp) {
  if (!(root in quotes)) return quotes;
  const v = Number(ltp);
  if (!Number.isFinite(v) || v <= 0) return quotes;
  if (quotes[root].ltp === v) return quotes;  // same LTP — no $state write needed
  return { ...quotes, [root]: { ...quotes[root], ltp: v } };
}

/**
 * Build the merged underlyingSpotStore quote map from a batchQuote response,
 * guarding against two races that a plain object-spread merge doesn't:
 *
 *   1. Zero-value guard — a missing/zero broker row for a root must not
 *      clobber a previously-good ltp/prev_close for that root. batchQuote
 *      can return ltp=0 for a transiently-unavailable instrument; blindly
 *      writing that through blanks a cell that was showing a real price.
 *
 *   2. Per-root ordering guard — a batchQuote request can resolve AFTER a
 *      newer live tick has already been applied via patchUnderlyingSpot
 *      (SSE ticks arrive at WebSocket speed; the poll round-trip can take
 *      longer). Detected via `lastTickAt[root] > reqStartedAt`: if a tick
 *      for this root landed after this request started, the response's
 *      ltp is older than what's already on screen — discard the
 *      response's ltp specifically, but still let prev_close/day_pct
 *      update from it (those aren't tick-driven, so there's no fresher
 *      value to protect).
 *
 * When either guard keeps the previous ltp, day_pct is recomputed from
 * the KEPT ltp against the response's close — the broker's own
 * change_pct/change_percent field is relative to the ltp we just
 * discarded and would be wrong paired with the kept value.
 *
 * Returns the full merged map (previous quotes spread + this batch's
 * updates) — callers assign the return value directly as the new store
 * state; no separate `{...prev, ...next}` merge step needed.
 *
 * @param {Array<{ root: string, quoteKey: string }>} pairs
 * @param {any[]} items - batchQuote response items
 * @param {Record<string, { ltp: number, day_pct: number|null, prev_close: number }>} prevQuotes
 * @param {Record<string, number>} lastTickAt - root → epoch-ms of last patchUnderlyingSpot apply
 * @param {number} reqStartedAt - epoch-ms captured before the batchQuote await
 * @returns {Record<string, { ltp: number, day_pct: number|null, prev_close: number }>}
 */
export function buildUnderlyingQuoteUpdate(pairs, items, prevQuotes, lastTickAt, reqStartedAt) {
  /** @type {Record<string, any>} */
  const byKey = {};
  for (const it of items || []) {
    if (!it?.exchange || !it?.tradingsymbol) continue;
    byKey[`${it.exchange}:${it.tradingsymbol}`] = it;
  }

  const next = { ...(prevQuotes || {}) };
  for (const { root, quoteKey } of pairs || []) {
    const q = byKey[quoteKey];
    if (!q) continue;

    const prev = (prevQuotes || {})[root];
    const rawLtp   = Number(q.ltp   ?? q.last_price ?? 0);
    const rawClose = Number(q.close ?? q.ohlc?.close ?? 0);

    // Ordering guard: a tick applied after this request started makes
    // this response's ltp stale, regardless of whether it's nonzero.
    const staleLtp = (Number(lastTickAt?.[root]) || 0) > (Number(reqStartedAt) || 0);
    // Zero-value guard: a missing/zero broker row must not clobber a
    // previously-good ltp.
    const zeroLtp  = !(rawLtp > 0);

    let ltp = rawLtp;
    let kept = false;
    if ((staleLtp || zeroLtp) && prev?.ltp > 0) {
      ltp  = prev.ltp;
      kept = true;
    }

    let close = rawClose;
    if (!(rawClose > 0) && prev?.prev_close > 0) {
      close = prev.prev_close;
    }

    let pct = null;
    if (kept) {
      // Broker's change_pct/change_percent is relative to the ltp we
      // just discarded — recompute from the kept ltp instead.
      pct = close > 0 && ltp > 0 ? ((ltp - close) / close) * 100 : null;
    } else if (q.change_pct != null) {
      pct = Number(q.change_pct);
    } else if (q.change_percent != null) {
      pct = Number(q.change_percent);
    } else if (close > 0 && ltp > 0) {
      pct = ((ltp - close) / close) * 100;
    }

    next[root] = { ltp, day_pct: pct, prev_close: close };
  }
  return next;
}
