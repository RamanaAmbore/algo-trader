"""
Canonical symbol-resolution helpers for MCX / CDS futures.

All resolver callsites across the codebase delegate here — this is the
single source of truth for:

  - ``list_active_futures(root, exchange, limit)``
      Returns the next *limit* non-expired futures sorted ascending by
      expiry.  Uses the ``inst.x > today_iso (IST)`` rule so a settling
      contract is never considered "active" on its own expiry date.
      Falls back to the last listed contract (non-empty list) only for
      ``limit=1`` paths where a None return would silently drop the row.

  - ``resolve_symbol(virtual, exchange)``
      Maps a virtual first-class symbol to the real tradingsymbol.
      ``CRUDEOIL``      → front-month (futures[0])
      ``CRUDEOIL_NEXT`` → back-month  (futures[1] if available else futures[0])
      Any non-virtual symbol (real contract, equity) passes through unchanged.
      Returns the real tradingsymbol, or the virtual symbol itself when the
      instruments cache is cold.

  - ``root_of(contract, exchange)``
      Reverse resolver: given a real contract like ``CRUDEOIL26JUNFUT`` on
      MCX, returns the virtual display root (``CRUDEOIL``, ``CRUDEOIL_NEXT``,
      or the raw contract for far-month positions).
      Non-futures / unknown exchanges pass the contract through unchanged.

Virtual roots supported
-----------------------
MCX  : CRUDEOIL, CRUDEOILM, NATURALGAS, NATGASMINI, GOLD, GOLDM,
       GOLDGUINEA, GOLDPETAL, SILVER, SILVERM, SILVERMIC,
       COPPER, ZINC, LEAD, ALUMINIUM, NICKEL, MENTHAOIL, COTTON, CPO
CDS  : USDINR, EURINR, GBPINR, JPYINR

The sets are purely informational; ``list_active_futures`` works for any
root string — it queries the live instruments cache, so any new MCX/CDS
commodity that Kite adds automatically resolves without a code change.

Caching
-------
``list_active_futures`` reads the instruments cache (24-hour TTL, warmed at
startup + 08:00 IST).  It does NOT add a second layer — the instruments
cache is already the canonical data source and is the only caching layer
used by this module.

Design note
-----------
All functions are ``async`` because they read the instruments cache via
``get_or_fetch`` which wraps an asyncio.Lock.  Callers inside sync contexts
(non-async tests) can use ``asyncio.run(resolve_symbol(...))`` or
``asyncio.get_event_loop().run_until_complete(...)`` — the functions are
thin enough that the overhead is acceptable.
"""

from __future__ import annotations

import re
from typing import Optional

from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Supported virtual roots (informational — resolver works for any root)
# ---------------------------------------------------------------------------

MCX_VIRTUAL_ROOTS: frozenset[str] = frozenset({
    "CRUDEOIL", "CRUDEOILM", "NATURALGAS", "NATGASMINI",
    "GOLD", "GOLDM", "GOLDGUINEA", "GOLDPETAL",
    "SILVER", "SILVERM", "SILVERMIC",
    "COPPER", "ZINC", "LEAD", "ALUMINIUM", "NICKEL",
    "MENTHAOIL", "COTTON", "CPO",
})

CDS_VIRTUAL_ROOTS: frozenset[str] = frozenset({
    "USDINR", "EURINR", "GBPINR", "JPYINR",
})

# Exchanges that support virtual-root resolution
_VIRTUAL_EXCHANGES: frozenset[str] = frozenset({"MCX", "CDS"})

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ist_today_iso() -> str:
    """Return today's date as YYYY-MM-DD in IST."""
    from datetime import datetime as _dt
    try:
        from zoneinfo import ZoneInfo
        return _dt.now(ZoneInfo("Asia/Kolkata")).date().isoformat()
    except Exception:
        return _dt.utcnow().date().isoformat()


def _is_virtual(symbol: str) -> bool:
    """True when *symbol* looks like a virtual root (bare alpha, no digits)."""
    return bool(symbol) and symbol.isalpha()


def _strip_next(symbol: str) -> tuple[str, bool]:
    """Return (root, is_next) by stripping the ``_NEXT`` suffix if present."""
    upper = symbol.upper()
    if upper.endswith("_NEXT"):
        return upper[:-5], True
    return upper, False


# ---------------------------------------------------------------------------
# Core: list_active_futures
# ---------------------------------------------------------------------------

async def list_active_futures(
    root: str,
    exchange: str,
    limit: int = 2,
) -> list[str]:
    """Return the next *limit* non-expired futures tradingsymbols for *root*
    on *exchange*, sorted ascending by expiry.

    A contract whose expiry equals today IST is considered settling — it is
    excluded so callers never get a settlement-price quote masquerading as
    the live spot.

    Returns an empty list when the instruments cache is cold, no contracts
    match, or limit ≤ 0.
    """
    if not root or limit <= 0:
        return []

    from backend.api.cache import get_or_fetch
    from backend.api.routes.instruments import _fetch_instruments, _TTL_SECONDS
    try:
        resp = await get_or_fetch(
            "instruments", _fetch_instruments, ttl_seconds=_TTL_SECONDS
        )
        items = resp.items if resp else []
    except Exception:
        items = []
    if not items:
        return []

    today_iso = _ist_today_iso()
    target_u = root.upper()
    exch_upper = exchange.upper()

    candidates = [
        inst for inst in items
        if (inst.e == exch_upper
            and inst.t == "FUT"
            and (inst.u or "").upper() == target_u
            and inst.x
            and inst.x > today_iso)
    ]
    if not candidates:
        return []
    candidates.sort(key=lambda i: i.x or "")
    return [c.s for c in candidates[:limit]]


# ---------------------------------------------------------------------------
# Forward resolver: resolve_symbol
# ---------------------------------------------------------------------------

async def resolve_symbol(virtual: str, exchange: str) -> str:
    """Map a virtual first-class symbol to the real tradingsymbol.

    ``CRUDEOIL``      → front-month (list_active_futures[0])
    ``CRUDEOIL_NEXT`` → back-month  (list_active_futures[1], falls back to [0])
    Non-virtual symbols (e.g. ``CRUDEOIL26JUNFUT``, ``RELIANCE``) pass through.

    Falls back to the most-recently-listed contract when all active futures
    are in the instruments-cache lag window (all expired but cache not yet
    refreshed).  This mirrors the watchlist resolver fallback and ensures a
    non-None symbol rather than a silent row drop.

    Parameters
    ----------
    virtual:
        Symbol to resolve — virtual root (``GOLD``), virtual back-month
        (``GOLD_NEXT``), or an already-real tradingsymbol (passed through).
    exchange:
        Exchange string (``"MCX"`` / ``"CDS"``).  Non-MCX/CDS exchanges
        bypass resolution and return *virtual* unchanged.

    Returns
    -------
    str
        Real tradingsymbol, or *virtual* itself when resolution fails.
    """
    exch_upper = exchange.upper()
    if exch_upper not in _VIRTUAL_EXCHANGES:
        return virtual

    root, is_next = _strip_next(virtual)

    # Only bare alpha roots qualify for resolution (real contracts have digits)
    if not _is_virtual(root):
        return virtual

    futures = await list_active_futures(root, exch_upper, limit=2)

    if not futures:
        # instruments cache lag fallback — fetch all (include expiring today)
        futures = await _list_all_futures_fallback(root, exch_upper, limit=2)
        if not futures:
            logger.warning(
                f"symbol_resolver: no futures found for {root} on {exch_upper}"
            )
            return virtual

    if is_next:
        # Back-month: second contract if available, else front-month
        return futures[1] if len(futures) >= 2 else futures[0]
    else:
        return futures[0]


async def _list_all_futures_fallback(
    root: str, exchange: str, limit: int = 2
) -> list[str]:
    """Like list_active_futures but includes expiring-today contracts.
    Used only as a last resort when all active contracts are in cache-lag.
    Returns empty list if the instruments cache is cold.
    """
    from backend.api.cache import get_or_fetch
    from backend.api.routes.instruments import _fetch_instruments, _TTL_SECONDS
    try:
        resp = await get_or_fetch(
            "instruments", _fetch_instruments, ttl_seconds=_TTL_SECONDS
        )
        items = resp.items if resp else []
    except Exception:
        items = []
    if not items:
        return []

    target_u = root.upper()
    exch_upper = exchange.upper()
    candidates = [
        inst for inst in items
        if (inst.e == exch_upper
            and inst.t == "FUT"
            and (inst.u or "").upper() == target_u
            and inst.x)
    ]
    if not candidates:
        return []
    candidates.sort(key=lambda i: i.x or "")
    return [c.s for c in candidates[:limit]]


# ---------------------------------------------------------------------------
# Reverse resolver: root_of
# ---------------------------------------------------------------------------

# Kite tradingsymbol pattern for MCX/CDS futures:
#   ROOT + YY + MON + "FUT"   e.g. CRUDEOIL26JUNFUT, USDINR25MAYFUT
_FUT_RE = re.compile(
    r"^(?P<root>[A-Z]+)\d{2}[A-Z]{3}FUT$",
    re.IGNORECASE,
)


async def root_of(contract: str, exchange: str) -> str:
    """Reverse resolver: map a real futures tradingsymbol to its virtual root.

    CRUDEOIL26JUNFUT → ``CRUDEOIL``       (front-month)
    CRUDEOIL26JULFUT → ``CRUDEOIL_NEXT``  (back-month)
    CRUDEOIL26AUGFUT → ``CRUDEOIL26AUGFUT`` (far-month, pass through)
    RELIANCE         → ``RELIANCE``        (equity, pass through)

    Algorithm
    ---------
    1. Parse the root via regex (e.g. ``CRUDEOIL`` from ``CRUDEOIL26JUNFUT``).
    2. Fetch the two nearest active futures via ``list_active_futures``.
    3. If contract == futures[0] → return root (front-month virtual).
    4. If contract == futures[1] → return root + "_NEXT" (back-month virtual).
    5. Otherwise → return contract unchanged (far-month, pass through raw).

    Non-futures contracts (equities, options) and non-MCX/CDS exchanges pass
    through unchanged.
    """
    exch_upper = (exchange or "").upper()
    if exch_upper not in _VIRTUAL_EXCHANGES:
        return contract

    m = _FUT_RE.match(contract.upper())
    if not m:
        return contract

    root = m.group("root")
    futures = await list_active_futures(root, exch_upper, limit=2)

    if not futures:
        # Cache lag: try including today's expiry
        futures = await _list_all_futures_fallback(root, exch_upper, limit=2)
    if not futures:
        return contract

    contract_upper = contract.upper()
    if futures[0].upper() == contract_upper:
        return root
    if len(futures) >= 2 and futures[1].upper() == contract_upper:
        return f"{root}_NEXT"
    # Far-month or unknown — return raw contract
    return contract


# ---------------------------------------------------------------------------
# Batch resolver: resolve_market_data_keys
# ---------------------------------------------------------------------------

class MarketDataKeyMap:
    """Return value from ``resolve_market_data_keys``.

    Attributes
    ----------
    broker_keys : list[str]
        Resolved broker keys in ``EXCHANGE:TRADINGSYMBOL`` format — safe to
        pass directly to ``broker.quote()`` or ``broker.ltp()``.
    input_to_broker : dict[str, str]
        Maps each original input key (e.g. ``"MCX:CRUDEOIL"``) to its
        resolved broker key (e.g. ``"MCX:CRUDEOILM26JULFUT"``).
        Identity entries are included for non-virtual symbols so callers can
        always look up any input key without a special-case branch.
    broker_to_input : dict[str, str]
        Reverse map — resolved broker key → original input key.  Used to
        re-key broker response rows back to operator-facing symbols.
    """

    __slots__ = ("broker_keys", "input_to_broker", "broker_to_input")

    def __init__(
        self,
        broker_keys: list[str],
        input_to_broker: dict[str, str],
        broker_to_input: dict[str, str],
    ) -> None:
        self.broker_keys = broker_keys
        self.input_to_broker = input_to_broker
        self.broker_to_input = broker_to_input


async def resolve_market_data_keys(keys: list[str]) -> "MarketDataKeyMap":
    """Resolve a list of ``EXCHANGE:TRADINGSYMBOL`` broker keys, replacing
    virtual MCX/CDS root symbols with their front-month futures contracts.

    Virtual roots (bare alpha symbols on MCX/CDS) are not tradable
    instruments — ``broker.quote("MCX:CRUDEOIL")`` returns nothing.
    This helper maps them to the real contract (e.g.
    ``"MCX:CRUDEOILM26JULFUT"``) before the broker call, then lets
    callers re-key the broker response back to the original operator-facing
    symbol via the returned maps.

    Non-virtual keys (equities, real contracts, indices) are passed through
    unchanged — an identity mapping is still included so callers can use
    ``broker_to_input`` for every key without a special-case branch.

    Resolution failures (instruments cache cold, network timeout) return an
    identity mapping for the affected key so the call proceeds; the broker
    will return empty data for that key which the caller already handles.

    Parameters
    ----------
    keys : list[str]
        Input broker keys in ``EXCHANGE:TRADINGSYMBOL`` format.
        Keys without a ``:`` separator are passed through unchanged.

    Returns
    -------
    MarketDataKeyMap
        ``.broker_keys`` — deduplicated resolved keys for the broker call.
        ``.input_to_broker`` — original key → resolved key.
        ``.broker_to_input`` — resolved key → original key (last-wins on
        duplicate broker keys, which can't happen because two distinct
        original inputs never resolve to the same contract).

    Log tag
    -------
    ``[MARKET-DATA-VIRTUAL-RESOLVE]`` — emitted once per resolved key so
    the operator can grep for resolution events.
    """
    input_to_broker: dict[str, str] = {}
    broker_to_input: dict[str, str] = {}

    for key in keys:
        if ":" not in key:
            # Malformed — pass through; broker will ignore or error.
            input_to_broker[key] = key
            broker_to_input[key] = key
            continue

        exch, sym = key.split(":", 1)
        exch_upper = exch.upper()

        # Virtual roots include bare alpha symbols (CRUDEOIL) AND back-month
        # variants (CRUDEOIL_NEXT / GOLDM_NEXT).  The _NEXT suffix contains an
        # underscore so isalpha() fails; strip the suffix before the check.
        sym_root, _ = _strip_next(sym)
        is_virtual_sym = exch_upper in _VIRTUAL_EXCHANGES and _is_virtual(sym_root)
        if is_virtual_sym:
            try:
                resolved_sym = await resolve_symbol(sym, exch_upper)
            except Exception:
                resolved_sym = sym

            resolved_key = f"{exch_upper}:{resolved_sym}"

            if resolved_sym != sym.upper():
                logger.info(
                    f"[MARKET-DATA-VIRTUAL-RESOLVE] input={sym} "
                    f"exchange={exch_upper} resolved={resolved_sym}"
                )
        else:
            # Non-virtual: identity mapping.
            resolved_key = f"{exch_upper}:{sym.upper()}"

        input_to_broker[key] = resolved_key
        broker_to_input[resolved_key] = key

    # Deduplicated broker keys (preserves original insertion order).
    seen: set[str] = set()
    broker_keys: list[str] = []
    for bk in input_to_broker.values():
        if bk not in seen:
            seen.add(bk)
            broker_keys.append(bk)

    return MarketDataKeyMap(
        broker_keys=broker_keys,
        input_to_broker=input_to_broker,
        broker_to_input=broker_to_input,
    )
