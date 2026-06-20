/**
 * Resolve an underlying name to a tradeable tradingsymbol + exchange.
 *
 * Mirrors the backend `derivatives.underlying_ltp_key` /
 * `is_mcx_underlying` + `findNearestFuture` chain.
 *
 *   Indices (NIFTY, BANKNIFTY, …)    → spot tradingsymbol on NSE/BSE
 *   MCX commodities (GOLD, CRUDEOIL) → nearest expiry future
 *   CDS currencies (USDINR)           → nearest expiry future
 *   Everything else                   → NSE: <name> spot
 *
 * findNearestFut is injected so the function stays pure-data (no
 * import of instruments.js from this layer — the instruments cache
 * has a loading lifecycle the caller owns).
 *
 * @param {string} name
 * @param {((u:string) => any) | null | undefined} findNearestFut
 * @returns {{
 *   tradingsymbol: string, exchange: string, quoteKey: string,
 *   underlying_group: string, kind: 'spot'|'fut'|'mcx',
 * } | null}
 */
export function resolveUnderlying(name, findNearestFut) {
  const n = String(name || '').toUpperCase();
  if (!n) return null;
  const idx = INDEX_LTP_KEY[n];
  if (idx) {
    return {
      tradingsymbol: idx.tradingsymbol,
      exchange: idx.exchange,
      quoteKey: `${idx.exchange}:${idx.tradingsymbol}`,
      underlying_group: n,
      kind: 'spot',
    };
  }
  if (MCX_COMMODITIES.has(n)) {
    const fut = findNearestFut?.(n);
    if (fut?.s && fut?.e) {
      return {
        tradingsymbol: fut.s,
        exchange: fut.e,
        quoteKey: `${fut.e}:${fut.s}`,
        underlying_group: n,
        kind: 'fut',
      };
    }
    // MCX commodity with no resolvable nearest future — fall through
    // to a stub anchor (commodity name as tradingsymbol, no live
    // quote). Better than dropping the anchor entirely: GOLDM /
    // GOLDPETAL / similar mini contracts may have option positions
    // even when MCX hasn't published a matching nearest-future
    // tradingsymbol the instruments cache can find.
    return {
      tradingsymbol: n,
      exchange: 'MCX',
      quoteKey: `MCX:${n}`,   // synthetic; quote will silently miss
      underlying_group: n,
      kind: 'mcx',
    };
  }
  if (CDS_CURRENCIES.has(n)) {
    const fut = findNearestFut?.(n);
    if (fut?.s && fut?.e) {
      return {
        tradingsymbol: fut.s,
        exchange: fut.e,
        quoteKey: `${fut.e}:${fut.s}`,
        underlying_group: n,
        kind: 'fut',
      };
    }
    return null;
  }
  return {
    tradingsymbol: n,
    exchange: 'NSE',
    quoteKey: `NSE:${n}`,
    underlying_group: n,
    kind: 'spot',
  };
}

// Mirrors the backend `derivatives.underlying_ltp_key` index map.
export const INDEX_LTP_KEY = {
  NIFTY:      { tradingsymbol: 'NIFTY 50',         exchange: 'NSE' },
  BANKNIFTY:  { tradingsymbol: 'NIFTY BANK',       exchange: 'NSE' },
  FINNIFTY:   { tradingsymbol: 'NIFTY FIN SERVICE', exchange: 'NSE' },
  MIDCPNIFTY: { tradingsymbol: 'NIFTY MID SELECT', exchange: 'NSE' },
  SENSEX:     { tradingsymbol: 'SENSEX',           exchange: 'BSE' },
  BANKEX:     { tradingsymbol: 'BANKEX',           exchange: 'BSE' },
};

export const MCX_COMMODITIES = new Set([
  'CRUDEOIL', 'CRUDEOILM', 'NATURALGAS', 'NATGASMINI',
  'GOLD', 'GOLDM', 'GOLDMINI', 'GOLDPETAL', 'GOLDGUINEA',
  'SILVER', 'SILVERM', 'SILVERMINI', 'SILVERMIC',
  'COPPER', 'ZINC', 'ZINCMINI', 'LEAD', 'LEADMINI',
  'ALUMINIUM', 'ALUMINI', 'NICKEL',
  'MENTHAOIL', 'COTTON', 'CASTORSEED', 'KAPAS', 'CARDAMOM',
]);

export const CDS_CURRENCIES = new Set(['USDINR']);

// Kite spot-index quote-keys → tradeable F&O underlying root. The chart
// + order modals use the same translation so e.g. "NIFTY 50" → "NIFTY"
// → NIFTY*FUT instead of trying to place an index spot order / fetch a
// chart by the non-tradeable spot key.
export const KITE_INDEX_QUOTE_KEY_TO_ROOT = {
  'NIFTY 50':           'NIFTY',
  'NIFTY BANK':         'BANKNIFTY',
  'NIFTY FIN SERVICE':  'FINNIFTY',
  'NIFTY MID SELECT':   'MIDCPNIFTY',
  'NIFTY NEXT 50':      'NIFTYNXT50',
  'SENSEX':             'SENSEX',
  'BANKEX':             'BANKEX',
};

/**
 * Translate any anchor (Kite spot-index quote-key, MCX commodity root,
 * CDS currency root, or already-tradeable tradingsymbol) into the
 * nearest-month tradeable contract. Returns the original string when
 * the anchor doesn't need translation (e.g. RELIANCE, NIFTY26JUNFUT).
 *
 *   "NIFTY 50"      → "NIFTY26JUNFUT"
 *   "CRUDEOIL"      → "CRUDEOILM26JUNFUT"
 *   "GOLD"          → "GOLD26JUNFUT"
 *   "USDINR"        → "USDINR26JUNFUT"
 *   "RELIANCE"      → "RELIANCE"
 *   "NIFTY26JUNFUT" → "NIFTY26JUNFUT"
 *
 * Returns null when the instruments cache is cold AND the anchor needs
 * translation — caller falls back to the anchor itself.
 *
 * @param {string} anchor
 * @param {((root:string) => any) | null | undefined} findNearestFut
 * @returns {string | null}
 */
export function resolveAnchorToTradeable(anchor, findNearestFut) {
  const upper = String(anchor || '').toUpperCase();
  if (!upper) return null;
  // Quote-key path (e.g. "NIFTY 50" → root "NIFTY").
  const indexRoot   = KITE_INDEX_QUOTE_KEY_TO_ROOT[upper];
  // Already-a-root path (e.g. "NIFTY", "BANKNIFTY") — when the
  // operator's setting carries the bare root the dict lookup misses,
  // so we ALSO match against INDEX_LTP_KEY whose keys ARE the roots.
  // Without this, default_symbol="NIFTY" returned "NIFTY" unchanged
  // and the modal opened on a non-tradeable underlying name.
  const isIndexRoot = !!INDEX_LTP_KEY[upper];
  const isMcx       = MCX_COMMODITIES.has(upper);
  const isCds       = CDS_CURRENCIES.has(upper);
  const root        = indexRoot
                   || (isIndexRoot ? upper : null)
                   || (isMcx || isCds ? upper : null);
  if (!root) return upper;  // already tradeable (equity or specific contract)
  const fut = findNearestFut?.(root);
  return fut?.s ? String(fut.s) : null;
}
