"""
LiveQuoteSource — real broker bid/ask for the mode-2 paper engine.

Calls the broker's quote endpoint via the `Broker` adapter so the same
engine that fills sim orders against fabricated prices can fill paper
orders against live Kite ticks. Bid/ask is derived from the broker's
returned depth when present, otherwise from `last_price` ± half of
`simulator.default_spread_pct` (same knob the simulator uses).
"""

from __future__ import annotations

from backend.api.algo.quote.base import QuoteSource
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


class LiveQuoteSource(QuoteSource):
    """Bid/ask from `broker.quote(...)` (preferred) or `broker.ltp(...)`
    (fallback). `prefetch_for` does one batched `broker.quote([many])`
    per account per tick so 10 open orders cost 1 round-trip per
    account, not 10. `bid_ask_for_order` then reads from the cache
    populated by the prefetch."""

    def __init__(self) -> None:
        # Cache populated by prefetch_for; key = "EXCHANGE:tradingsymbol".
        # Cleared at the start of every prefetch.
        self._cache: dict[str, dict] = {}

    def prefetch_for(self, orders: list[dict]) -> None:
        from backend.brokers import get_broker
        # Bucket orders by account so each broker handle gets exactly
        # one quote call. Within each bucket, collect distinct keys.
        by_account: dict[str, set[str]] = {}
        for o in orders:
            acct = str(o.get("account") or "")
            sym  = str(o.get("symbol")  or "")
            exch = str(o.get("exchange") or "NFO")
            if not (acct and sym):
                continue
            by_account.setdefault(acct, set()).add(f"{exch}:{sym}")

        new_cache: dict[str, dict] = {}
        for acct, keys in by_account.items():
            try:
                broker = get_broker(acct)
                resp   = broker.quote(list(keys)) or {}
            except Exception as e:
                logger.debug(f"Live quote prefetch failed for {acct}: {e}")
                continue
            for k, v in resp.items():
                new_cache[k] = v or {}
        self._cache = new_cache

    def bid_ask_for_order(self, order: dict) -> tuple[float | None, float | None]:
        from backend.shared.helpers.settings import get_float

        account  = str(order.get("account") or "")
        symbol   = str(order.get("symbol") or "")
        exchange = str(order.get("exchange") or "NFO")
        if not (account and symbol):
            return None, None

        key   = f"{exchange}:{symbol}"
        quote = self._cache.get(key)
        if quote is None:
            # Cache miss — fall back to a single-key fetch. This shouldn't
            # happen on the engine's hot path (prefetch_for runs first),
            # but action handlers calling bid_ask_for_order ad-hoc need
            # something sensible.
            from backend.brokers import get_broker
            try:
                broker = get_broker(account)
                quote  = broker.quote([key]).get(key) or {}
            except Exception as e:
                logger.debug(f"Live quote miss for {key} on {account}: {e}")
                return None, None

        # Prefer real depth from the broker's quote response.
        depth = quote.get("depth") or {}
        buy_book  = depth.get("buy")  or []
        sell_book = depth.get("sell") or []
        bid = buy_book[0].get("price")  if buy_book  else None
        ask = sell_book[0].get("price") if sell_book else None
        if bid and ask:
            return float(bid), float(ask)

        # Fall back to LTP ± half-spread when depth is empty (illiquid
        # contract, off-hours, etc.). Same default the simulator uses.
        ltp = quote.get("last_price")
        if ltp is None:
            return None, None
        spread = max(0.0, get_float("simulator.default_spread_pct", 0.10) / 100.0)
        half   = spread / 2.0
        ltp    = float(ltp)
        return ltp * (1.0 - half), ltp * (1.0 + half)
