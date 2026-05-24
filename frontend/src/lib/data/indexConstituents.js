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

// Quick lookup: classify a tradingsymbol into 'midcap' / 'smallcap' / null.
// Use the equity symbol — strip exchange prefix if passed (NSE:RELIANCE → RELIANCE).
export function classifyByIndex(tradingsymbol) {
  if (!tradingsymbol) return null;
  const sym = String(tradingsymbol).replace(/^.*:/, '').toUpperCase();
  if (NIFTY_MIDCAP_100.has(sym)) return 'midcap';
  if (NIFTY_SMLCAP_100.has(sym)) return 'smallcap';
  return null;
}
