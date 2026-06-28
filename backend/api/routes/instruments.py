"""
Instruments endpoint — Kite master instrument dump for client-side autocomplete.

GET /api/instruments    — full instrument list, cached daily (refreshed at 08:00 IST)

Returns a trimmed list suitable for symbol autocomplete:
  [
    {"s": "RELIANCE", "e": "NSE", "t": "EQ", "ls": 1, "ts": 0.05},
    {"s": "NIFTY25APR0322500CE", "e": "NFO", "t": "CE", "u": "NIFTY",
     "x": "2026-04-03", "k": 22500, "ls": 50, "ts": 0.05},
    ...
  ]

Field abbreviations keep payload small:
  s  = tradingsymbol
  e  = exchange
  t  = instrument_type (EQ / FUT / CE / PE)
  u  = underlying name (options/futures only)
  x  = expiry (YYYY-MM-DD, options/futures only)
  k  = strike (options only)
  ls = lot_size
  ts = tick_size
"""

from datetime import date
from typing import Optional

import msgspec
from litestar import Controller, get
from litestar.exceptions import HTTPException

from backend.api.auth_guard import auth_or_demo_guard
from backend.api.cache import get_or_fetch
from backend.brokers.connections import Connections
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)

_TTL_SECONDS = 86400  # 24 h — background task re-warms daily at 08:00 IST
_EXCHANGES   = ("NSE", "NFO", "BSE", "MCX", "CDS")

# MCX commodity lot-size overrides. Kite Connect's `kite.instruments("MCX")`
# response returns `lot_size=1` for every commodity contract — the actual
# contract size (e.g. CRUDEOIL = 100 barrels, NATURALGAS = 1250 mmBtu)
# isn't exposed via the API. Without this override, the OrderTicket on a
# 2-lot CRUDEOIL position (200 qty) renders as "Lots: 200" instead of
# "Lots: 2 (× 100 = 200)" because lotSize defaults to 1.
#
# Keyed by Kite's `name` field (the underlying ticker). Add commodities
# here as the trader desk verifies their contract size. The override
# applies to FUT and CE/PE rows alike (options on commodity contracts use
# the same multiplier as the underlying future).
#
# References: MCX contract specs at
#   https://www.mcxindia.com/products/bullion/mcx-products
_MCX_LOT_OVERRIDES = {
    'CRUDEOIL':   100,     # 100 barrels per lot
    'CRUDEOILM':  10,      # mini: 10 barrels
    'NATURALGAS': 1250,    # 1250 mmBtu per lot
    'NATGASMINI': 250,     # mini: 250 mmBtu
    'GOLD':       100,     # 100 grams per lot
    'GOLDM':      10,      # mini: 10 grams
    'GOLDGUINEA': 8,       # 8 grams
    'GOLDPETAL':  1,       # 1 gram
    'SILVER':     30,      # 30 kg per lot
    'SILVERM':    5,       # 5 kg
    'SILVERMIC':  1,       # 1 kg
    'COPPER':     2500,    # 2500 kg
    'ZINC':       5000,    # 5000 kg
    'LEAD':       5000,    # 5000 kg
    'ALUMINIUM':  5000,    # 5000 kg
    'NICKEL':     1500,    # 1500 kg
    'MENTHAOIL':  360,     # 360 kg
    'COTTON':     185,     # 185 bales (verify per contract)
    'CPO':        10,      # 10 mt
}


class Instrument(msgspec.Struct, omit_defaults=True):
    s: str                        # tradingsymbol
    e: str                        # exchange
    t: str                        # instrument_type (EQ / FUT / CE / PE)
    ls: int                       # lot_size
    ts: float                     # tick_size
    u: Optional[str]  = None      # underlying name
    x: Optional[str]  = None      # expiry YYYY-MM-DD
    k: Optional[float] = None     # strike


class InstrumentsResponse(msgspec.Struct):
    cycle_date: str
    count: int
    items: list[Instrument]


def _fetch_instruments() -> InstrumentsResponse:
    """Fetch full instrument dump from Kite across all relevant exchanges.

    Routes through the Broker ABC so the call hops to conn_service when
    RAMBOQ_USE_CONN_SERVICE=1."""
    from backend.brokers.registry import all_brokers
    brokers = all_brokers()
    if not brokers:
        return InstrumentsResponse(cycle_date="", count=0, items=[])
    broker = brokers[0]

    items: list[Instrument] = []
    for exch in _EXCHANGES:
        try:
            raw = broker.instruments(exch)
        except Exception as e:
            logger.warning(f"Instruments: {exch} fetch failed: {e}")
            continue

        for inst in raw:
            itype = inst.get("instrument_type", "")
            expiry = inst.get("expiry")
            strike = inst.get("strike")
            # MCX commodities → real lot sizes from the override map
            # (Kite reports them all as 1). Other exchanges keep the
            # vendor-supplied lot_size verbatim.
            ls_raw = int(inst.get("lot_size") or 1)
            if exch == "MCX":
                ls_raw = _MCX_LOT_OVERRIDES.get(
                    (inst.get("name") or "").upper(), ls_raw
                )
            items.append(Instrument(
                s=inst["tradingsymbol"],
                e=inst["exchange"],
                t=itype,
                ls=ls_raw,
                ts=float(inst.get("tick_size") or 0.05),
                u=inst.get("name") or None,
                x=expiry.isoformat() if isinstance(expiry, date) else (expiry or None),
                k=float(strike) if strike not in (None, 0, 0.0) else None,
            ))

    logger.info(f"Instruments: loaded {len(items)} rows across {len(_EXCHANGES)} exchanges")
    return InstrumentsResponse(
        cycle_date=date.today().isoformat(),
        count=len(items),
        items=items,
    )


class InstrumentsController(Controller):
    path = "/api/instruments"
    guards = [auth_or_demo_guard]

    @get("/")
    async def get_instruments(self) -> InstrumentsResponse:
        try:
            return await get_or_fetch("instruments", _fetch_instruments, ttl_seconds=_TTL_SECONDS)
        except Exception as e:
            logger.error(f"Instruments API error: {e}")
            raise HTTPException(status_code=500, detail=str(e))
