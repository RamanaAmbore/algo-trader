"""
Default seed list for the SHARED GLOBAL Pinned watchlist.

The global Pinned is shared across every user; only admin / designated
users can mutate it. seed_global_pinned() in routes/watchlist.py reads
this list and populates an empty global Pinned at app boot. Operators
add / remove items via the standard manage-watchlists UI.

Item-naming policy:
  - Indices stored as the broker quote key (e.g. "NIFTY 50") — stable.
  - ETFs stored as the NSE tradingsymbol (e.g. GOLDBEES) — stable.
  - F&O is NOT seeded by default. Operator adds futures / options via
    the /pulse watchlist UI when wanted.

Removed from seed (with migration markers in seed_global_pinned):
  - GOLDM, USDINR — prior cleanup
  - COPPER, CRUDEOIL, NATURALGAS, SILVERM (MCX) — Jul 2026 (bare roots
    resolve to near-month futures; operator adds explicitly if wanted)
"""

# Each entry: (tradingsymbol, exchange). The order here is the
# `sort_order` the symbols land at — indices first, then ETFs.
# Pinned is INDICES + ETFs only. F&O is not seeded by default.
MARKETS_DEFAULT: list[tuple[str, str]] = [
    # Indices — quote endpoint maps these via broker.quote() keys
    # like NSE:NIFTY 50. Stable across months.
    ("NIFTY 50",            "NSE"),
    ("SENSEX",              "BSE"),
    ("NIFTY BANK",          "NSE"),
    ("NIFTY IT",            "NSE"),
    ("NIFTY MIDCAP 100",    "NSE"),
    ("NIFTY SMLCAP 100",    "NSE"),
    ("INDIA VIX",           "NSE"),

    # ETFs — gold + silver cash exposure via Nippon's BeES family.
    ("GOLDBEES",            "NSE"),
    ("SILVERBEES",          "NSE"),
]


def markets_default_rows() -> list[dict]:
    """Returns the seed list as plain dicts ready to splat into a
    WatchlistItem(**row) constructor. Used by seed_global_pinned to
    populate an empty shared Pinned at app boot."""
    return [
        {"tradingsymbol": sym, "exchange": exch, "sort_order": i}
        for i, (sym, exch) in enumerate(MARKETS_DEFAULT)
    ]
