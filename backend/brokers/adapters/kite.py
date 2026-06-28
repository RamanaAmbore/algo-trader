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

from backend.brokers.base import Broker
from backend.brokers.connections import KiteConnection
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


def from_kite_qty(exchange: str, kite_qty: int, lot_size: int) -> int:
    """Reverse of `to_kite_qty`: convert Kite's reported qty (whatever
    `place_order` was given) back to our internal contract qty.

    Sprint D fix — the chase loop reads `status.filled_quantity` from
    Kite and compares it against `remaining_qty` (which is contracts).
    For MCX/NCO Kite returns lots, so without this reverse-translate
    every MCX chase saw `filled_quantity=1` against `remaining=100`
    and triggered the partial-fill branch on every poll — corrupting
    `AlgoOrder.filled_quantity` (lots written into a contracts column)
    and starting an infinite chase even when the order had fully
    filled.

    Equity paths pass through unchanged (Kite already reports
    contracts on NSE/BSE/NFO).
    """
    if exchange in ("MCX", "NCO") and lot_size > 0 and kite_qty > 0:
        return kite_qty * lot_size
    return kite_qty


# Lot-size index — built lazily from the instruments cache, rebuilt
# when the cache refreshes. Pre-fix `get_lot_size` did an O(N) linear
# scan over ~90k instruments on every call; the ticket route +
# basket-margin path called it 2-3 times per order. Now O(1) dict
# lookup. Same `_TICK_INDEX` pattern in routes/orders.py.
_LOT_INDEX: dict[tuple[str, str], int] = {}
_LOT_INDEX_STAMP: object | None = None


def _rebuild_lot_index(items) -> None:
    """Rebuild the (exchange, tradingsymbol) → lot_size dict from
    the instruments cache. Called once per cache refresh."""
    global _LOT_INDEX
    new_index: dict[tuple[str, str], int] = {}
    for inst in items:
        try:
            ls = int(inst.ls) if inst.ls > 0 else 1
        except (TypeError, ValueError):
            ls = 1
        new_index[(inst.e, inst.s)] = ls
    _LOT_INDEX = new_index


async def get_lot_size(exchange: str, tradingsymbol: str) -> int:
    """Look up lot_size from the instruments cache via `_LOT_INDEX`
    (O(1) dict lookup; rebuilt only when the cache version stamp
    flips). Returns 1 (safe no-op for to_kite_qty) when the cache
    is cold or the symbol isn't found.

    This is intentionally a best-effort read; it must never raise.
    """
    global _LOT_INDEX_STAMP
    try:
        from backend.api.cache import get_or_fetch
        from backend.api.routes.instruments import _fetch_instruments, _TTL_SECONDS
        resp = await get_or_fetch("instruments", _fetch_instruments,
                                  ttl_seconds=_TTL_SECONDS)
        if resp is not _LOT_INDEX_STAMP or not _LOT_INDEX:
            _rebuild_lot_index(resp.items if resp else [])
            _LOT_INDEX_STAMP = resp
    except Exception as e:
        logger.debug(f"[KITE-QTY] lot_size lookup failed for {exchange}/{tradingsymbol}: {e}")
        return 1
    return _LOT_INDEX.get((exchange, tradingsymbol), 1)


# Kite rejects orders with `tag` > 20 chars: "invalid tags - maximum
# allowed length is 20". Defensive truncation at the adapter layer so
# no caller (including a future one that forgets the limit) can bypass
# it. Operator may see a slightly clipped tag in their Kite order
# history, but the order goes through instead of being rejected.
_KITE_TAG_MAX = 20

def _truncate_tag(kwargs: dict[str, Any]) -> None:
    """In-place truncation of a Kite-bound order kwargs dict's `tag`
    field. No-op when `tag` is absent or None."""
    tag = kwargs.get("tag")
    if tag is None:
        return
    s = str(tag)
    if len(s) > _KITE_TAG_MAX:
        kwargs["tag"] = s[:_KITE_TAG_MAX]
        logger.warning(
            f"[KITE-TAG] tag truncated from {len(s)} → {_KITE_TAG_MAX} chars: "
            f"{s!r} → {kwargs['tag']!r}"
        )


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

    def order_status(self, order_id: str) -> dict:
        """Targeted single-order status via Kite's `order_history`
        endpoint — returns the order's full lifecycle (placed → open →
        complete / cancelled / rejected). Last entry is the current
        state. Used by the chase engine's 20-s status poll; replaces
        a full `orders()` round-trip with a single-order REST call.

        Kite returns an empty list when the order_id is unknown — we
        map that to an empty dict so callers see the same shape as
        the default `orders()` filter."""
        try:
            history = self.kite.order_history(str(order_id)) or []
        except Exception:
            return {}
        return history[-1] if history else {}

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
        _truncate_tag(kwargs)
        return self.kite.place_order(**kwargs)

    def modify_order(self, order_id: str, **kwargs: Any) -> str:
        _truncate_tag(kwargs)
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

    def cancel_gtt(self, gtt_id: str, *, exchange: str | None = None) -> str:
        # Kite SDK method is `delete_gtt`, not cancel — wrap for ABC consistency.
        # `exchange` is accepted for parity with the ABC + Dhan + Groww
        # signatures (the OCO pair-watcher passes it). Kite uses trigger_id
        # alone to identify the GTT so we just ignore it.
        del exchange  # unused on Kite
        resp = self.kite.delete_gtt(trigger_id=int(gtt_id))
        return str(resp.get("trigger_id", gtt_id) if isinstance(resp, dict) else gtt_id)

    def get_gtts(self) -> list[dict]:
        return self.kite.get_gtts()

    # ── Qty translation ───────────────────────────────────────────────

    def translate_qty(self, exchange: str, raw_qty: int,
                      lot_size: int) -> int:
        """MCX/NCO want qty=lots; every other Kite exchange wants
        qty=contracts. Delegates to the module-level `to_kite_qty`
        helper which encodes Kite's lot-vs-contract convention."""
        return to_kite_qty(exchange, raw_qty, lot_size)

    def normalise_qty(self, exchange: str, raw_qty: int,
                      lot_size: int) -> int:
        """Back-compat alias — prefer translate_qty in new code."""
        return self.translate_qty(exchange, raw_qty, lot_size)
