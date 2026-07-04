"""
Default seed list for the SHARED GLOBAL Pinned watchlist.

The global Pinned is shared across every user; only admin / designated
users can mutate it. seed_global_pinned() in routes/watchlist.py reads
this list and populates an empty global Pinned at app boot. Operators
add / remove items via the standard manage-watchlists UI.

Item-naming policy:
  - Indices stored as the broker quote key (e.g. "NIFTY 50") — stable.
  - ETFs stored as the NSE tradingsymbol (e.g. GOLDBEES) — stable.
  - Bare roots (e.g. CRUDEOIL, GOLD, USDINR) are FIRST-CLASS SYMBOLS.
    Displayed as-is in the watchlist UI. At LTP subscribe / order /
    chart time the resolver translates each root to the active
    near-month contract (e.g. "GOLD" → "GOLDAUG25FUT"). Rollover to the
    next-month future is automatic — operator never updates the watchlist
    row on expiry. Roots can be added / removed via the /pulse UI exactly
    like any other watchlist item.

Migration history (markers in seed_global_pinned):
  - GOLDM, USDINR — removed Jun 2026 (wave 1)
  - COPPER, CRUDEOIL, NATURALGAS, SILVERM (MCX) — removed Jul 2026 (wave 2)
  - SILVER (MCX) — removed Jul 2026 (wave 3); operator confirmed mistake
  - USDINR contract rows (e.g. USDINR26JULFUT) → bare root Jul 2026 (wave 4)
  Migration markers stay recorded as historical audit. NATURALGAS and
  SILVER remain excluded (operator adds explicitly if wanted). All other
  MCX/CDS roots restored as first-class bare-root symbols (wave markers
  prevent the one-shot DELETE from re-firing; top-up loop re-adds them).

CDS currency roots (USDINR) are first-class bare-root entries, same
convention as MCX roots (GOLD, CRUDEOIL, etc.). The resolver translates
bare root → active near-month contract at quote / tick time. The grid
shows "USDINR" with no dated contract alias.
"""

# Each entry: (tradingsymbol, exchange). The order here is the
# `sort_order` the symbols land at — indices first, then ETFs, then
# MCX/CDS bare roots.
#
# Bare roots are first-class watchlist entries. LTP, order routing,
# chart data, and sparklines all go through the resolver which maps
# root → active near-month contract for broker calls. Rollover on
# expiry is automatic.
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

    # MCX commodity bare roots — resolver maps to active near-month future.
    # Rollover is automatic. Operator manages via /pulse UI same as any symbol.
    # SILVER (MCX) excluded — operator confirmed it was added by mistake (wave 3).
    ("SILVERM",             "MCX"),
    ("GOLD",                "MCX"),
    ("GOLDM",               "MCX"),
    ("CRUDEOIL",            "MCX"),
    ("COPPER",              "MCX"),

    # CDS currency bare root — resolver maps to active near-month future.
    ("USDINR",              "CDS"),
]


def markets_default_rows() -> list[dict]:
    """Returns the seed list as plain dicts ready to splat into a
    WatchlistItem(**row) constructor. Used by seed_global_pinned to
    populate an empty shared Pinned at app boot."""
    return [
        {"tradingsymbol": sym, "exchange": exch, "sort_order": i}
        for i, (sym, exch) in enumerate(MARKETS_DEFAULT)
    ]
