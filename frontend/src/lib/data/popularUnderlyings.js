// Curated priority list of underlyings surfaced at the top of every
// chain-picker dropdown. Single source of truth — imported by both
// the in-page picker on /admin/options and the OrderTicket "Chain"
// tab so they stay in sync.
//
// Order matters: indices first, then top NSE F&O single stocks
// (NSE F&O turnover top by ADV), then MCX commodities. Anything not
// in this list still shows up via the alphabetical `suggestUnderlyings`
// fallback — this list just ensures the frequently-traded names sit
// above the alphabetical dump so "rel" surfaces RELIANCE before
// RELAXO / RELCHEMQ / RELIGARE.
export const POPULAR_UNDERLYINGS = [
  // Indices (always on top for derivatives traders).
  'NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'SENSEX', 'BANKEX',
  // Top-traded NSE F&O single stocks.
  'RELIANCE', 'HDFCBANK', 'INFY', 'ICICIBANK', 'TCS', 'SBIN',
  'AXISBANK', 'KOTAKBANK', 'ITC', 'LT', 'BHARTIARTL', 'HINDUNILVR',
  'ASIANPAINT', 'BAJFINANCE', 'BAJAJFINSV', 'TITAN', 'WIPRO',
  'MARUTI', 'M&M', 'ADANIENT', 'ADANIPORTS', 'TATAMOTORS', 'TATASTEEL',
  'JSWSTEEL', 'COALINDIA', 'POWERGRID', 'NTPC', 'ONGC', 'IOC',
  'BPCL', 'HEROMOTOCO', 'EICHERMOT', 'CIPLA', 'DRREDDY', 'SUNPHARMA',
  'GRASIM', 'ULTRACEMCO', 'TECHM', 'HCLTECH', 'NESTLEIND', 'BRITANNIA',
  'INDUSINDBK', 'BAJAJ-AUTO',
  // MCX commodities (operator's primary segment outside indices).
  'CRUDEOIL', 'CRUDEOILM', 'NATURALGAS', 'NATGASMINI',
  'GOLD', 'GOLDM', 'GOLDMINI', 'GOLDPETAL',
  'SILVER', 'SILVERM', 'SILVERMINI', 'SILVERMIC',
  'COPPER', 'ZINC', 'ZINCMINI', 'LEAD', 'LEADMINI',
  'ALUMINIUM', 'ALUMINI', 'NICKEL',
  'MENTHAOIL', 'COTTON',
];
