// pulseColumns.js — ag-Grid column-definition factories for MarketPulse.
//
// Every factory accepts ACCESSOR FUNCTIONS (getters) for any reactive
// state that is reassigned in the Svelte component (e.g. _ltpFlashUp
// is `$state(new Set())` and gets reassigned each tick). Plain values
// are accepted for stable references (_mpFlash, formatters, RA const).
//
// Pattern:
//   mkLtpCol({ getLiveLtpSnap, getLtpFlashUp, getLtpFlashDown, ... })
//   NOT: mkLtpCol({ liveLtpSnap, ltpFlashUp, ... })  ← frozen snapshot
//
// The factories are called ONCE at onMount (inside mountGrid) and the
// column-definition objects are handed to ag-Grid, which holds onto the
// closures. Accessors ensure the closures see the current $state value
// on every ag-Grid redraw — not the stale binding captured at mount.

// ─── Pure helpers ────────────────────────────────────────────────────

/**
 * Map a numeric P&L value to a CSS direction class.
 * Pure function — safe to call anywhere without reactive context.
 * @param {number|null|undefined} v
 * @returns {string}
 */
export function dirCls(v) {
  if (v == null) return 'cell-flat';
  if (v > 0) return 'cell-pos';
  if (v < 0) return 'cell-neg';
  return 'cell-flat';
}

// ─── Shared column factories ─────────────────────────────────────────

/**
 * Build the `cellClass` function used for P&L and Day-P&L columns.
 * Merges directional text colouring, mp-pnl-cell bg tint, and
 * LTP-cascade / poll-diff tick flash.
 *
 * @param {{
 *   RA: string,
 *   getMpFlash: () => ReturnType<typeof import('$lib/data/tickFlash.svelte.js').createTickFlash>,
 *   getLtpFlashUp: () => Set<string>,
 *   getLtpFlashDown: () => Set<string>,
 * }} opts
 * @returns {(p: any, field: string) => string}
 */
export function mkPnlCellClass({ RA, getMpFlash, getLtpFlashUp, getLtpFlashDown }) {
  return (p, field) => {
    const base = `${RA} ${dirCls(p.value)} mp-pnl-cell`;
    if (p.data?._isTotal) return base;
    const sym = p.data?.tradingsymbol;
    if (!sym || !field) return base;
    const symUpper = String(sym).toUpperCase();
    const ltpFlashUp   = getLtpFlashUp();
    const ltpFlashDown = getLtpFlashDown();
    // LTP cascade takes precedence over poll-diff flash.
    if (ltpFlashUp.has(symUpper))   return `${base} ltp-flash-up`;
    if (ltpFlashDown.has(symUpper)) return `${base} ltp-flash-down`;
    const fc = getMpFlash().classOf(`${sym}:${field}`);
    return fc ? `${base} ${fc}` : base;
  };
}

/**
 * Build the resolver used by LTP valueGetter + _ltpCol.cellClass.
 * Returns null when no positive price is available so cells render "—".
 *
 * @param {{ getLiveLtpSnap: () => Record<string, number> }} opts
 * @returns {(p: any) => number|null}
 */
export function mkResolveCellLtp({ getLiveLtpSnap }) {
  return (p) => {
    if (!p?.data) return null;
    const snap = getLiveLtpSnap();
    // For MCX mover rows, tradingsymbol is the bare commodity root
    // (CRUDEOIL) while SSE ticks are keyed on the resolved front-month
    // contract (CRUDEOIL26JUNFUT). Try quote_symbol first so the LTP
    // cell reads the sub-second SSE value rather than falling through
    // to the 30 s polled last_price.
    const quoteSym = p.data.quote_symbol ? String(p.data.quote_symbol).toUpperCase() : null;
    if (quoteSym) {
      const liveBySym = snap[quoteSym];
      if (typeof liveBySym === 'number' && liveBySym > 0) return liveBySym;
    }
    const sym  = String(p.data.tradingsymbol || '').toUpperCase();
    const live = snap[sym];
    if (typeof live === 'number' && live > 0) return live;
    const polled = Number(p.data.ltp);
    if (Number.isFinite(polled) && polled > 0) return polled;
    return null;
  };
}

// ─── Symbol columns ──────────────────────────────────────────────────

/**
 * Left-grid symbol column (Pinned / Watchlist / Movers).
 * @param {{ symRenderer: (p: any) => any }} opts
 */
export function mkSymColLeft({ symRenderer }) {
  return {
    field: 'tradingsymbol', headerName: 'Symbol', width: 168, pinned: 'left',
    cellRenderer: symRenderer, sortable: true,
    cellClass: 'ag-col-sym ag-col-fill',
  };
}

/**
 * Right-grid symbol column (Positions / Holdings) with account-tinted bg.
 * @param {{ symRenderer: (p: any) => any }} opts
 */
export function mkSymColRight({ symRenderer }) {
  return {
    field: 'tradingsymbol', headerName: 'Symbol', width: 168, pinned: 'left',
    cellRenderer: symRenderer, sortable: true,
    cellClass: 'ag-col-sym ag-col-fill mp-sym-acct',
    cellStyle: (p) => {
      if (p.data?._isTotal) return {};
      const color = p.data?._acctColor ?? null;
      if (!color) return {};
      return { '--mp-sym-acct-color': color };
    },
  };
}

// ─── Sparkline column ────────────────────────────────────────────────

/**
 * 5-day sparkline column (both grids).
 * @param {{ sparkRenderer: (p: any) => any }} opts
 */
export function mkSparkCol({ sparkRenderer }) {
  return {
    field: 'tradingsymbol', headerName: '5d', width: 44, minWidth: 44,
    maxWidth: 48, colId: 'sparkline',
    cellRenderer: (p) => p.data?._isTotal ? '' : sparkRenderer(p),
    sortable: false, resizable: false,
    cellClass: 'spark-cell',
    headerClass: 'ag-header-cell-spark',
  };
}

// ─── LTP column ──────────────────────────────────────────────────────

/**
 * LTP column with real-time SSE tick, directional flash, and vs-avg/vs-prev
 * heat encoding. Snapshot rows (is_animating=false) render a static LTP
 * number without any visible chip — tick-flash is still gated on is_animating.
 *
 * @param {{
 *   getLiveLtpSnap: () => Record<string, number>,
 *   getLtpFlashUp: () => Set<string>,
 *   getLtpFlashDown: () => Set<string>,
 *   numFmt: (p: { value: any }) => string,
 *   RA: string,
 *   numericHdr: string,
 * }} opts
 */
// Legacy `ltp_source` value → normalised `price_source` value. The
// backend renamed the field in Jul 2026 (unified animation model);
// the old value only appears on cached responses served during the
// deploy window. `snapshot` maps to `snapshot_settled` (safe default).
function _normalisePriceSource(row) {
  if (!row) return 'live';
  const ps = row.price_source ?? row.ltp_source ?? 'live';
  if (ps === 'snapshot') return 'snapshot_settled';
  return ps;
}

// Under the unified model, `is_animating` is the SINGLE decision point
// for cell animation. Rows on a currently-open exchange animate; rows
// carrying a close-snapshot LTP freeze. Defaults to true (animating) so
// rows without the field (movers, watchlist) keep live-flash behaviour
// rather than being incorrectly frozen. The legacy price_source fallback
// is removed — it produced false-negatives during market hours when the
// field was absent (e.g. movers rows), which suppressed tick-flash and
// added the ltp-snap class even on live-price rows.
function _isAnimating(row) {
  if (!row) return true;
  if (typeof row.is_animating === 'boolean') return row.is_animating;
  return true;
}

export function mkLtpCol({ getLiveLtpSnap, getLtpFlashUp, getLtpFlashDown, numFmt, RA, numericHdr }) {
  const resolveCellLtp = mkResolveCellLtp({ getLiveLtpSnap });
  return {
    colId: 'ltp', headerName: 'LTP', width: 77, minWidth: 77, maxWidth: 96,
    type: 'numericColumn', headerClass: numericHdr,
    cellClass: (p) => {
      if (!p.data || p.data._isTotal) return RA;
      const sym  = String(p.data.tradingsymbol || '').toUpperCase();
      const ltp  = resolveCellLtp(p);
      const prev = p.data.close ?? null;
      const avg  = (p.data.qty_pos && p.data.avg_pos) ? p.data.avg_pos
                 : (p.data.qty_hold && p.data.avg_hold) ? p.data.avg_hold
                 : null;
      const cls = [RA];
      const animating = _isAnimating(p.data);
      const ps = _normalisePriceSource(p.data);
      // Animation gate — tick-flash only when the row's exchange is
      // currently open. Snapshot rows render static.
      if (animating) {
        const ltpFlashUp   = getLtpFlashUp();
        const ltpFlashDown = getLtpFlashDown();
        if      (ltpFlashUp.has(sym))   cls.push('ltp-flash-up');
        else if (ltpFlashDown.has(sym)) cls.push('ltp-flash-down');
      } else {
        cls.push('ltp-snap');
        // Slight visual differentiation for the pre-settle window —
        // frontend can style .ltp-snap-unsettled with a dashed border
        // to convey "close_price not yet published".
        if (ps === 'snapshot_unsettled') cls.push('ltp-snap-unsettled');
      }
      if (typeof ltp === 'number' && typeof avg === 'number' && avg > 0) {
        cls.push(ltp > avg ? 'ltp-vs-avg-up' : ltp < avg ? 'ltp-vs-avg-down' : 'ltp-vs-avg-flat');
      }
      if (typeof ltp === 'number' && typeof prev === 'number' && prev > 0) {
        cls.push(ltp > prev ? 'ltp-vs-prev-up' : ltp < prev ? 'ltp-vs-prev-down' : 'ltp-vs-prev-flat');
      }
      return cls.join(' ');
    },
    valueGetter: resolveCellLtp,
    valueFormatter: (p) => p.data?._isTotal ? '' : numFmt({ value: p.value }),
  };
}

// ─── Prev/Open columns ───────────────────────────────────────────────

/**
 * Previous-close column.
 * @param {{ RA: string, numericHdr: string, numFmt: (p: { value: any }) => string }} opts
 */
export function mkPrevCol({ RA, numericHdr, numFmt }) {
  return {
    field: 'close', headerName: 'Close', width: 68, minWidth: 68, maxWidth: 84,
    type: 'numericColumn', headerClass: numericHdr,
    cellClass: `${RA} cell-muted`,
    valueFormatter: (p) => p.data?._isTotal ? '' : numFmt({ value: p.value }),
  };
}

/**
 * Open price column.
 * @param {{ RA: string, numericHdr: string, numFmt: (p: { value: any }) => string }} opts
 */
export function mkOpenCol({ RA, numericHdr, numFmt }) {
  return {
    field: 'open', headerName: 'Open', width: 68, minWidth: 68, maxWidth: 90,
    type: 'numericColumn', headerClass: numericHdr,
    cellClass: `${RA} cell-muted`,
    valueFormatter: (p) => p.data?._isTotal ? '' : numFmt({ value: p.value }),
  };
}

// ─── Volume / OI columns ─────────────────────────────────────────────

/**
 * Volume column.
 * @param {{ RA: string, numericHdr: string, aggCompact: (v: number) => string }} opts
 */
export function mkVolCol({ RA, numericHdr, aggCompact }) {
  return {
    field: 'volume', headerName: 'Vol', width: 58, minWidth: 58, maxWidth: 80,
    type: 'numericColumn', headerClass: numericHdr,
    cellClass: `${RA} cell-muted`,
    valueFormatter: (p) => {
      if (p.data?._isTotal) return '';
      return (p.value == null || p.value === 0) ? '—' : aggCompact(p.value);
    },
  };
}

/**
 * Open-interest column.
 * @param {{ RA: string, numericHdr: string, aggCompact: (v: number) => string }} opts
 */
export function mkOiCol({ RA, numericHdr, aggCompact }) {
  return {
    field: 'oi', headerName: 'OI', width: 58, minWidth: 58, maxWidth: 80,
    type: 'numericColumn', headerClass: numericHdr,
    cellClass: `${RA} cell-muted`,
    valueFormatter: (p) => {
      if (p.data?._isTotal) return '';
      return (p.value == null || p.value === 0) ? '—' : aggCompact(p.value);
    },
  };
}

// ─── Account (trailing) column ───────────────────────────────────────

/**
 * Trailing Account column for the right (Positions/Holdings) grid.
 * Renders a "STALE @ HH:MM" badge when the row came from the broker_apis
 * LKG frame cache (circuit breaker OPEN at fetch time).
 *
 * @param {{ RA: string }} opts
 */
export function mkAcctColTrailing({ RA }) {
  return {
    field: '_acct_display', headerName: 'Account', colId: 'account',
    width: 86, minWidth: 70, maxWidth: 110,
    cellClass: 'mp-acct-cell',
    cellStyle: (p) => {
      if (p.data?._isTotal) return {};
      const color = p.data?._acctColor ?? null;
      if (!color) return {};
      return { color };
    },
    valueGetter: (p) => {
      if (p.data?._isTotal) return '';
      const accts = p.data?.accounts;
      if (!accts) return '';
      const list = accts instanceof Set ? Array.from(accts) : Array.isArray(accts) ? accts : [];
      if (list.length === 0) return '';
      if (list.length === 1) return list[0];
      return `${list[0]} +${list.length - 1}`;
    },
    cellRenderer: (p) => {
      const val = p.valueFormatted ?? p.value ?? '';
      const stale = p.data?.account_stale === true;
      const since = p.data?.account_stale_since;
      if (!stale || !since) return val || '';
      const el = document.createElement('span');
      el.style.cssText = 'display:flex;align-items:center;gap:4px;white-space:nowrap;overflow:hidden;';
      const name = document.createElement('span');
      name.textContent = val;
      name.style.cssText = 'overflow:hidden;text-overflow:ellipsis;';
      const badge = document.createElement('span');
      badge.textContent = `STALE@${since.replace(' IST', '')}`;
      badge.style.cssText = 'font-size:9px;color:rgba(148,163,184,0.75);flex-shrink:0;';
      badge.title = `Last live data: ${since}`;
      el.appendChild(name);
      el.appendChild(badge);
      return el;
    },
  };
}

// ─── Left column-def array ───────────────────────────────────────────

/**
 * Build the left-grid column-def array (Pinned / Watchlist / Movers).
 * Inlines the Day % column (no factory needed — it's one-off).
 *
 * @param {{
 *   symColLeft: any,
 *   sparkCol: any,
 *   ltpCol: any,
 *   prevCol: any,
 *   openCol: any,
 *   volCol: any,
 *   oiCol: any,
 *   numericHdr: string,
 *   dirCellClass: (p: any) => string,
 *   pctFmtGrid: (p: { value: any }) => string,
 * }} opts
 * @returns {any[]}
 */
export function mkLeftColDefs({ symColLeft, sparkCol, ltpCol, prevCol, openCol, volCol, oiCol, numericHdr, dirCellClass, pctFmtGrid }) {
  return /** @type {any[]} */ ([
    symColLeft,
    sparkCol,
    ltpCol,
    { field: 'change_pct', headerName: 'Day %', colId: 'left_change_pct',
      width: 64, type: 'numericColumn', headerClass: numericHdr,
      cellClass: dirCellClass,
      valueFormatter: pctFmtGrid,
      headerTooltip: 'Raw symbol day-change % (no qty).' },
    prevCol,
    openCol,
    volCol,
    oiCol,
  ]);
}

// ─── Right column-def array ──────────────────────────────────────────

/**
 * Build the right-grid column-def array (Positions / Holdings).
 *
 * @param {{
 *   symColRight: any,
 *   sparkCol: any,
 *   ltpCol: any,
 *   prevCol: any,
 *   openCol: any,
 *   volCol: any,
 *   oiCol: any,
 *   acctColTrailing: any,
 *   RA: string,
 *   numericHdr: string,
 *   pnlCellClass: (p: any, field: string) => string,
 *   dirCellClass: (p: any) => string,
 *   pctFmtGrid: (p: { value: any }) => string,
 *   aggFmtGrid: (p: { value: any }) => string,
 *   numFmt: (p: { value: any }) => string,
 *   qtyFmt: (v: number) => string,
 *   lotsForRow: (row: any) => number|null,
 *   fmtLots: (v: number|null|undefined) => string,
 * }} opts
 * @returns {any[]}
 */
export function mkRightColDefs({
  symColRight, sparkCol, ltpCol, prevCol, openCol, volCol, oiCol, acctColTrailing,
  RA, numericHdr,
  pnlCellClass, dirCellClass, pctFmtGrid, aggFmtGrid, numFmt, qtyFmt,
  lotsForRow, fmtLots,
}) {
  return /** @type {any[]} */ ([
    symColRight,
    sparkCol,
    ltpCol,
    { field: 'avg_combined', headerName: 'Avg', colId: 'avg_combined',
      width: 68, minWidth: 60, maxWidth: 90,
      type: 'numericColumn', headerClass: numericHdr,
      // Directional tint mirrors LTP/P&L cells: long (net qty > 0) = green,
      // short (net qty < 0) = red, flat/total = plain RA.
      cellClass: (p) => {
        if (!p.data || p.data._isTotal) return RA;
        const qty = (Number(p.data.qty_pos) || 0) + (Number(p.data.qty_hold) || 0);
        return `${RA} ${dirCls(qty)}`;
      },
      valueFormatter: (p) => p.data?._isTotal ? '' : numFmt({ value: p.value }),
      headerTooltip: 'Weighted average entry across positions + holdings.' },
    { field: 'day_pnl', headerName: 'Day P&L', width: 78, minWidth: 60, maxWidth: 96,
      type: 'numericColumn', headerClass: numericHdr,
      cellClass: (p) => pnlCellClass(p, 'day_pnl'),
      valueFormatter: aggFmtGrid },
    // Day P&L % — one-day return on yesterday's market value (close × qty).
    // NOT cost basis: close × qty is the honest denominator — per-symbol
    // this collapses to change_pct; TOTAL gets a market-value-weighted
    // day return.
    { field: 'day_pnl_pct', headerName: 'Day %', colId: 'day_pnl_pct',
      width: 64, type: 'numericColumn', headerClass: numericHdr,
      cellClass: (p) => `${RA} ${dirCls(p.value)} mp-pnl-cell`,
      valueGetter: (p) => {
        const dpnl = Number(p.data?.day_pnl);
        const prev = Number(p.data?._prev_market_value);
        if (!Number.isFinite(dpnl) || prev <= 0) {
          const cp = Number(p.data?.change_pct);
          return Number.isFinite(cp) ? cp : null;
        }
        return (dpnl / prev) * 100;
      },
      valueFormatter: pctFmtGrid,
      headerTooltip: `Day P&L as % of yesterday's market value (close × qty).` },
    prevCol,
    { field: 'pnl', headerName: 'P&L', width: 78, minWidth: 60, maxWidth: 96,
      type: 'numericColumn', headerClass: numericHdr,
      cellClass: (p) => pnlCellClass(p, 'pnl'),
      valueFormatter: aggFmtGrid },
    { field: 'pnl_pct', headerName: 'P&L %', colId: 'pnl_pct',
      width: 64, type: 'numericColumn', headerClass: numericHdr,
      cellClass: (p) => `${RA} ${dirCls(p.value)} mp-pnl-cell`,
      valueGetter: (p) => {
        const pnl  = Number(p.data?.pnl);
        const cost = Number(p.data?._cost_basis);
        if (!Number.isFinite(pnl) || !(cost > 0)) return null;
        return (pnl / cost) * 100;
      },
      valueFormatter: pctFmtGrid,
      headerTooltip: 'P&L as % of cost basis.' },
    { field: 'qty_net', headerName: 'Qty', width: 56, colId: 'qty_net',
      type: 'numericColumn', headerClass: numericHdr,
      cellClass: RA,
      valueGetter: (p) => {
        if (p.data?._isTotal) return null;
        const q = (Number(p.data?.qty_pos) || 0) + (Number(p.data?.qty_hold) || 0);
        return q === 0 ? null : q;
      },
      valueFormatter: ({ value }) => value == null ? '' : qtyFmt(value) },
    { field: 'lots', headerName: 'Lots', width: 52, colId: 'lots',
      type: 'numericColumn', headerClass: numericHdr,
      cellClass: RA,
      valueGetter: (p) => lotsForRow(p.data),
      valueFormatter: ({ value }) => fmtLots(value),
      headerTooltip: 'Qty in F&O lot units. Holdings on F&O underlyings use the underlying lot; option / futures positions use the contract lot. Cash equity + non-F&O rows read 0.' },
    { field: 'inv_val', headerName: 'Invested', colId: 'inv_val',
      width: 78, type: 'numericColumn', headerClass: numericHdr,
      cellClass: `${RA} cell-muted`,
      valueFormatter: aggFmtGrid,
      headerTooltip: 'Avg cost × held qty — your invested rupees on this holding.' },
    { field: 'cur_val', headerName: 'Value', colId: 'cur_val',
      width: 78, type: 'numericColumn', headerClass: numericHdr,
      cellClass: RA,
      valueFormatter: aggFmtGrid,
      headerTooltip: 'Live LTP × held qty — current market value of this holding.' },
    openCol,
    volCol,
    oiCol,
    acctColTrailing,
  ]);
}

// ─── Summary / Funds column arrays ───────────────────────────────────

/**
 * Positions-summary grid columns (Account | Day P&L | Day % | P&L).
 * Note: the pnlCellClass here is called without a `field` arg because
 * the summary rows have no tick-flash key — they use it only for
 * directional tinting.
 *
 * @param {{
 *   numericHdr: string,
 *   pnlCellClass: (p: any, field?: string) => string,
 *   aggFmtGrid: (p: { value: any }) => string,
 *   pctFmtGrid: (p: { value: any }) => string,
 * }} opts
 * @returns {any[]}
 */
export function mkPosSummaryCols({ numericHdr, pnlCellClass, aggFmtGrid, pctFmtGrid }) {
  return [
    { field: 'account',               headerName: 'Account', width: 76,
      cellClass: 'ag-col-fill' },
    { field: 'day_pnl',               headerName: 'Day P&L', width: 78,
      type: 'numericColumn', headerClass: numericHdr,
      cellClass: pnlCellClass, valueFormatter: aggFmtGrid },
    { field: 'day_change_percentage', headerName: 'Day %',   width: 60,
      type: 'numericColumn', headerClass: numericHdr,
      cellClass: pnlCellClass, valueFormatter: pctFmtGrid },
    { field: 'pnl',                   headerName: 'P&L',     width: 78,
      type: 'numericColumn', headerClass: numericHdr,
      cellClass: pnlCellClass, valueFormatter: aggFmtGrid },
  ];
}

/**
 * Holdings-summary grid columns (Account | Day P&L | Day % | P&L | P&L % | Value | Invested).
 *
 * @param {{
 *   RA: string,
 *   numericHdr: string,
 *   pnlCellClass: (p: any, field?: string) => string,
 *   aggFmtGrid: (p: { value: any }) => string,
 *   pctFmtGrid: (p: { value: any }) => string,
 * }} opts
 * @returns {any[]}
 */
export function mkHoldSummaryCols({ RA, numericHdr, pnlCellClass, aggFmtGrid, pctFmtGrid }) {
  return [
    { field: 'account',               headerName: 'Account', width: 76,
      cellClass: 'ag-col-fill' },
    { field: 'day_pnl',               headerName: 'Day P&L', width: 78,
      type: 'numericColumn', headerClass: numericHdr,
      cellClass: pnlCellClass, valueFormatter: aggFmtGrid },
    { field: 'day_change_percentage', headerName: 'Day %',   width: 60,
      type: 'numericColumn', headerClass: numericHdr,
      cellClass: pnlCellClass, valueFormatter: pctFmtGrid },
    { field: 'pnl',                   headerName: 'P&L',     width: 78,
      type: 'numericColumn', headerClass: numericHdr,
      cellClass: pnlCellClass, valueFormatter: aggFmtGrid },
    { field: 'pnl_percentage',        headerName: 'P&L %',   width: 60,
      type: 'numericColumn', headerClass: numericHdr,
      cellClass: pnlCellClass, valueFormatter: pctFmtGrid },
    { field: 'cur_val',               headerName: 'Value', width: 78,
      type: 'numericColumn', headerClass: numericHdr,
      cellClass: RA, valueFormatter: aggFmtGrid },
    { field: 'inv_val',               headerName: 'Invested', width: 78,
      type: 'numericColumn', headerClass: numericHdr,
      cellClass: RA, valueFormatter: aggFmtGrid },
  ];
}

/**
 * Funds grid columns (Account | Cash | Live Cash | Opt Cash | Avail Margin |
 * Used Margin | Collateral).
 *
 * @param {{
 *   RA: string,
 *   numericHdr: string,
 *   dirCellClass: (p: any) => string,
 *   aggFmtGrid: (p: { value: any }) => string,
 * }} opts
 * @returns {any[]}
 */
export function mkFundsCols({ RA, numericHdr, dirCellClass, aggFmtGrid }) {
  return [
    { field: 'account',        headerName: 'Account',   width: 76,
      cellClass: 'ag-col-fill' },
    { field: 'cash_total',     headerName: 'Cash',      width: 78,
      type: 'numericColumn', headerClass: numericHdr,
      cellClass: dirCellClass, valueFormatter: aggFmtGrid,
      headerTooltip: 'Live cash + cash debited on currently-held long options (= cash you would have if every long option were closed at its entry premium)',
      valueGetter: (/** @type {any} */ p) => {
        const d = p?.data || {};
        const lc  = Number(d.live_cash ?? 0);
        const loc = Number(d._long_opt_cash ?? 0);
        return (lc !== 0 ? lc : Number(d.cash ?? 0)) + loc;
      } },
    { field: 'live_cash',      headerName: 'Live Cash', width: 78,
      type: 'numericColumn', headerClass: numericHdr,
      cellClass: dirCellClass, valueFormatter: aggFmtGrid,
      headerTooltip: 'Current cash — decreases when option premium is debited' },
    { field: '_long_opt_cash', headerName: 'Opt Cash',  width: 78,
      type: 'numericColumn', headerClass: numericHdr,
      cellClass: RA, valueFormatter: aggFmtGrid,
      headerTooltip: 'Cash debited on currently-held long options (sum of avg_price × |qty| across each long CE/PE)' },
    { field: 'avail_margin',   headerName: 'Avail Margin', width: 92,
      type: 'numericColumn', headerClass: numericHdr,
      cellClass: RA, valueFormatter: aggFmtGrid,
      headerTooltip: 'Available margin — net trading buffer' },
    { field: 'used_margin',    headerName: 'Used Margin', width: 90,
      type: 'numericColumn', headerClass: numericHdr,
      cellClass: RA, valueFormatter: aggFmtGrid,
      headerTooltip: 'Margin currently locked against open positions' },
    { field: 'collateral',     headerName: 'Collateral', flex: 1, minWidth: 80,
      type: 'numericColumn', headerClass: numericHdr,
      cellClass: RA, valueFormatter: aggFmtGrid,
      headerTooltip: 'Collateral value from pledged holdings' },
  ];
}
