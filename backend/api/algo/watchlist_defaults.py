"""
Default watchlist seed lists. Every new user gets:

  1. Default        — empty, the operator's working list.
  2. Markets        — major Indian indices + MCX commodities so the
                      operator sees something familiar on first login.

The Markets list is editable — operators can delete / reorder / add to
it just like any user-created list. The seeding is a one-shot at user
creation time; it does NOT reconcile or re-add removed symbols on
subsequent logins.

MCX commodity symbols are stored as the **plain commodity name**
(e.g. `GOLD`, `CRUDEOIL`) — the quote endpoint resolves these to the
current near-month future at fetch time via the instrument cache so the
operator never has to update them month-over-month. Index symbols
(NIFTY 50 etc.) are stable.
"""

# Each entry: (tradingsymbol, exchange). The order here is the
# `sort_order` the symbols land at — index instruments first, then
# commodities, so the user's eye lands on the broad market gauge first.
MARKETS_DEFAULT: list[tuple[str, str]] = [
    # Indices — quote endpoint maps these via the broker.quote() key
    # form `NSE:NIFTY 50` etc. Stable across months.
    ("NIFTY 50",            "NSE"),
    ("NIFTY BANK",          "NSE"),
    ("NIFTY MIDCAP 100",    "NSE"),
    ("NIFTY SMLCAP 100",    "NSE"),   # Kite's abbreviated key (not "SMALLCAP")
    ("INDIA VIX",           "NSE"),
    ("SENSEX",              "BSE"),

    # MCX commodities — stored as bare commodity name; the quote
    # endpoint resolves to the near-month future at fetch time.
    ("GOLD",        "MCX"),
    ("SILVER",      "MCX"),
    ("CRUDEOIL",    "MCX"),
    ("NATURALGAS",  "MCX"),
    ("COPPER",      "MCX"),

    # CDS currency futures — stored as bare currency pair name; the
    # quote endpoint resolves to the near-month future at fetch time.
    ("USDINR",      "CDS"),
]


def markets_default_rows() -> list[dict]:
    """Returns the seed list as plain dicts ready to splat into a
    WatchlistItem(**row) constructor. Used by routes/watchlist.py at
    user-creation time."""
    return [
        {"tradingsymbol": sym, "exchange": exch, "sort_order": i}
        for i, (sym, exch) in enumerate(MARKETS_DEFAULT)
    ]
