// NIFTY index constituents — used by /dashboard Top Winners/Losers
// to classify holdings into Midcap / Smallcap buckets alongside the
// per-symbol top movers.
//
// Source: NSE index publications (NIFTY MIDCAP 100 / NIFTY SMLCAP 100).
// Both indices are reconstituted quarterly — update this file when
// constituent changes are announced (typically end of Mar / Sep).
//
// Lookups go through symbol membership, not market-cap math, because
// "midcap" / "smallcap" is an index designation in India, not a
// universal classification. A stock can be NIFTY 100 (largecap),
// MIDCAP 100, or SMLCAP 100 — and may shift across rebalances.
// Index-membership is the canonical answer to "is this a midcap?".
//
// Names are NSE tradingsymbols (the same identifier used in
// holdings.tradingsymbol). Sets, not arrays, so membership checks
// are O(1).

// NIFTY MIDCAP 100 — representative current constituents.
// Quarterly rebalance: keep this list synced with NSE publications.
export const NIFTY_MIDCAP_100 = new Set([
  'AUROPHARMA', 'BALKRISIND', 'BANDHANBNK', 'BHARATFORG', 'BHEL',
  'CGPOWER', 'COFORGE', 'CONCOR', 'CUMMINSIND', 'DIXON',
  'FEDERALBNK', 'GMRINFRA', 'GODREJPROP', 'GUJGASLTD', 'HDFCAMC',
  'HINDPETRO', 'IDEA', 'IDFCFIRSTB', 'INDHOTEL', 'IRCTC',
  'JINDALSTEL', 'JSWENERGY', 'JUBLFOOD', 'LICHSGFIN', 'LUPIN',
  'MAXHEALTH', 'MFSL', 'MOTHERSON', 'MPHASIS', 'MRF',
  'NMDC', 'OBEROIRLTY', 'PAGEIND', 'PERSISTENT', 'PETRONET',
  'PIIND', 'POLYCAB', 'PRESTIGE', 'RECLTD', 'SAIL',
  'SCHAEFFLER', 'SHRIRAMFIN', 'SOLARINDS', 'SRF', 'SUNDARMFIN',
  'SUNTV', 'SUPREMEIND', 'SYNGENE', 'TATACHEM', 'TATACOMM',
  'TATAELXSI', 'TATAPOWER', 'TATATECH', 'TIINDIA', 'TORNTPOWER',
  'TRENT', 'TVSMOTOR', 'UBL', 'UNIONBANK', 'UPL',
  'VOLTAS', 'YESBANK', 'ZEEL', 'ABCAPITAL', 'ACC',
  'ADANIENSOL', 'ADANIGREEN', 'ADANIPOWER', 'ALKEM', 'APLLTD',
  'ASHOKLEY', 'ASTRAL', 'AUBANK', 'BANKBARODA', 'BERGEPAINT',
  'BHARATFORG', 'BIOCON', 'BOSCHLTD', 'CANBK', 'CHOLAFIN',
  'COLPAL', 'CROMPTON', 'DALBHARAT', 'DEEPAKNTR', 'DELHIVERY',
  'ESCORTS', 'EXIDEIND', 'GLAND', 'GLENMARK', 'GODREJIND',
  'HDFCLIFE', 'HONAUT', 'IDBI', 'IGL', 'INDIANB',
  'IPCALAB', 'IRFC', 'KPITTECH', 'LTTS', 'M&MFIN',
  'MARICO', 'MGL', 'MUTHOOTFIN', 'NATIONALUM', 'OFSS',
]);

// NIFTY SMLCAP 100 — representative current constituents.
export const NIFTY_SMLCAP_100 = new Set([
  '3MINDIA', 'AAVAS', 'AEGISLOG', 'AFFLE', 'AJANTPHARM',
  'AKZOINDIA', 'ALLCARGO', 'AMARAJABAT', 'AMBER', 'ANGELONE',
  'APARINDS', 'ASTERDM', 'AVANTIFEED', 'BBTC', 'BLUEDART',
  'BLUESTARCO', 'BSE', 'BSOFT', 'CAMS', 'CARBORUNIV',
  'CDSL', 'CENTRALBK', 'CESC', 'CHAMBLFERT', 'CHENNPETRO',
  'CHOLAHLDNG', 'CRISIL', 'CYIENT', 'DCMSHRIRAM', 'DEEPAKFERT',
  'DELTACORP', 'EIDPARRY', 'EIHOTEL', 'ELGIEQUIP', 'EMAMILTD',
  'ENGINERSIN', 'EPL', 'EQUITASBNK', 'ERIS', 'FACT',
  'FINPIPE', 'FIVESTAR', 'FLUOROCHEM', 'FORTIS', 'GESHIP',
  'GICRE', 'GILLETTE', 'GLAXO', 'GMDCLTD', 'GNFC',
  'GODFRYPHLP', 'GPPL', 'GRINDWELL', 'GSPL', 'HATSUN',
  'HFCL', 'HINDCOPPER', 'HSCL', 'IBULHSGFIN', 'IIFL',
  'INDIACEM', 'INDIAMART', 'INOXWIND', 'INTELLECT', 'IRB',
  'ISGEC', 'JBCHEPHARM', 'JINDALSAW', 'JKCEMENT', 'JKLAKSHMI',
  'JKPAPER', 'JKTYRE', 'JMFINANCIL', 'JSL', 'JUBLINGREA',
  'JUSTDIAL', 'JWL', 'KAJARIACER', 'KARURVYSYA', 'KEC',
  'KEI', 'KFINTECH', 'KIMS', 'KIRLOSENG', 'KNRCON',
  'KSB', 'LAOPALA', 'LAURUSLABS', 'LEMONTREE', 'LXCHEM',
  'MAHABANK', 'MANAPPURAM', 'MASTEK', 'MAZDOCK', 'METROBRAND',
  'MMTC', 'MOIL', 'MOTILALOFS', 'NATCOPHARM', 'NAVA',
]);

// F&O underlying universe — used by the /dashboard Top Winners/Losers
// Underlying tab to surface top movers across the broader exchange
// (not just symbols in the user's positions). Mix of the broad F&O
// stock list + the major indices that also trade as derivatives.
// Quarterly review when SEBI / NSE rotate the F&O eligible list.
export const FO_UNDERLYINGS = new Set([
  // Major indices
  'NIFTY 50', 'NIFTY BANK', 'NIFTY FIN SERVICE', 'NIFTY MIDCAP 100',
  'NIFTY NEXT 50', 'NIFTY IT', 'SENSEX', 'BANKEX', 'INDIA VIX',
  // Top F&O stocks (representative subset of the ~200 SEBI-eligible names)
  'RELIANCE', 'TCS', 'HDFCBANK', 'INFY', 'ICICIBANK', 'HINDUNILVR',
  'ITC', 'SBIN', 'BHARTIARTL', 'KOTAKBANK', 'LT', 'AXISBANK',
  'ASIANPAINT', 'MARUTI', 'BAJFINANCE', 'WIPRO', 'HCLTECH', 'SUNPHARMA',
  'TITAN', 'M&M', 'TATAMOTORS', 'NESTLEIND', 'ULTRACEMCO', 'POWERGRID',
  'NTPC', 'TATASTEEL', 'GRASIM', 'TECHM', 'HDFCLIFE', 'JSWSTEEL',
  'INDUSINDBK', 'BAJAJFINSV', 'CIPLA', 'DRREDDY', 'DIVISLAB',
  'ADANIPORTS', 'COALINDIA', 'HEROMOTOCO', 'BAJAJ-AUTO', 'EICHERMOT',
  'BPCL', 'HINDALCO', 'BRITANNIA', 'UPL', 'APOLLOHOSP', 'ONGC',
  'TATACONSUM', 'ADANIENT', 'SBILIFE', 'IOC', 'GAIL', 'IRCTC',
  'CHOLAFIN', 'NAUKRI', 'SHRIRAMFIN', 'PIDILITIND', 'GODREJCP',
  'PIIND', 'AUROPHARMA', 'LUPIN', 'TVSMOTOR', 'BANDHANBNK',
  'DLF', 'GODREJPROP', 'OBEROIRLTY', 'PERSISTENT', 'COFORGE',
  'MPHASIS', 'LTIM', 'MARICO', 'COLPAL', 'DABUR', 'BERGEPAINT',
  'SIEMENS', 'ABB', 'BHEL', 'BEL', 'HAL', 'CGPOWER', 'MAZDOCK',
  'VEDL', 'NMDC', 'SAIL', 'JINDALSTEL', 'NATIONALUM', 'HINDPETRO',
  'PETRONET', 'BPCL', 'POLYCAB', 'HAVELLS', 'VOLTAS', 'CROMPTON',
  'TRENT', 'PAGEIND', 'INDIGO', 'IRFC', 'RECLTD', 'PFC', 'IDFCFIRSTB',
  'CANBK', 'UNIONBANK', 'PNB', 'IDBI', 'YESBANK', 'FEDERALBNK',
]);

// Quick lookup: classify a tradingsymbol into 'midcap' / 'smallcap' / null.
// Use the equity symbol — strip exchange prefix if passed (NSE:RELIANCE → RELIANCE).
export function classifyByIndex(tradingsymbol) {
  if (!tradingsymbol) return null;
  const sym = String(tradingsymbol).replace(/^.*:/, '').toUpperCase();
  if (NIFTY_MIDCAP_100.has(sym)) return 'midcap';
  if (NIFTY_SMLCAP_100.has(sym)) return 'smallcap';
  return null;
}

// Map an equity symbol to the Kite quote key. Indices use the
// `NSE:NIFTY 50` style; plain stocks just get the `NSE:` prefix.
// Mirrors backend.api.algo.derivatives.underlying_ltp_key, kept
// frontend-local so the dashboard doesn't need a round-trip per
// symbol.
const INDEX_QUOTE_KEYS = {
  'NIFTY 50':            'NSE:NIFTY 50',
  'NIFTY BANK':          'NSE:NIFTY BANK',
  'NIFTY FIN SERVICE':   'NSE:NIFTY FIN SERVICE',
  'NIFTY MIDCAP 100':    'NSE:NIFTY MIDCAP 100',
  'NIFTY NEXT 50':       'NSE:NIFTY NEXT 50',
  'NIFTY IT':            'NSE:NIFTY IT',
  'SENSEX':              'BSE:SENSEX',
  'BANKEX':              'BSE:BANKEX',
  'INDIA VIX':           'NSE:INDIA VIX',
};
export function quoteKeyFor(symbol) {
  const k = INDEX_QUOTE_KEYS[symbol];
  if (k) return k;
  return `NSE:${symbol}`;
}

// Indices subset of FO_UNDERLYINGS — used by the Pulse Winners /
// Losers tabs to split "Underlying" (indices the operator trades
// against) from "Large Cap" (F&O-eligible stocks). Any name carrying
// a space is an index ("NIFTY 50", "NIFTY BANK", …); stocks are
// always single-token tradingsymbols. INDIA VIX is included as an
// index for completeness even though it doesn't trade.
export const FO_INDICES = new Set(
  [...FO_UNDERLYINGS].filter(n => n.includes(' '))
);
export const FO_LARGECAP_STOCKS = new Set(
  [...FO_UNDERLYINGS].filter(n => !n.includes(' '))
);

// Pre-built quote-key arrays for the four universes — saves a map
// allocation on every poll.
export const FO_QUOTE_KEYS      = [...FO_UNDERLYINGS].map(quoteKeyFor);
export const INDICES_QUOTE_KEYS = [...FO_INDICES].map(quoteKeyFor);
export const LARGECAP_QUOTE_KEYS = [...FO_LARGECAP_STOCKS].map(quoteKeyFor);
export const MIDCAP_QUOTE_KEYS  = [...NIFTY_MIDCAP_100].map(quoteKeyFor);
export const SMLCAP_QUOTE_KEYS  = [...NIFTY_SMLCAP_100].map(quoteKeyFor);

// Reverse lookup: quote key → display symbol (strips exchange prefix
// for stocks; keeps the full index name for "NSE:NIFTY 50"-style
// keys). Lets the dashboard render a clean symbol cell from quote
// responses without parsing each key separately.
export function symbolFromQuoteKey(key) {
  if (!key) return '';
  const k = String(key);
  // Index keys carry a space ("NSE:NIFTY 50") — keep the right side
  // intact so the index name renders fully.
  const m = k.split(':');
  return m.length > 1 ? m.slice(1).join(':') : k;
}
