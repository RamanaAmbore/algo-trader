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
  - _NEXT variants (e.g. GOLD_NEXT) are BACK-MONTH virtual roots.
    They MUST appear IMMEDIATELY after their bare root in this list.
    This adjacency invariant is enforced by sort_order = index * 10,
    so root lands at N*10 and _NEXT lands at (N+1)*10 (the very next
    entry). Wave-5 migration re-stamps sort_orders to enforce adjacency
    on existing installations.

Migration history (markers in seed_global_pinned):
  - GOLDM, USDINR — removed Jun 2026 (wave 1)
  - COPPER, CRUDEOIL, NATURALGAS, SILVERM (MCX) — removed Jul 2026 (wave 2)
  - SILVER (MCX) — removed Jul 2026 (wave 3); operator confirmed mistake
  - USDINR contract rows (e.g. USDINR26JULFUT) → bare root Jul 2026 (wave 4)
  - _NEXT variants added adjacent to roots Jul 2026 (wave 5);
    SILVER MCX re-added as virtual root alongside SILVER_NEXT
  Migration markers stay recorded as historical audit. NATURALGAS remains
  excluded (operator adds explicitly if wanted). All MCX/CDS roots restored
  as first-class bare-root symbols, each with their _NEXT back-month pair
  immediately adjacent (wave markers prevent the one-shot DELETE from
  re-firing; top-up loop re-adds them).

CDS currency roots (USDINR) are first-class bare-root entries, same
convention as MCX roots (GOLD, CRUDEOIL, etc.). The resolver translates
bare root -> active near-month contract at quote / tick time. The grid
shows "USDINR" with no dated contract alias.
"""

# Each entry: (tradingsymbol, exchange). The order here is the canonical
# sort_order the symbols land at -- indices first, then ETFs, then
# MCX/CDS bare roots with their _NEXT back-month variant immediately after.
#
# INVARIANT: every _NEXT entry MUST follow its bare root immediately.
# sort_order is assigned as index * 10, giving root=N*10 and
# _NEXT=(N+1)*10 -- always adjacent.
#
# Bare roots are first-class watchlist entries. LTP, order routing,
# chart data, and sparklines all go through the resolver which maps
# root -> active near-month contract for broker calls. Rollover on
# expiry is automatic.
#
# NATURALGAS excluded -- not restored after wave 2; operator adds explicitly if wanted.
MARKETS_DEFAULT: list[tuple[str, str]] = [
    # Indices -- quote endpoint maps these via broker.quote() keys
    # like NSE:NIFTY 50. Stable across months.
    ("NIFTY 50",         "NSE"),
    ("SENSEX",           "BSE"),
    ("NIFTY BANK",       "NSE"),
    ("NIFTY IT",         "NSE"),
    ("NIFTY MIDCAP 100", "NSE"),
    ("NIFTY SMLCAP 100", "NSE"),
    ("INDIA VIX",        "NSE"),

    # ETFs -- gold + silver cash exposure via Nippon's BeES family.
    ("GOLDBEES",         "NSE"),
    ("SILVERBEES",       "NSE"),

    # MCX commodity bare roots -- each followed immediately by its _NEXT
    # back-month variant. Resolver maps root -> active near-month future;
    # _NEXT -> back-month. Rollover is automatic on expiry.
    ("CRUDEOIL",         "MCX"),
    ("CRUDEOIL_NEXT",    "MCX"),
    ("GOLD",             "MCX"),
    ("GOLD_NEXT",        "MCX"),
    ("GOLDM",            "MCX"),
    ("GOLDM_NEXT",       "MCX"),
    ("SILVER",           "MCX"),
    ("SILVER_NEXT",      "MCX"),
    ("SILVERM",          "MCX"),
    ("SILVERM_NEXT",     "MCX"),
    ("COPPER",           "MCX"),
    ("COPPER_NEXT",      "MCX"),

    # CDS currency bare root -- resolver maps to active near-month future.
    ("USDINR",           "CDS"),
    ("USDINR_NEXT",      "CDS"),
]


def markets_default_rows() -> list[dict]:
    """Returns the seed list as plain dicts with canonical sort_order.

    sort_order = index * 10 so there is a gap between every entry, making
    root + _NEXT pairs adjacent (root at N*10, _NEXT at (N+1)*10).
    Wave-5 re-stamps existing rows to this scheme so the Pinned grid
    naturally sorts root immediately before _NEXT.

    Used by seed_global_pinned to populate / top-up the shared Pinned.
    """
    return [
        {"tradingsymbol": sym, "exchange": exch, "sort_order": i * 10}
        for i, (sym, exch) in enumerate(MARKETS_DEFAULT)
    ]
