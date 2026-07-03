"""
Symbol resolution endpoints — virtual first-class MCX/CDS symbol support.

GET /api/symbols/resolve?symbol=CRUDEOIL&exchange=MCX
    Maps a virtual symbol to the real tradingsymbol.
    CRUDEOIL      → CRUDEOIL26JUNFUT   (front-month)
    CRUDEOIL_NEXT → CRUDEOIL26JULFUT   (back-month)
    CRUDEOIL26JUNFUT → CRUDEOIL26JUNFUT (real contract, pass-through)

GET /api/symbols/root_of?contract=CRUDEOIL26JUNFUT&exchange=MCX
    Reverse resolver: maps a real contract to its virtual display root.
    CRUDEOIL26JUNFUT → CRUDEOIL         (front-month)
    CRUDEOIL26JULFUT → CRUDEOIL_NEXT    (back-month)
    CRUDEOIL26AUGFUT → CRUDEOIL26AUGFUT (far-month, pass-through)
    RELIANCE         → RELIANCE          (equity, pass-through)

Both endpoints are auth-gated (demo allowed). Instruments cache is shared
with the 24h-cached /api/instruments endpoint so no extra broker calls.

Response shapes are kept minimal (single string field) to allow inlining
in the frontend resolution helper without any schema gymnastics.
"""

from __future__ import annotations

import msgspec
from litestar import Controller, get
from litestar.exceptions import HTTPException
from litestar.params import Parameter

from backend.api.auth_guard import auth_or_demo_guard
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


class SymbolResolveResponse(msgspec.Struct):
    """Forward resolution: virtual symbol → real tradingsymbol."""
    virtual: str       # input symbol (as-sent)
    exchange: str      # exchange (upper-cased)
    resolved: str      # real tradingsymbol (or same as virtual if pass-through)
    is_front: bool     # True when resolved = front-month contract
    is_back: bool      # True when resolved = back-month (_NEXT) contract
    is_passthrough: bool  # True when no resolution was needed


class RootOfResponse(msgspec.Struct):
    """Reverse resolution: real contract → virtual display root."""
    contract: str      # input contract (as-sent)
    exchange: str      # exchange (upper-cased)
    root: str          # virtual root (or same as contract if no mapping)
    is_front: bool     # True when contract is the current front-month
    is_back: bool      # True when contract is the current back-month
    is_far: bool       # True when contract is further out (pass-through)


class SymbolsController(Controller):
    path = "/api/symbols"
    guards = [auth_or_demo_guard]

    @get("/resolve")
    async def resolve_symbol(
        self,
        symbol: str = Parameter(query="symbol", description="Virtual or real symbol"),
        exchange: str = Parameter(query="exchange", description="Exchange: MCX or CDS"),
    ) -> SymbolResolveResponse:
        """Resolve a virtual first-class symbol to the real tradingsymbol.

        For MCX/CDS virtual roots (e.g. CRUDEOIL, USDINR) returns the
        front-month contract.  Appending _NEXT returns the back-month.
        All other inputs (real contracts, equities) pass through unchanged.
        """
        try:
            from backend.api.algo.symbol_resolver import (
                resolve_symbol as _resolve,
                _strip_next,
                _is_virtual,
                _VIRTUAL_EXCHANGES,
            )
            exch_upper = exchange.upper()
            root, is_next = _strip_next(symbol)
            is_virtual_root = (
                exch_upper in _VIRTUAL_EXCHANGES and _is_virtual(root)
            )

            resolved = await _resolve(symbol, exchange)

            # Determine if it was a pass-through (no resolution)
            is_passthrough = not is_virtual_root or resolved == symbol

            # Determine front vs back
            is_front = is_virtual_root and not is_next and not is_passthrough
            is_back = is_virtual_root and is_next and not is_passthrough

            return SymbolResolveResponse(
                virtual=symbol,
                exchange=exch_upper,
                resolved=resolved,
                is_front=is_front,
                is_back=is_back,
                is_passthrough=is_passthrough,
            )
        except Exception as exc:
            logger.error(f"Symbols resolve error symbol={symbol} exch={exchange}: {exc}")
            raise HTTPException(status_code=500, detail=str(exc))

    @get("/root_of")
    async def root_of(
        self,
        contract: str = Parameter(query="contract", description="Real tradingsymbol"),
        exchange: str = Parameter(query="exchange", description="Exchange: MCX or CDS"),
    ) -> RootOfResponse:
        """Map a real futures contract to its virtual display root.

        Returns the virtual root (e.g. CRUDEOIL) for front-month contracts
        and ROOT_NEXT for back-month contracts.  Far-month contracts and
        non-futures instruments pass through unchanged.
        """
        try:
            from backend.api.algo.symbol_resolver import (
                root_of as _root_of,
                list_active_futures,
                _VIRTUAL_EXCHANGES,
                _FUT_RE,
            )
            exch_upper = exchange.upper()
            root_label = await _root_of(contract, exchange)

            # Determine front / back / far
            is_front = False
            is_back = False
            is_far = False

            if exch_upper in _VIRTUAL_EXCHANGES:
                m = _FUT_RE.match(contract.upper())
                if m:
                    raw_root = m.group("root")
                    if root_label.upper() == raw_root:
                        is_front = True
                    elif root_label.upper() == f"{raw_root}_NEXT":
                        is_back = True
                    else:
                        is_far = True

            return RootOfResponse(
                contract=contract,
                exchange=exch_upper,
                root=root_label,
                is_front=is_front,
                is_back=is_back,
                is_far=is_far,
            )
        except Exception as exc:
            logger.error(f"Symbols root_of error contract={contract} exch={exchange}: {exc}")
            raise HTTPException(status_code=500, detail=str(exc))
