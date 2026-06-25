"""
Mover universe — the broader symbol set the /pulse Winners/Losers
tabs scan. Sparkline warm pre-fetches past-5-day closes for this
set so the operator never sees the cold-cache lag when a stock
rotates into the top movers.

Source: mirrors frontend/src/lib/data/indexConstituents.js. Keep
the two files in sync when SEBI / NSE rebalance the indices
(quarterly).

Why mirrored (not derived): the warm task runs daily at 00:30
IST; fetching the constituent list from NSE on every run would
add an external dependency to a critical-path background job.
Static lists are explicit, easy to diff, and safe to update.

Industry analog: Bloomberg, TradingView, IBKR all maintain a
similar "candidate universe" of liquid names that get continuous
historical-data refresh in the background; rotation-time latency
on top movers is invisible to the operator.
"""

# NIFTY MIDCAP 100 (representative current constituents).
# Mirror of frontend NIFTY_MIDCAP_100 set. Quarterly rebalance.
NIFTY_MIDCAP_100 = frozenset({
    "AUROPHARMA", "BALKRISIND", "BANDHANBNK", "BHARATFORG", "BHEL",
    "CGPOWER", "COFORGE", "CONCOR", "CUMMININD", "DIXON",
    "FEDERALBNK", "GMRINFRA", "GODREJPROP", "GUJGASLTD", "HDFCAMC",
    "HINDPETRO", "IDEA", "IDFCFIRSTB", "INDHOTEL", "IRCTC",
    "JINDALSTEL", "JSWENERGY", "JUBLFOOD", "LICHSGFIN", "LUPIN",
    "MAXHEALTH", "MFSL", "MOTHERSON", "MPHASIS", "MRF",
    "NMDC", "OBEROIRLTY", "PAGEIND", "PERSISTENT", "PETRONET",
    "PIIND", "POLYCAB", "PRESTIGE", "RECLTD", "SAIL",
    "SCHAEFFLER", "SHRIRAMFIN", "SOLARINDS", "SRF", "SUNDARMFIN",
    "SUNTV", "SUPREMEIND", "SYNGENE", "TATACHEM", "TATACOMM",
    "TATAELXSI", "TATAPOWER", "TATATECH", "TIINDIA", "TORNTPOWER",
    "TRENT", "TVSMOTOR", "UBL", "UNIONBANK", "UPL",
    "VOLTAS", "YESBANK", "ZEEL", "ABCAPITAL", "ACC",
    "ADANIENSOL", "ADANIGREEN", "ADANIPOWER", "ALKEM", "APLLTD",
    "ASHOKLEY", "ASTRAL", "AUBANK", "BANKBARODA", "BERGEPAINT",
    "BIOCON", "BOSCHLTD", "CANBK", "CHOLAFIN",
    "COLPAL", "CROMPTON", "DALBHARAT", "DEEPAKNTR", "DELHIVERY",
    "ESCORTS", "EXIDEIND", "GLAND", "GLENMARK", "GODREJIND",
    "HDFCLIFE", "HONAUT", "IDBI", "IGL", "INDIANB",
    "IPCALAB", "IRFC", "KPITTECH", "LTTS", "M&MFIN",
    "MARICO", "MGL", "MUTHOOTFIN", "NATIONALUM", "OFSS",
})

# NIFTY SMLCAP 100 (representative current constituents).
NIFTY_SMLCAP_100 = frozenset({
    "3MINDIA", "AAVAS", "AEGISLOG", "AFFLE", "AJANTPHARM",
    "AKZOINDIA", "ALLCARGO", "AMARAJABAT", "AMBER", "ANGELONE",
    "APARINDS", "ASTERDM", "AVANTIFEED", "BBTC", "BLUEDART",
    "BLUESTARCO", "BSE", "BSOFT", "CAMS", "CARBORUNIV",
    "CDSL", "CENTRALBK", "CESC", "CHAMBLFERT", "CHENNPETRO",
    "CHOLAHLDNG", "CRISIL", "CYIENT", "DCMSHRIRAM", "DEEPAKFERT",
    "DELTACORP", "EIDPARRY", "EIHOTEL", "ELGIEQUIP", "EMAMILTD",
    "ENGINERSIN", "EPL", "EQUITASBNK", "ERIS", "FACT",
    "FINPIPE", "FIVESTAR", "FLUOROCHEM", "FORTIS", "GESHIP",
    "GICRE", "GILLETTE", "GLAXO", "GMDCLTD", "GNFC",
    "GODFRYPHLP", "GPPL", "GRINDWELL", "GSPL", "HATSUN",
    "HFCL", "HINDCOPPER", "HSCL", "IBULHSGFIN", "IIFL",
    "INDIACEM", "INDIAMART", "INOXWIND", "INTELLECT", "IRB",
    "ISGEC", "JBCHEPHARM", "JINDALSAW", "JKCEMENT", "JKLAKSHMI",
    "JKPAPER", "JKTYRE", "JMFINANCIL", "JSL", "JUBLINGREA",
    "JUSTDIAL", "JWL", "KAJARIACER", "KARURVYSYA", "KEC",
    "KEI", "KFINTECH", "KIMS", "KIRLOSENG", "KNRCON",
    "KSB", "LAOPALA", "LAURUSLABS", "LEMONTREE", "LXCHEM",
    "MAHABANK", "MANAPPURAM", "MASTEK", "MAZDOCK", "METROBRAND",
    "MMTC", "MOIL", "MOTILALOFS", "NATCOPHARM", "NAVA",
})

# F&O underlying universe — major indices + the top F&O stocks the
# operator's Winners/Losers Underlying tab surfaces.
FO_UNDERLYINGS = frozenset({
    # Indices
    "NIFTY 50", "NIFTY BANK", "NIFTY FIN SERVICE", "NIFTY MIDCAP 100",
    "NIFTY NEXT 50", "NIFTY IT", "SENSEX", "BANKEX", "INDIA VIX",
    # Top F&O stocks
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "HINDUNILVR",
    "ITC", "SBIN", "BHARTIARTL", "KOTAKBANK", "LT", "AXISBANK",
    "ASIANPAINT", "MARUTI", "BAJFINANCE", "WIPRO", "HCLTECH", "SUNPHARMA",
    "TITAN", "M&M", "TATAMOTORS", "NESTLEIND", "ULTRACEMCO", "POWERGRID",
    "NTPC", "TATASTEEL", "GRASIM", "TECHM", "HDFCLIFE", "JSWSTEEL",
    "INDUSINDBK", "BAJAJFINSV", "CIPLA", "DRREDDY", "DIVISLAB",
    "ADANIPORTS", "COALINDIA", "HEROMOTOCO", "BAJAJ-AUTO", "EICHERMOT",
    "BPCL", "HINDALCO", "BRITANNIA", "UPL", "APOLLOHOSP", "ONGC",
    "TATACONSUM", "ADANIENT", "SBILIFE", "IOC", "GAIL", "IRCTC",
    "CHOLAFIN", "NAUKRI", "SHRIRAMFIN", "PIDILITIND", "GODREJCP",
    "PIIND", "AUROPHARMA", "LUPIN", "TVSMOTOR", "BANDHANBNK",
    "DLF", "GODREJPROP", "OBEROIRLTY", "PERSISTENT", "COFORGE",
    "MPHASIS", "LTIM", "MARICO", "COLPAL", "DABUR", "BERGEPAINT",
    "SIEMENS", "ABB", "BHEL", "BEL", "HAL", "CGPOWER", "MAZDOCK",
    "VEDL", "NMDC", "SAIL", "JINDALSTEL", "NATIONALUM", "HINDPETRO",
    "PETRONET", "POLYCAB", "HAVELLS", "VOLTAS", "CROMPTON",
    "TRENT", "PAGEIND", "INDIGO", "IRFC", "RECLTD", "PFC", "IDFCFIRSTB",
    "CANBK", "UNIONBANK", "PNB", "IDBI", "YESBANK", "FEDERALBNK",
})

# Exchange resolution — indices carry a space in their name and live
# on NSE / BSE; plain stocks always trade on NSE.
_BSE_INDICES = frozenset({"SENSEX", "BANKEX"})


def _exchange_for(symbol: str) -> str:
    """Return the exchange a sparkline warm should fetch a symbol on.
    BSE for SENSEX/BANKEX; everything else NSE. Indices like
    "NIFTY 50" stay on NSE."""
    return "BSE" if symbol in _BSE_INDICES else "NSE"


def mover_warm_pairs() -> list[tuple[str, str]]:
    """Return deduplicated (tradingsymbol, exchange) pairs covering the
    full mover universe. The order is stable across boots so the warm
    task processes the same symbols in the same order (deterministic
    cache fill timing). Total size is ~250 symbols after dedup."""
    seen: set[tuple[str, str]] = set()
    pairs: list[tuple[str, str]] = []
    # Walk in priority order: indices + F&O largecap first (most likely
    # to surface as movers), then midcap, then smallcap. If a future
    # rate-limit forces us to truncate, the most valuable symbols are
    # already at the top of the list.
    for sym in sorted(FO_UNDERLYINGS):
        key = (sym, _exchange_for(sym))
        if key not in seen:
            seen.add(key); pairs.append(key)
    for sym in sorted(NIFTY_MIDCAP_100):
        key = (sym, "NSE")
        if key not in seen:
            seen.add(key); pairs.append(key)
    for sym in sorted(NIFTY_SMLCAP_100):
        key = (sym, "NSE")
        if key not in seen:
            seen.add(key); pairs.append(key)
    return pairs
