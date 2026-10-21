// Pure, store-free conversion of a single `payoffDrafts` entry (see
// payoffDrafts.svelte.js) into a generic position-shaped row.
//
// Extracted from pulseUnified.js's mergeDraftRows() (2026-10) so the
// identical identity/qty/price mapping is shared by every surface that
// displays draft positions alongside real ones — MarketPulse's Positions
// grid (via mergeDraftRows, which maps these fields onto its own internal
// `qty_pos`/`avg_combined` row-factory schema) and PerformancePage's
// Positions Breakdown grid (which maps them onto its own broker-raw-shaped
// `quantity`/`average_price`/`last_price` schema).
//
// Deliberately excludes anything consumer-specific: no financial
// aggregation (day_pnl/pnl/cost-basis), no ag-Grid row-factory coupling,
// no Svelte reactivity/store imports. Each caller maps the returned
// neutral fields onto its own row schema and computes whatever P&L
// fields it needs itself.
//
// CLAUDE.md invariant: a draft is display-layer-only. This function never
// writes anywhere and must only be called at a render/derive boundary,
// reading `payoffDrafts.value` fresh each time — never inside
// `fetchPositions()`/`positionsStore`'s own fetch or cache logic.

/**
 * @param {{
 *   id: number, symbol: string, qty: number, avg_cost?: number|null,
 *   underlying?: string|null, exchange?: string|null, account?: string|null,
 *   option_type?: 'CE'|'PE'|null, strike?: number|null, expiry?: string|null,
 * } | null | undefined} entry  one payoffDrafts.value entry
 * @param {(sym: string) => any} liveSnapFn  symbolStore live-snapshot lookup
 *   (e.g. `liveSnap` from symbolStore.svelte.js, or an equivalent
 *   `untrack(() => getSnapshot(sym))` closure) — supplies ltp/bid/ask/
 *   open/close/day_change/day_change_pct. Never reads the entry's own
 *   `.ltp` field — payoffDrafts.svelte.js documents it as always
 *   empty/stale; live price always comes from the symbol store.
 * @returns {{
 *   tradingsymbol: string, exchange: string, account: string,
 *   underlying: string|null, opt_type: 'CE'|'PE'|null,
 *   strike: number|null, expiry: string|null,
 *   qty: number, avg_cost: number|null,
 *   draftId: number, source: 'draft',
 *   ltp: number|null, bid: number|null, ask: number|null,
 *   open: number|null, close: number|null,
 *   change: number|null, change_pct: number|null,
 * } | null}  null when the entry has no symbol/id — nothing to render
 */
export function draftEntryToPositionRow(entry, liveSnapFn) {
  const sym = String(entry?.symbol || '').toUpperCase();
  if (!sym || entry?.id == null) return null;
  const snap = typeof liveSnapFn === 'function' ? liveSnapFn(sym) : null;
  return {
    tradingsymbol: sym,
    exchange:      entry.exchange || 'NFO',
    account:       entry.account || '',
    underlying:    entry.underlying || null,
    opt_type:      entry.option_type ?? null,
    strike:        entry.strike ?? null,
    expiry:        entry.expiry ?? null,
    qty:           Number(entry.qty) || 0,
    avg_cost:      entry.avg_cost != null ? Number(entry.avg_cost) : null,
    draftId:       entry.id,
    source:        /** @type {'draft'} */ ('draft'),
    ltp:        snap?.ltp        ?? null,
    bid:        snap?.bid        ?? null,
    ask:        snap?.ask        ?? null,
    open:       snap?.open       ?? null,
    close:      snap?.close      ?? null,
    change:     snap?.day_change     ?? null,
    change_pct: snap?.day_change_pct ?? null,
  };
}
