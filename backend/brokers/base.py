"""
Broker abstract base. See `backend/brokers/__init__.py` for the
extension contract. Every method here corresponds to a capability the
rest of the codebase depends on — if a new vendor doesn't natively
expose one, the adapter should either synthesise the result or raise a
clear `NotImplementedError` with a pointer to what the caller needs to
handle.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from backend.brokers.capabilities import (
    BrokerCapabilities,
    capabilities_for_broker_id,
)


class Broker(ABC):
    """
    Broker-agnostic interface.

    Diagnostics — every adapter populates _last_req / _last_resp before /
    after each HTTP call so operator debugging doesn't require log-diving:
      broker.last_request_debug() → {"request": {...}, "response": {...}}
    Adapters update these dicts in their HTTP dispatch path (_safe_call,
    _retry_groww_auth, etc.). Base initialises them to empty dicts.

    Conventions shared by every adapter:
      - `account` is the RamboQuant-internal account code (e.g. "ZG0790").
      - `broker_id` is the canonical vendor identifier stored in
        `broker_accounts.broker_id` (e.g. "zerodha_kite", "upstox").
      - Every method returns broker-native response shapes that the
        codebase already consumes. Specifically:
          * holdings / positions / margins / orders — list[dict] or
            dict matching the Zerodha Kite shape the summarise helpers
            expect. Adapters for other brokers normalise to this shape
            so callers don't branch per vendor.
          * ltp / quote — dict keyed by broker-formatted symbol.
          * instruments — list[dict] with tradingsymbol / instrument_token
            / exchange / expiry / strike / lot_size columns.
          * holidays — set[str] of ISO dates.
          * historical_data — list[dict] with date/open/high/low/close/volume.
      - Re-authentication / token refresh is owned by the adapter; the
        caller should never have to check connection health.

    Escape hatch: adapters may expose an underlying SDK handle (e.g.
    `KiteBroker.kite`) for features that haven't been lifted into the
    interface yet. Any new use of that handle is a smell — prefer to
    add the operation to this ABC.
    """

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)

    # Populated by adapters in their HTTP dispatch path (_safe_call, etc.)
    _last_req: dict
    _last_resp: dict

    def __init__(self) -> None:
        self._last_req: dict = {}
        self._last_resp: dict = {}

    def last_request_debug(self) -> dict:
        """Return the most recent request + response metadata for debugging.
        Adapters populate _last_req / _last_resp; base returns both."""
        return {"request": dict(self._last_req), "response": dict(self._last_resp)}

    @property
    @abstractmethod
    def account(self) -> str:
        """RamboQuant account code (e.g. "ZG0790")."""

    @property
    @abstractmethod
    def broker_id(self) -> str:
        """Canonical broker vendor identifier (e.g. "zerodha_kite").
        Must match the value stored in broker_accounts.broker_id and
        the key registered in registry._ADAPTERS."""

    @property
    def capabilities(self) -> BrokerCapabilities:
        """What this broker can do natively. Looked up from the
        capability matrix via `broker_id` — adapters override only
        when they need to declare something different per-account
        (e.g. a tier-restricted Dhan account that doesn't have the
        full Forever-OCO quota). Default reads the matrix verbatim."""
        return capabilities_for_broker_id(self.broker_id)

    # ── Account state ─────────────────────────────────────────────────

    @abstractmethod
    def profile(self) -> dict: ...

    @abstractmethod
    def holdings(self) -> list[dict]: ...

    @abstractmethod
    def positions(self) -> dict:
        """Return positions (typically keyed by `net` / `day` buckets)."""

    @abstractmethod
    def margins(self, segment: str | None = None) -> dict: ...

    @abstractmethod
    def orders(self) -> list[dict]: ...

    def order_status(self, order_id: str) -> dict:
        """Return the latest status snapshot for a single order. Used by
        the chase engine to poll status without paying for a full
        `orders()` round-trip on every 20-s cycle.

        Default implementation filters `orders()` — works for every
        broker but is wasteful when the SDK exposes a targeted
        single-order endpoint. Kite overrides this with
        `kite.order_history(order_id)` which fetches only the
        requested order's lifecycle. Dhan / Groww keep the default
        until their SDKs expose an equivalent.

        Returns the matching order dict (Kite-shape — `status`,
        `filled_quantity`, `average_price`, `status_message`, …) or
        an empty dict when the order_id isn't in the broker's day
        book (rare — usually means it was placed under a different
        account)."""
        for o in self.orders():
            if str(o.get("order_id")) == str(order_id):
                return o
        return {}

    @abstractmethod
    def trades(self) -> list[dict]:
        """Executed trades for the current trading day. Returns Kite-shape
        rows: tradingsymbol, exchange, order_id, transaction_type, quantity,
        average_price, exchange_timestamp."""

    # ── Market data ───────────────────────────────────────────────────

    @abstractmethod
    def ltp(self, symbols: list[str]) -> dict: ...

    @abstractmethod
    def quote(self, symbols: list[str]) -> dict: ...

    @abstractmethod
    def instruments(self, exchange: str | None = None) -> list[dict]: ...

    @abstractmethod
    def historical_data(
        self,
        instrument_token: int,
        from_date: Any,
        to_date: Any,
        interval: str = "day",
    ) -> list[dict]:
        """OHLCV candles for the given instrument token and date range.

        `from_date` / `to_date` accept anything the broker SDK accepts
        (datetime objects or ISO strings). Returns a list of dicts with
        at minimum: date, open, high, low, close, volume."""

    @abstractmethod
    def holidays(self, exchange: str) -> set[str]: ...

    def market_status(self, exchange: str) -> bool | None:
        """Return True / False if the broker exposes a market-status
        endpoint for `exchange`; None when the adapter doesn't
        implement one or the call fails.

        Optional method — not abstract. Adapters override when the
        broker's SDK ships a market-status / exchange-hours endpoint
        (Dhan + Groww have variants). Kite Connect has no such API
        — the fallback in `shared/helpers/market_probe.py` runs the
        bellwether-quote check instead.

        Used by `probe_market_active()` to skip the bellwether-quote
        round-trip when authoritative data is available. Caching
        happens at the probe layer; adapter doesn't need to memoise.
        """
        return None

    # ── Order entry ───────────────────────────────────────────────────

    @abstractmethod
    def basket_order_margins(self, orders: list[dict]) -> list[dict]:
        """Validate a basket of orders and return margin requirements.

        Each input dict follows Kite's basket_order_margins shape
        (tradingsymbol, exchange, transaction_type, variety, product,
        order_type, quantity, price). Returns the broker's margin
        response list, one entry per order."""

    @abstractmethod
    def place_order(self, *, intent: str | None = None, **kwargs: Any) -> str:
        """Place a single order. Returns the broker order id.

        `intent` is a RamboQuant-level hint — ``"close"`` signals that this
        order is reducing an existing position. Adapter-layer safety ceilings
        (e.g. the 50-lot MCX absurd-value guard in KiteBroker) are bypassed
        when ``intent == "close"`` because a legitimate full-position unwind
        may exceed the ceiling that guards against typo-driven new opens.
        The kwarg is consumed by the adapter and never forwarded to the
        broker SDK.
        """

    @abstractmethod
    def modify_order(self, order_id: str, **kwargs: Any) -> str: ...

    @abstractmethod
    def cancel_order(self, order_id: str, **kwargs: Any) -> str: ...

    # ── GTT / trigger orders ──────────────────────────────────────────
    #
    # Templates (see backend/api/algo/templates.py — Phase 3) translate
    # a TP/SL choice into one of these calls. Adapters whose vendor
    # doesn't natively support a feature (e.g. Groww + OCO) MUST raise
    # NotImplementedError from this layer — the orchestrator above
    # reads BrokerCapabilities BEFORE dispatching, so this method only
    # fires when capabilities say it should.

    def place_gtt(
        self,
        *,
        trigger_type: str,   # "single" | "two-leg" (OCO)
        tradingsymbol: str,
        exchange: str,
        last_price: float,
        orders: list[dict],  # one dict per leg: {transaction_type, quantity, price, order_type, product}
        trigger_values: list[float],  # one float per leg
        tag: str | None = None,
    ) -> str:
        """Place a GTT (broker-native trigger order). Returns the broker
        GTT id. Default raises so unimplemented adapters surface a
        clear error instead of silently no-op'ing."""
        raise NotImplementedError(
            f"{self.broker_id} adapter has not implemented place_gtt"
        )

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
        """Modify an existing GTT. Returns the (possibly new) broker GTT
        id — vendors that cancel+replace under the hood may return a
        different id; callers must use the return value, not the input."""
        raise NotImplementedError(
            f"{self.broker_id} adapter has not implemented modify_gtt"
        )

    def cancel_gtt(self, gtt_id: str, *, exchange: str | None = None) -> str:
        """Cancel a GTT. Returns the cancelled GTT id.

        `exchange` is an optional hint some adapters use to skip a
        cross-segment blind retry (Groww needs the segment up-front
        because cancel_smart_order requires it). Adapters that don't
        need it should ignore the kwarg.
        """
        raise NotImplementedError(
            f"{self.broker_id} adapter has not implemented cancel_gtt"
        )

    def get_gtts(self) -> list[dict]:
        """List every active GTT on this account. Each row carries at
        minimum: gtt_id, status (active/triggered/cancelled),
        trigger_type, tradingsymbol, exchange, trigger_values,
        last_price, orders (list of leg dicts), created_at."""
        raise NotImplementedError(
            f"{self.broker_id} adapter has not implemented get_gtts"
        )

    # ── Per-broker qty translation ────────────────────────────────────

    def translate_qty(self, exchange: str, raw_qty: int,
                      lot_size: int) -> int:
        """Translate operator-supplied qty to the unit the broker's
        place_order API expects.

        Semantics: `raw_qty` is in operator-friendly units — contracts
        for NSE/BSE/NFO/BFO (e.g. 50 = 1 NIFTY lot), or lots for MCX
        on Kite (e.g. 1 = 1 lot of CRUDEOIL). The return value is the
        exact integer to pass to the broker's place_order `quantity`
        field. Each adapter overrides this to match its own convention.

        Default is a no-op (returns `raw_qty` unchanged) — correct for
        Dhan and Groww which always want qty in contracts. Kite overrides
        for MCX/NCO where qty=lots is required.

        Only called when `lot_size > 0` and the caller knows the
        symbol's lot_size (best-effort via the instruments cache). When
        the lookup fails, `raw_qty` is passed through unchanged and the
        broker provides an explicit rejection if the qty is wrong."""
        return raw_qty

    def normalise_qty(self, exchange: str, raw_qty: int,
                      lot_size: int) -> int:
        """Back-compat alias for translate_qty. Prefer translate_qty in
        new code."""
        return self.translate_qty(exchange, raw_qty, lot_size)
