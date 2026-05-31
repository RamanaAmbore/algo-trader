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
