"""
Zerodha Kite implementation of the `Broker` interface.

Wraps the existing `KiteConnection` (see
`backend/shared/helpers/connections.py`) so auth, token caching, 23 h
refresh, multi-account IPv6 binding, and the parallel-login lock all
keep working exactly as they do today — this module is a thin typed
facade over that machinery.
"""

from __future__ import annotations

from typing import Any

from backend.shared.brokers.base import Broker
from backend.shared.helpers.connections import KiteConnection
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


def to_kite_qty(exchange: str, raw_qty: int, lot_size: int) -> int:
    """Translate raw contract qty to Kite's quantity convention.

    NSE/BSE/NFO/BFO/CDS/BCD: quantity = contracts. NSE F&O places
    qty=50 to trade 1 NIFTY lot (lot_size=50). MCX/NCO: quantity =
    LOTS. MCX CRUDEOIL with lot_size=100 wants qty=1 to trade 1 lot.
    Without this translation, a 1-lot MCX order ends up as 100 lots
    on Kite. lot_size==0 falls through unchanged.

    Only translates when raw_qty >= lot_size (operator typed contracts,
    not an already-translated value). Sub-lot-size values pass through
    as-is — better to let Kite reject an odd qty than silently divide
    and send a nonsensical number.
    """
    if exchange in ("MCX", "NCO") and lot_size > 0 and raw_qty >= lot_size:
        translated = max(1, raw_qty // lot_size)
        if translated != raw_qty:
            logger.info(
                f"[KITE-QTY] {exchange}: contracts={raw_qty} → lots={translated} "
                f"(lot_size={lot_size})"
            )
        return translated
    return raw_qty


async def get_lot_size(exchange: str, tradingsymbol: str) -> int:
    """Look up lot_size from the instruments cache.

    Returns 1 (safe no-op for to_kite_qty) when the cache is cold or
    the symbol isn't found — the order goes through as-is and Kite
    provides the real rejection if the qty is wrong.

    This is intentionally a best-effort read; it must never raise.
    """
    try:
        from backend.api.cache import get_or_fetch
        from backend.api.routes.instruments import _fetch_instruments, _TTL_SECONDS
        resp = await get_or_fetch("instruments", _fetch_instruments,
                                  ttl_seconds=_TTL_SECONDS)
        for inst in resp.items:
            if inst.e == exchange and inst.s == tradingsymbol:
                return int(inst.ls) if inst.ls > 0 else 1
    except Exception as e:
        logger.debug(f"[KITE-QTY] lot_size lookup failed for {exchange}/{tradingsymbol}: {e}")
    return 1


class KiteBroker(Broker):

    def __init__(self, conn: KiteConnection) -> None:
        self._conn = conn

    # ── Identity + escape hatch ───────────────────────────────────────

    @property
    def account(self) -> str:
        return self._conn.account

    @property
    def broker_id(self) -> str:
        return "zerodha_kite"

    @property
    def kite(self):
        """
        Underlying KiteConnect SDK handle. Re-validates the token on
        every access (via `get_kite_conn(test_conn=False)`), so cheap
        after the singleton is warmed. Prefer the typed methods below
        — this property is the escape hatch for operations that
        haven't been lifted into `Broker` yet.
        """
        return self._conn.get_kite_conn()

    # ── Account state ─────────────────────────────────────────────────

    def profile(self) -> dict:
        return self.kite.profile()

    def holdings(self) -> list[dict]:
        return self.kite.holdings()

    def positions(self) -> dict:
        return self.kite.positions()

    def margins(self, segment: str | None = None) -> dict:
        return self.kite.margins(segment) if segment else self.kite.margins()

    def orders(self) -> list[dict]:
        return self.kite.orders()

    def trades(self) -> list[dict]:
        return self.kite.trades()

    # ── Market data ───────────────────────────────────────────────────

    def ltp(self, symbols: list[str]) -> dict:
        return self.kite.ltp(symbols)

    def quote(self, symbols: list[str]) -> dict:
        return self.kite.quote(symbols)

    def instruments(self, exchange: str | None = None) -> list[dict]:
        return self.kite.instruments(exchange) if exchange else self.kite.instruments()

    def historical_data(
        self,
        instrument_token: int,
        from_date: Any,
        to_date: Any,
        interval: str = "day",
    ) -> list[dict]:
        return self.kite.historical_data(instrument_token, from_date,
                                         to_date, interval)

    def holidays(self, exchange: str) -> set[str]:
        return self.kite.holidays(exchange)

    # ── Order entry ───────────────────────────────────────────────────

    def basket_order_margins(self, orders: list[dict]) -> list[dict]:
        return self.kite.basket_order_margins(orders)

    def place_order(self, **kwargs: Any) -> str:
        return self.kite.place_order(**kwargs)

    def modify_order(self, order_id: str, **kwargs: Any) -> str:
        return self.kite.modify_order(order_id=order_id, **kwargs)

    def cancel_order(self, order_id: str, **kwargs: Any) -> str:
        return self.kite.cancel_order(order_id=order_id, **kwargs)

    # ── GTT / trigger orders ──────────────────────────────────────────
    #
    # Kite GTT shape (from kiteconnect SDK):
    #   trigger_type : "single" | "two-leg"
    #   orders       : list of {transaction_type, quantity, price,
    #                            order_type, product, exchange,
    #                            tradingsymbol}
    #   trigger_values: list[float]; len==1 for single, len==2 for OCO
    # See: https://kite.trade/docs/connect/v3/gtt/

    def place_gtt(
        self,
        *,
        trigger_type: str,
        tradingsymbol: str,
        exchange: str,
        last_price: float,
        orders: list[dict],
        trigger_values: list[float],
        tag: str | None = None,
    ) -> str:
        # Kite's place_gtt requires every order dict to carry exchange +
        # tradingsymbol on the leg itself. Inject them so callers can
        # keep the order dict broker-agnostic (Dhan uses different keys).
        enriched_orders = [
            {**o, "exchange": exchange, "tradingsymbol": tradingsymbol}
            for o in orders
        ]
        resp = self.kite.place_gtt(
            trigger_type=trigger_type,
            tradingsymbol=tradingsymbol,
            exchange=exchange,
            trigger_values=trigger_values,
            last_price=last_price,
            orders=enriched_orders,
        )
        # SDK returns {"trigger_id": <int>}; coerce to string for
        # consistency with the rest of the Broker interface.
        return str(resp.get("trigger_id", "") if isinstance(resp, dict) else resp)

    def modify_gtt(
        self,
        gtt_id: str,
        *,
        trigger_type: str,
        tradingsymbol: str,
        exchange: str,
        last_price: float,
        orders: list[dict],
        trigger_values: list[float],
    ) -> str:
        enriched_orders = [
            {**o, "exchange": exchange, "tradingsymbol": tradingsymbol}
            for o in orders
        ]
        resp = self.kite.modify_gtt(
            trigger_id=int(gtt_id),
            trigger_type=trigger_type,
            tradingsymbol=tradingsymbol,
            exchange=exchange,
            trigger_values=trigger_values,
            last_price=last_price,
            orders=enriched_orders,
        )
        return str(resp.get("trigger_id", gtt_id) if isinstance(resp, dict) else gtt_id)

    def cancel_gtt(self, gtt_id: str) -> str:
        # Kite SDK method is `delete_gtt`, not cancel — wrap for ABC consistency.
        resp = self.kite.delete_gtt(trigger_id=int(gtt_id))
        return str(resp.get("trigger_id", gtt_id) if isinstance(resp, dict) else gtt_id)

    def get_gtts(self) -> list[dict]:
        return self.kite.get_gtts()

    # ── Qty translation ───────────────────────────────────────────────

    def normalise_qty(self, exchange: str, raw_qty: int,
                      lot_size: int) -> int:
        """MCX/NCO want qty=lots; every other Kite exchange wants
        qty=contracts. Delegates to the module-level `to_kite_qty`."""
        return to_kite_qty(exchange, raw_qty, lot_size)
