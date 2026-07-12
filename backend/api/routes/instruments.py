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
    'CRUDE OIL':  100,     # space-variant alias (Kite `name` field may differ)
    'CRUDEOILM':  10,      # mini: 10 barrels
    'CRUDE OIL M': 10,     # space-variant alias
    'NATURALGAS': 1250,    # 1250 mmBtu per lot
    'NATURAL GAS': 1250,   # space-variant alias
    'NATGASMINI': 250,     # mini: 250 mmBtu
    'NAT GAS MINI': 250,   # space-variant alias
    'GOLD':       100,     # 100 grams per lot
    'GOLDM':      10,      # mini: 10 grams
    'GOLD M':     10,      # space-variant alias
    'GOLDGUINEA': 8,       # 8 grams
    'GOLD GUINEA': 8,      # space-variant alias
    'GOLDPETAL':  1,       # 1 gram
    'GOLD PETAL': 1,       # space-variant alias
    'SILVER':     30,      # 30 kg per lot
    'SILVERM':    5,       # 5 kg
    'SILVER M':   5,       # space-variant alias
    'SILVERMIC':  1,       # 1 kg
    'SILVER MIC': 1,       # space-variant alias
    'COPPER':     2500,    # 2500 kg
    'ZINC':       5000,    # 5000 kg
    'LEAD':       5000,    # 5000 kg
    'ALUMINIUM':  5000,    # 5000 kg
    'NICKEL':     1500,    # 1500 kg
    'MENTHAOIL':  360,     # 360 kg
    'MENTHA OIL': 360,     # space-variant alias
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


def _fetch_exchange_raw(exch: str, kite_accts: list) -> "list | None":
    """Attempt to fetch the Kite instrument dump for one exchange.

    Walks every loaded Kite account and returns the first non-empty
    Kite-shaped response, or None when all accounts fail / return empty.
    Logs warnings on every failure; caller skips the exchange on None.

    NEVER falls over to Dhan/Groww — a partial Kite cache is strictly
    better than a poisoned schema with missing instrument_type/name fields.
    """
    from backend.brokers.registry import get_broker
    _last_err: Exception | None = None
    for _acct in kite_accts:
        try:
            broker = get_broker(_acct)
            _resp = broker.instruments(exch)
        except Exception as e:
            _last_err = e
            continue
        if not _resp:
            continue
        # Kite-shape sanity check: `instrument_type` must be present on row 0.
        if not isinstance(_resp[0], dict) or "instrument_type" not in _resp[0]:
            logger.warning(
                f"Instruments: {exch} on {_acct} returned non-Kite shape "
                f"({list(_resp[0].keys()) if isinstance(_resp[0], dict) else type(_resp[0]).__name__}) "
                f"— trying next account"
            )
            continue
        return _resp
    # All accounts failed or empty.
    if _last_err is not None:
        logger.warning(f"Instruments: {exch} fetch failed on every Kite account: {_last_err}")
    else:
        logger.warning(f"Instruments: {exch} returned no usable data on any Kite account")
    return None


def _build_instrument_row(inst: dict, exch: str, mcx_diag_logged: set) -> Instrument:
    """Construct a single Instrument struct from one raw Kite instrument dict.

    Applies MCX lot-size overrides and emits one diagnostic log line per
    MCX derivative type per fetch cycle (tracked via mcx_diag_logged set).
    """
    itype   = inst.get("instrument_type", "")
    expiry  = inst.get("expiry")
    strike  = inst.get("strike")
    ls_raw  = int(inst.get("lot_size") or 1)
    if exch == "MCX":
        if itype in ("CE", "PE", "FUT") and itype not in mcx_diag_logged:
            mcx_diag_logged.add(itype)
            logger.info(
                f"[MCX-INSTR-DIAG] kind={itype} "
                f"tradingsymbol={inst.get('tradingsymbol')} "
                f"name='{inst.get('name')}' "
                f"lot_size={inst.get('lot_size')}"
            )
        ls_raw = _MCX_LOT_OVERRIDES.get((inst.get("name") or "").upper(), ls_raw)
    return Instrument(
        s=inst["tradingsymbol"],
        e=inst["exchange"],
        t=itype,
        ls=ls_raw,
        ts=float(inst.get("tick_size") or 0.05),
        u=inst.get("name") or None,
        x=expiry.isoformat() if isinstance(expiry, date) else (expiry or None),
        k=float(strike) if strike not in (None, 0, 0.0) else None,
    )


def _fetch_instruments() -> InstrumentsResponse:
    """Fetch full instrument dump from Kite across all relevant exchanges.

    Must use a Kite broker — Dhan/Groww instruments() return a different dict
    schema (no `instrument_type` / `name` fields) which would leave every
    instrument with t='' and break the movers underlyings universe.
    When RAMBOQ_USE_CONN_SERVICE=1, get_broker() returns RemoteBroker stubs
    that proxy through conn_service, so the Kite filter still applies.

    Kite-only walk (Jul 2026 defect fix): iterate ALL loaded Kite accounts
    explicitly; break on first success per exchange. NEVER fall over to
    Dhan/Groww — a partial Kite cache is strictly better than a poisoned
    Dhan cache with missing instrument_type/name/expiry/strike fields.
    """
    from backend.brokers.registry import _loaded_accounts, _broker_id_for
    accts = _loaded_accounts()
    kite_accts = [a for a in accts if _broker_id_for(a) in {"zerodha_kite", "kite"}]
    if not kite_accts:
        logger.warning("Instruments: no Kite account loaded — dump unavailable")
        return InstrumentsResponse(cycle_date="", count=0, items=[])

    items: list[Instrument] = []
    for exch in _EXCHANGES:
        raw = _fetch_exchange_raw(exch, kite_accts)
        if raw is None:
            continue
        # MCX diagnostic set: one log line per derivative type per cycle.
        _mcx_diag_logged: set[str] = set()
        for inst in raw:
            items.append(_build_instrument_row(inst, exch, _mcx_diag_logged))

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
