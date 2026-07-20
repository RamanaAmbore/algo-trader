"""
Zerodha Kite implementation of the `Broker` interface.

Wraps the existing `KiteConnection` (see
`backend/brokers/connections.py`) so auth, token caching, 23 h
refresh, multi-account IPv6 binding, and the parallel-login lock all
keep working exactly as they do today — this module is a thin typed
facade over that machinery.
"""

from __future__ import annotations

from typing import Any

from backend.brokers.base import Broker
from backend.brokers.connections import KiteConnection
from backend.brokers.errors import (
    BrokerAuthError, BrokerNetworkError, BrokerOrderError,
    BrokerInputError, BrokerError,
)
from backend.brokers.rate_limiter import TokenBucketLimiter
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)

# Kite Connect rate limits — https://kite.trade/docs/connect/v3/ — review on SDK upgrade
# historical data = 3 req/s per user; order placement = 10 req/s (conservative safe limit).
_KITE_RATE_LIMITER = TokenBucketLimiter({
    "history": (3, 1.0),   # 3 calls/s for historical data
    "orders":  (10, 1.0),  # 10 calls/s for order placement
})

# Maps kiteconnect SDK exception class names → typed BrokerError subclass.
# Used by _kite_exc() to convert SDK exceptions at the adapter boundary.
_KITE_ERROR_MAP: dict[str, type[BrokerError]] = {
    "TokenException":    BrokerAuthError,
    "NetworkException":  BrokerNetworkError,
    "OrderException":    BrokerOrderError,
    "InputException":    BrokerInputError,
    "DataException":     BrokerInputError,
    "GeneralException":  BrokerError,
}


def _kite_exc(e: Exception) -> BrokerError:
    """Wrap a kiteconnect SDK exception in the typed BrokerError hierarchy."""
    cls = _KITE_ERROR_MAP.get(type(e).__name__, BrokerError)
    return cls(str(e), broker="zerodha_kite", code=type(e).__name__)


def to_kite_qty(exchange: str, raw_qty: int, lot_size: int) -> int:
    """Translate raw contract qty to Kite's quantity convention.

    NSE/BSE/NFO/BFO/CDS/BCD: quantity = contracts. NSE F&O places
    qty=50 to trade 1 NIFTY lot (lot_size=50). MCX/NCO: quantity =
    LOTS. MCX CRUDEOIL with lot_size=100 wants qty=1 to trade 1 lot.
    Without this translation, a 1-lot MCX order ends up as 100 lots
    on Kite.

    SAFETY for MCX/NCO:
      - lot_size <= 1 (0 or 1) is ALWAYS a cache miss for any real MCX
        contract (smallest real lot_size is GOLDPETAL at 10g; every
        liquid MCX contract has lot_size >> 1). Dividing raw_qty by 1
        (or passing through on 0) sends raw_qty unchanged as LOTS —
        100× oversize for a 1-lot CRUDEOIL (lot_size=100) order.
        Raises ValueError so callers surface a safe 503/422 instead of
        silently sending a catastrophic position to the exchange.
      - lot_size > 1: translate contracts → lots normally.

    For non-MCX exchanges the function is always a no-op (Kite wants
    contracts everywhere else, which is what callers already hold).

    Only translates when raw_qty >= lot_size (operator typed contracts).
    Sub-lot-size values pass through as-is — better to let Kite reject
    an odd qty than silently divide and send a nonsensical number.
    """
    if exchange in ("MCX", "NCO"):
        if lot_size <= 1:
            # lot_size == 0 or 1 on MCX means instruments cache missed.
            # No real MCX contract has lot_size ≤ 1. Refuse rather than
            # sending raw_qty as lots (100× oversize incident on CRUDEOIL).
            raise ValueError(
                f"[KITE-QTY-GUARD] {exchange} lot_size={lot_size} for "
                f"qty={raw_qty} — instruments cache miss (no real MCX "
                f"contract has lot_size≤1). Refusing order to prevent "
                f"catastrophic oversize. Retry after cache warms."
            )
        if raw_qty >= lot_size:
            translated = max(1, raw_qty // lot_size)
            if translated != raw_qty:
                logger.info(
                    f"[KITE-QTY] {exchange}: contracts={raw_qty} → lots={translated} "
                    f"(lot_size={lot_size})"
                )
            return translated
        # raw_qty < lot_size — sub-lot: Kite will likely reject the order.
        # Log a warning so there is a clear audit trail before the SDK call.
        logger.warning(
            "[QTY-GUARD] sub-lot qty=%d < lot_size=%d for sym=%s — Kite will likely reject",
            raw_qty, lot_size, exchange,
        )
        return raw_qty
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
    flips).

    Return convention:
      - Found in cache with lot_size > 1: returns actual lot_size.
      - Found in cache with lot_size == 1 (equity / micro): returns 1.
      - NOT found / cache cold: returns 0 (sentinel for "unknown").

    Callers must handle 0 for MCX/NCO — to_kite_qty raises ValueError
    when lot_size == 1 on MCX (likely cache miss); a 0 return lets the
    route layer raise a clean 503 before invoking to_kite_qty.

    For non-MCX exchanges the fallback was always 1 (no translation),
    which is still safe — we preserve that by returning 1 for non-MCX
    misses so existing NSE/NFO paths are unaffected.
    """
    global _LOT_INDEX_STAMP
    _mcx = exchange in ("MCX", "NCO")
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
        # For MCX: return 0 (unknown) so callers can refuse safely.
        # For non-MCX: return 1 (no-op — same as before).
        return 0 if _mcx else 1
    # Cache miss sentinel: 0 for MCX (dangerous to assume), 1 for non-MCX (safe no-op).
    return _LOT_INDEX.get((exchange, tradingsymbol), 0 if _mcx else 1)


# Kite rejects orders with `tag` > 20 chars: "invalid tags - maximum
# allowed length is 20". Defensive truncation at the adapter layer so
# no caller (including a future one that forgets the limit) can bypass
# it. Operator may see a slightly clipped tag in their Kite order
# history, but the order goes through instead of being rejected.
_KITE_TAG_MAX = 20

def _validate_kite_order_prices(
    order_type: str,
    price: Any,
    trigger_price: Any,
) -> None:
    """Validate that price / trigger_price fields are populated for
    order types that require them.

    Raises BrokerOrderError with a descriptive message before the SDK
    call so callers receive a typed error rather than an opaque
    kiteconnect InputException.
    """
    if order_type in ("LIMIT", "LMT"):
        if not (float(price or 0) > 0):
            raise BrokerOrderError(
                "LIMIT order requires price > 0", broker="kite"
            )
    if order_type in ("SL", "SL-M", "STOP_LOSS"):
        if not (float(trigger_price or 0) > 0):
            raise BrokerOrderError(
                "SL order requires trigger_price > 0", broker="kite"
            )
    if order_type == "SL":
        # SL (not SL-M) also requires a limit price for the order leg.
        if not (float(price or 0) > 0):
            raise BrokerOrderError(
                "SL order requires price > 0", broker="kite"
            )


def _check_kite_qty_ceiling(
    exchange: str,
    qty: int,
    symbol: str,
    is_close: bool,
) -> None:
    """Last-line-of-defence absurd-qty ceiling for place_order.

    MCX/NCO qty is LOTS on Kite.  50 lots is an exceptional but
    plausible institutional close; more than that on a new open order
    is almost certainly a typo.

    NFO/CDS/BFO qty is CONTRACTS.  50 000 catches 5-digit numeric
    typos while allowing large index-option books.

    Close orders (intent="close") bypass both ceilings so a full
    position unwind of any size can go through.
    """
    if not is_close and exchange in ("MCX", "NCO") and qty > 50:
        logger.error(
            "[ADAPTER-QTY-CEILING] REFUSING %s %s: qty=%s (MCX/NCO lots) "
            "> 50-lot absurd-value ceiling.", exchange, symbol, qty,
        )
        raise ValueError(
            f"[ADAPTER-QTY-CEILING] {exchange} qty={qty} exceeds 50-lot "
            f"absurd-value ceiling for {symbol}. Refusing at adapter layer."
        )
    if not is_close and exchange in ("NFO", "CDS", "BFO") and qty > 50000:
        logger.error(
            "[ADAPTER-QTY-CEILING] REFUSING %s %s: qty=%s > 50000-contract "
            "absurd-value ceiling.", exchange, symbol, qty,
        )
        raise ValueError(
            f"[ADAPTER-QTY-CEILING] {exchange} qty={qty} exceeds 50000-"
            f"contract absurd-value ceiling for {symbol}. Refusing at adapter layer."
        )
    if is_close and qty > 50 and exchange in ("MCX", "NCO"):
        logger.info(
            "[ADAPTER-QTY-CEILING] close-intent bypass: %s %s qty=%s lots "
            "(ceiling skipped for position unwind).", exchange, symbol, qty,
        )


def _check_kite_gtt_qty_ceiling(
    exchange: str,
    orders: list[dict],
    tradingsymbol: str,
) -> None:
    """Apply absurd-qty ceiling to each GTT leg. GTT has no close-intent
    so the ceiling is always enforced (no bypass)."""
    _exch = exchange.upper()
    for _leg in orders:
        _kqty = int(_leg.get("quantity") or 0)
        if _exch in ("MCX", "NCO") and _kqty > 50:
            logger.error(
                "[ADAPTER-GTT-QTY-CEILING] REFUSING GTT %s %s: leg qty=%s "
                "(MCX/NCO lots) > 50-lot absurd-value ceiling.",
                _exch, tradingsymbol, _kqty,
            )
            raise ValueError(
                f"[ADAPTER-GTT-QTY-CEILING] {_exch} GTT leg qty={_kqty} "
                f"exceeds 50-lot absurd-value ceiling for {tradingsymbol}. "
                f"Refusing at adapter layer — translate_qty must be called "
                f"before place_gtt on MCX/NCO."
            )
        if _exch in ("NFO", "CDS", "BFO") and _kqty > 50000:
            logger.error(
                "[ADAPTER-GTT-QTY-CEILING] REFUSING GTT %s %s: leg qty=%s "
                "> 50000-contract absurd-value ceiling.", _exch, tradingsymbol, _kqty,
            )
            raise ValueError(
                f"[ADAPTER-GTT-QTY-CEILING] {_exch} GTT leg qty={_kqty} "
                f"exceeds 50000-contract absurd-value ceiling for {tradingsymbol}."
            )


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
        super().__init__()
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
        _KITE_RATE_LIMITER.throttle("history")
        return self.kite.historical_data(instrument_token, from_date,
                                         to_date, interval)

    def holidays(self, exchange: str) -> set[str]:
        return self.kite.holidays(exchange)

    # ── Order entry ───────────────────────────────────────────────────

    def basket_order_margins(self, orders: list[dict]) -> list[dict]:
        return self.kite.basket_order_margins(orders)

    def place_order(self, *, intent: str | None = None, **kwargs: Any) -> str:
        _truncate_tag(kwargs)
        # M3 — LIMIT/SL price validation + absurd-qty ceiling before SDK call.
        # Both checks are pure logic extracted to module-level helpers to keep
        # this method's CC below D.
        _validate_kite_order_prices(
            str(kwargs.get("order_type") or "").upper(),
            kwargs.get("price"),
            kwargs.get("trigger_price"),
        )
        _check_kite_qty_ceiling(
            str(kwargs.get("exchange") or "").upper(),
            int(kwargs.get("quantity") or 0),
            str(kwargs.get("tradingsymbol") or ""),
            (intent or "").lower() == "close",
        )
        _KITE_RATE_LIMITER.throttle("orders")
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
        # M3 — GTT trigger-value + leg price validation before SDK call.
        # trigger_values is the per-row activation price for Kite GTT;
        # every value must be positive or Kite will reject with InputException.
        for _tv in trigger_values:
            if not (float(_tv or 0) > 0):
                raise BrokerOrderError(
                    f"GTT trigger_value must be > 0, got {_tv!r}", broker="kite"
                )
        # Each GTT leg with a LIMIT order_type must carry a positive price.
        # SL/SL-M legs must carry a positive trigger_price; SL also needs price > 0.
        for _leg in orders:
            _leg_ot = str(_leg.get("order_type") or "").upper()
            if _leg_ot in ("LIMIT", "LMT"):
                if not (float(_leg.get("price") or 0) > 0):
                    raise BrokerOrderError(
                        f"GTT leg with order_type={_leg_ot!r} requires price > 0",
                        broker="kite",
                    )
            if _leg_ot in ("SL", "SL-M", "STOP_LOSS"):
                if not (float(_leg.get("trigger_price") or 0) > 0):
                    raise BrokerOrderError(
                        f"GTT leg with order_type={_leg_ot!r} requires trigger_price > 0",
                        broker="kite",
                    )
            if _leg_ot == "SL":
                if not (float(_leg.get("price") or 0) > 0):
                    raise BrokerOrderError(
                        f"GTT SL leg requires price > 0 (limit price for the order leg)",
                        broker="kite",
                    )
        # LAST-LINE DEFENSE (GTT layer) — same ceiling as place_order but
        # applied to each GTT leg. GTT legs arrive with qty already
        # translated to lots (template_attach.apply_plan_live calls
        # translate_qty before this). Any leg still carrying raw contracts
        # (100 for CRUDEOIL) would be an untranslated qty leak — catch it
        # here before it hits the exchange.
        _check_kite_gtt_qty_ceiling(exchange, orders, tradingsymbol)
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
