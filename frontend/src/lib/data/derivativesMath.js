/**
 * derivativesMath.js — Pure math helpers for the derivatives workspace.
 *
 * All functions are pure (no Svelte reactive state, no DOM access).
 * The $derived.by() shells in +page.svelte read reactive state, run any
 * untrack() wraps, then call into these helpers with plain values.
 *
 * Extracted from frontend/src/routes/(algo)/admin/derivatives/+page.svelte
 * to reduce cyclomatic complexity in the three hotspots:
 *   expiryCloseAnalysis (cc=126), _byUnderlyingTotals (cc=56),
 *   and the shared matcher closures repeated across 5+ $derived.by blocks.
 */

// ─────────────────────────────────────────────────────────────────────────────
// Shared filter factories (eliminate repeated closure boilerplate)
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Build an account-match predicate from the page's selectedAccounts array.
 * Empty array = all accounts pass (fail-open / no filter).
 *
 * @param {string[]} selectedAccounts
 * @returns {(acct: string|null|undefined) => boolean}
 */
export function buildAcctMatcher(selectedAccounts) {
  const wanted = new Set(
    selectedAccounts.map(a => String(a || '').trim().toUpperCase())
  );
  if (wanted.size === 0) return () => true;
  return (acct) => wanted.has(String(acct || '').trim().toUpperCase());
}

/**
 * Build a strategy-symbol-match predicate.
 * Null strategyId = no filter (fail-open).
 * Empty openSymbols set = fail-open (still loading / empty strategy).
 *
 * @param {string|number|null|undefined} strategyId
 * @param {Set<string>} openSymbols
 * @returns {(sym: string|null|undefined) => boolean}
 */
export function buildStrategyMatcher(strategyId, openSymbols) {
  if (strategyId == null) return () => true;
  if (!openSymbols || openSymbols.size === 0) return () => true;
  return (sym) => openSymbols.has(String(sym || '').toUpperCase());
}

// ─────────────────────────────────────────────────────────────────────────────
// Expiry-band analysis (extracted inner body of expiryCloseAnalysis)
// ─────────────────────────────────────────────────────────────────────────────

const BAND_ORDER = { close: 0, netted: 1, otm: 2 };

/**
 * Annotate a set of option candidates with ITM/OTM metadata and spot.
 * Skips futures, zero-qty rows (when no expiry filter), and drafts.
 *
 * @param {{
 *   candidates: any[],
 *   spot: number,
 *   expFilter: string[],
 *   mcxUnderlyings: Set<string>,
 *   legAnalytics: Record<string,any>,
 *   getInstrument: (sym: string) => any,
 * }} params
 * @returns {any[]} annotated rows
 */
export function annotateOptionCandidates({
  candidates, spot, expFilter, mcxUnderlyings, legAnalytics, getInstrument,
}) {
  const annotated = [];
  for (const c of candidates) {
    const qty = Number(c.qty || 0);
    if (qty === 0 && !expFilter.length) continue;
    if (c.source === 'draft') continue;
    const inst = getInstrument(String(c.symbol || '').toUpperCase());
    if (!inst) continue;
    const optType = inst.t;
    if (optType !== 'CE' && optType !== 'PE') continue;
    const strike = Number(inst.k || 0);
    if (!strike) continue;
    const underlying = String(inst.u || '').toUpperCase();
    const expiry = String(inst.x || '');
    const segment = mcxUnderlyings.has(underlying) ? 'commodity' : 'equity';
    const isITM = optType === 'CE' ? spot > strike : spot < strike;
    const lg = legAnalytics[c.symbol];
    const theta = Number(lg?.greeks?.theta ?? 0) || 0;
    const otmDist = isITM ? 0
      : (optType === 'CE' ? strike - spot : spot - strike);
    annotated.push({
      ...c,
      _strike: strike,
      _underlying: underlying,
      _expiry: expiry,
      _optType: optType,
      _segment: segment,
      _isITM: isITM,
      _spot: spot,
      _qty: qty,
      _theta: theta,
      _otmDist: otmDist,
    });
  }
  return annotated;
}

/**
 * Check whether two annotated option rows can form a netting pair.
 * Rule 1+2: same opt type, opposite sign qty.
 * Rule 3+4: different opt type, same sign qty.
 *
 * @param {any} A
 * @param {any} B
 * @param {Map<any,number>} remaining signed qty map
 * @returns {boolean}
 */
function _canPair(A, B, remaining) {
  const aq = remaining.get(A) || 0;
  const bq = remaining.get(B) || 0;
  if (aq === 0 || bq === 0) return false;
  const aSign = Math.sign(aq);
  const bSign = Math.sign(bq);
  if (A._optType === B._optType && aSign !== bSign) return true;
  if (A._optType !== B._optType && aSign === bSign) return true;
  return false;
}

/**
 * Perform greedy theta-priority netting for one (account, underlying, expiry)
 * group of ITM commodity rows. Returns netted rows with pair metadata and
 * residual (to-close) rows.
 *
 * @param {any[]} grp annotated ITM commodity rows for one key
 * @returns {{ nettedRows: Array<{row:any,consumedQty:number,pairId:string,splitNote:string}>, residuals: Array<{row:any,qty:number}> }}
 */
export function netMcxGroup(grp) {
  const sortedAbs = grp.slice().sort(
    (a, b) => Math.abs(b._theta || 0) - Math.abs(a._theta || 0)
  );

  const remaining = new Map();
  for (const r of sortedAbs) remaining.set(r, r._qty);

  /** @type {Array<{row:any, consumedQty:number, pairId:string, splitNote:string}>} */
  const nettedRows = [];
  let pairCounter = 0;

  for (const A of sortedAbs) {
    let aq = remaining.get(A) || 0;
    while (aq !== 0) {
      let bestB = null;
      let bestT = -1;
      for (const B of sortedAbs) {
        if (B === A) continue;
        if (!_canPair(A, B, remaining)) continue;
        const t = Math.abs(B._theta || 0);
        if (t > bestT) { bestB = B; bestT = t; }
      }
      if (!bestB) break;
      pairCounter++;
      const pairId = `N${pairCounter}`;
      const bq = remaining.get(bestB) || 0;
      const netAmt = Math.min(Math.abs(aq), Math.abs(bq));
      const newAq = aq - netAmt * Math.sign(aq);
      const newBq = bq - netAmt * Math.sign(bq);
      remaining.set(A, newAq);
      remaining.set(bestB, newBq);
      const aSplit = newAq !== 0;
      const bSplit = newBq !== 0;
      nettedRows.push({
        row: A,
        consumedQty: netAmt * Math.sign(aq),
        pairId,
        splitNote: aSplit ? `split ${aq > 0 ? '+' : ''}${aq}→${netAmt * Math.sign(aq)}` : '',
      });
      nettedRows.push({
        row: bestB,
        consumedQty: netAmt * Math.sign(bq),
        pairId,
        splitNote: bSplit ? `split ${bq > 0 ? '+' : ''}${bq}→${netAmt * Math.sign(bq)}` : '',
      });
      aq = remaining.get(A) || 0;
    }
  }

  const residuals = [];
  for (const r of sortedAbs) {
    const q = remaining.get(r) || 0;
    if (q !== 0) residuals.push({ row: r, qty: q });
  }

  return { nettedRows, residuals };
}

/**
 * Assign pair-tint color-cycle indices (0..4) to NETTED rows in-place.
 * Adjacent pairs always get distinct tint indices.
 *
 * @param {any[]} arr array of band-annotated rows (mutates _pairTint)
 */
export function assignPairTints(arr) {
  const map = new Map();
  let cycle = 0;
  for (const r of arr) {
    if (r._band !== 'netted') continue;
    const pid = r._pairId || '';
    if (!pid) continue;
    if (!map.has(pid)) {
      map.set(pid, cycle % 5);
      cycle++;
    }
    r._pairTint = map.get(pid);
  }
}

/**
 * Comparator for band-sorted rows: BAND_ORDER first, then pairId within
 * NETTED (so N1-A and N1-B are adjacent), then account+symbol alpha.
 *
 * @param {any} a
 * @param {any} b
 * @returns {number}
 */
export function bandRowComparator(a, b) {
  const bo = (BAND_ORDER[a._band] ?? 9) - (BAND_ORDER[b._band] ?? 9);
  if (bo !== 0) return bo;
  if (a._band === 'netted' && b._band === 'netted') {
    const ap = a._pairId || '';
    const bp = b._pairId || '';
    if (ap !== bp) return ap < bp ? -1 : 1;
  }
  const ac = String(a.account || '').localeCompare(String(b.account || ''));
  if (ac !== 0) return ac;
  return String(a.symbol || '').localeCompare(String(b.symbol || ''));
}

/**
 * Compute the expiry-band analysis from an annotated option-candidate set.
 * Pure: takes annotated rows (output of annotateOptionCandidates), returns
 * the { equity, commodity } structure the page renders.
 *
 * Delegates MCX netting to netMcxGroup, pair-tinting to assignPairTints.
 *
 * @param {{ annotated: any[] }} params
 * @returns {{ equity: any[], commodity: any[] }}
 */
export function computeExpiryBands({ annotated }) {
  /** @type {{equity: any[], commodity: any[]}} */
  const result = { equity: [], commodity: [] };

  // Equity: ITM → close, OTM → otm. No netting on NFO.
  let _eqCloseCounter = 0;
  for (const r of annotated) {
    if (r._segment !== 'equity') continue;
    if (r._isITM) {
      _eqCloseCounter++;
      result.equity.push({
        ...r,
        _band: 'close',
        _closeId: `C${_eqCloseCounter}`,
        _reason: 'ITM equity — physical settlement risk',
      });
    } else {
      result.equity.push({
        ...r,
        _band: 'otm',
        _reason: `OTM by ₹${Math.round(r._otmDist).toLocaleString('en-IN')}`,
      });
    }
  }

  // Commodity OTM rows (non-ITM) emit directly.
  for (const r of annotated) {
    if (r._segment !== 'commodity' || r._isITM) continue;
    result.commodity.push({
      ...r,
      _band: 'otm',
      _reason: `OTM by ₹${Math.round(r._otmDist).toLocaleString('en-IN')}`,
    });
  }

  // Group ITM commodity rows by (account, underlying, expiry).
  /** @type {Record<string, any[]>} */
  const mcxGroups = {};
  for (const r of annotated) {
    if (r._segment !== 'commodity' || !r._isITM) continue;
    const key = `${r.account || ''}|${r._underlying}|${r._expiry}`;
    (mcxGroups[key] ??= []).push(r);
  }

  for (const grp of Object.values(mcxGroups)) {
    const { nettedRows, residuals } = netMcxGroup(grp);

    for (const { row, consumedQty, pairId, splitNote } of nettedRows) {
      result.commodity.push({
        ...row,
        _band: 'netted',
        _pairId: pairId,
        _residualQty: consumedQty,
        _reason: splitNote
          ? `Netted (${splitNote})`
          : `Netted — broker settles at expiry`,
      });
    }

    let closeCounter = 0;
    for (const { row, qty } of residuals) {
      closeCounter++;
      result.commodity.push({
        ...row,
        _band: 'close',
        _closeId: `C${closeCounter}`,
        _residualQty: qty,
        _reason: `Unhedged ITM commodity (residual qty ${qty > 0 ? '+' : ''}${qty})`,
      });
    }
  }

  result.equity.sort(bandRowComparator);
  result.commodity.sort(bandRowComparator);
  assignPairTints(result.commodity);
  assignPairTints(result.equity);

  return result;
}

// ─────────────────────────────────────────────────────────────────────────────
// Per-underlying rollup (extracted inner body of _byUnderlyingTotals)
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Build a per-underlying snapshot rollup from positions + holdings.
 * Returns an array of group objects sorted by |pnl_with| descending.
 *
 * @param {{
 *   positions: any[],
 *   holdings: any[],
 *   wantedSource: 'live'|'sim',
 *   matchAccount: (acct: string|null|undefined) => boolean,
 *   matchStrategy: (sym: string|null|undefined) => boolean,
 *   filterQ: string,
 *   decomposeSymbol: (sym: string) => { root: string },
 *   targetsForProxy: (sym: string) => string[],
 *   getOptionUnderlyingLot: (root: string) => number,
 *   baseDayPnlForPosition: (p: any) => number,
 * }} params
 * @returns {Array<{
 *   underlying: string,
 *   qty_fno: number, qty_eq: number,
 *   legs_with: number, legs_without: number,
 *   pnl_with: number, pnl_without: number,
 *   day_with: number, day_without: number,
 * }>}
 */
export function rollupByUnderlying({
  positions, holdings, wantedSource,
  matchAccount, matchStrategy,
  filterQ,
  decomposeSymbol, targetsForProxy, getOptionUnderlyingLot,
  baseDayPnlForPosition,
}) {
  const groups = new Map();
  const ensure = (root) => {
    let g = groups.get(root);
    if (!g) {
      g = {
        underlying: root,
        qty_fno: 0, qty_eq: 0,
        legs_with: 0, legs_without: 0,
        pnl_with: 0, pnl_without: 0,
        day_with: 0, day_without: 0,
      };
      groups.set(root, g);
    }
    return g;
  };

  for (const _p of positions) {
    const p = /** @type {any} */ (_p);
    if (p.source !== wantedSource) continue;
    if (!matchAccount(p.account)) continue;
    if (!matchStrategy(p.symbol || p.tradingsymbol)) continue;
    const sym = String(p.symbol || p.tradingsymbol || '').toUpperCase();
    if (!sym) continue;
    const isFut = /FUT$/i.test(sym);
    const isOpt = /(CE|PE)$/i.test(sym);
    if (!isFut && !isOpt) continue;
    const root = (decomposeSymbol(sym).root || sym).toUpperCase();
    if (!root) continue;
    const g = ensure(root);
    const qty = Number(p.quantity ?? p.qty) || 0;
    const pnl = Number(p.pnl) || 0;
    const day = baseDayPnlForPosition(p);
    g.qty_fno      += qty;
    g.legs_with++;
    g.legs_without++;
    g.pnl_with     += pnl;
    g.pnl_without  += pnl;
    g.day_with     += day;
    g.day_without  += day;
  }

  for (const _h of holdings) {
    const h = /** @type {any} */ (_h);
    if (!matchAccount(h.account)) continue;
    const sym = String(h.symbol || h.tradingsymbol || '').toUpperCase();
    if (!sym) continue;
    const qty = Number(h.opening_qty ?? h.opening_quantity ?? h.quantity ?? h.qty) || 0;
    const pnl = Number(h.pnl) || 0;
    const day = Number(h.day_change_val) || 0;
    const _targets = targetsForProxy(sym);
    const credits = _targets.length ? _targets : [sym];
    for (const root of credits) {
      const g = ensure(root);
      g.qty_eq += qty;
      const _lot = getOptionUnderlyingLot(root);
      if (_lot > 0) {
        g.legs_with += qty / _lot;
        g.pnl_with  += pnl;
        g.day_with  += day;
      }
    }
  }

  if (filterQ) {
    const q = filterQ.toUpperCase();
    for (const [k] of groups) if (!k.includes(q)) groups.delete(k);
  }

  // Hide eq-only rows (no F&O on this underlying).
  for (const [k, g] of groups) {
    if (g.legs_without === 0) groups.delete(k);
  }

  return Array.from(groups.values()).sort(
    (a, b) => Math.abs(b.pnl_with) - Math.abs(a.pnl_with)
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Per-root reduce (extracted inner body of _perRootReduce)
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Walk F&O positions and accumulate a per-root value via an accessor.
 * Equity holdings are excluded — they surface via separate _h* maps.
 *
 * @param {{
 *   positions: any[],
 *   wantedSource: 'live'|'sim',
 *   matchAccount: (acct: string|null|undefined) => boolean,
 *   matchStrategy: (sym: string|null|undefined) => boolean,
 *   decomposeSymbol: (sym: string) => { root: string },
 *   getSpot: (root: string, p: any) => number|null,
 *   accessor: (c: any, spot: number|null) => number|null|undefined,
 * }} params
 * @returns {Record<string, number>} root → summed value
 */
export function perRootReduce({
  positions, wantedSource,
  matchAccount, matchStrategy,
  decomposeSymbol, getSpot,
  accessor,
}) {
  /** @type {Record<string, number>} */
  const out = {};
  for (const _p of positions) {
    const p = /** @type {any} */ (_p);
    if (p.source !== wantedSource) continue;
    if (!matchAccount(p.account)) continue;
    if (!matchStrategy(p.symbol || p.tradingsymbol || '')) continue;
    const sym = String(p.symbol || p.tradingsymbol || '').toUpperCase();
    if (!sym) continue;
    const isFut = /FUT$/i.test(sym);
    const isOpt = /(CE|PE)$/i.test(sym);
    if (!isFut && !isOpt) continue;
    const c = { ...p, kind: isOpt ? 'opt' : 'fut' };
    const root = (decomposeSymbol(sym).root || sym).toUpperCase();
    if (!root) continue;
    const spot = getSpot(root, p);
    const v = accessor(c, spot);
    if (v == null || !isFinite(Number(v))) continue;
    out[root] = (out[root] || 0) + Number(v);
  }
  return out;
}
