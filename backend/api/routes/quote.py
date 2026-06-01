"""
Market quote endpoint — returns LTP + tick-size for a single instrument.
Used by the frontend command bar to suggest LIMIT prices around current price.

GET  /api/quote/?exchange=NSE&tradingsymbol=RELIANCE  → { ltp, tick_size }
POST /api/quotes/sparkline                            → { data, refreshed_at }
"""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

import msgspec
from litestar import Controller, get, post
from litestar.exceptions import HTTPException
from litestar.params import Parameter

from backend.api.auth_guard import auth_or_demo_guard
from backend.shared.helpers.connections import Connections
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


class DepthLevel(msgspec.Struct):
    price: float
    quantity: int
    orders: int = 0


class QuoteResponse(msgspec.Struct):
    tradingsymbol: str
    exchange: str
    ltp: float
    tick_size: float
    bid: Optional[float] = None
    ask: Optional[float] = None
    depth_buy: list[DepthLevel] = []
    depth_sell: list[DepthLevel] = []
    volume: int = 0


def _fetch_ltp(exchange: str, tradingsymbol: str) -> QuoteResponse:
    # Shared market-data fetch — route through get_price_broker() so
    # the operator's `connections.price_account` setting decides which
    # account's API handle services chart-data calls. Broker-agnostic
    # path; any vendor's adapter will work the same.
    from backend.shared.brokers.registry import get_price_broker
    broker = get_price_broker()
    key = f"{exchange}:{tradingsymbol}"

    bid = ask = None
    depth_buy: list[DepthLevel] = []
    depth_sell: list[DepthLevel] = []
    volume = 0
    ltp = 0.0

    try:
        full = broker.quote([key]).get(key) or {}
        ltp = float(full.get("last_price") or 0.0)
        volume = int(full.get("volume") or 0)
        depth = full.get("depth") or {}
        for level in (depth.get("buy") or [])[:5]:
            p, q, o = float(level.get("price") or 0), int(level.get("quantity") or 0), int(level.get("orders") or 0)
            if p > 0:
                depth_buy.append(DepthLevel(price=p, quantity=q, orders=o))
        for level in (depth.get("sell") or [])[:5]:
            p, q, o = float(level.get("price") or 0), int(level.get("quantity") or 0), int(level.get("orders") or 0)
            if p > 0:
                depth_sell.append(DepthLevel(price=p, quantity=q, orders=o))
        if depth_buy:
            bid = depth_buy[0].price
        if depth_sell:
            ask = depth_sell[0].price
    except Exception as e:
        # Fallback to ltp-only
        logger.warning(f"Quote depth failed for {key}: {e}")
        try:
            data = broker.ltp([key])
            row = data.get(key) or {}
            ltp = float(row.get("last_price") or 0.0)
        except Exception as e2:
            logger.error(f"Quote LTP fallback failed for {key}: {e2}")

    return QuoteResponse(
        tradingsymbol=tradingsymbol,
        exchange=exchange,
        ltp=ltp,
        tick_size=0.05,
        bid=bid,
        ask=ask,
        depth_buy=depth_buy,
        depth_sell=depth_sell,
        volume=volume,
    )


class BatchQuoteRow(msgspec.Struct):
    exchange: str
    tradingsymbol: str
    ltp: float = 0.0
    bid: Optional[float] = None
    ask: Optional[float] = None
    open: Optional[float] = None   # Today's open (first traded price)
    close: Optional[float] = None  # Yesterday's close (used to compute day_change)
    change: float = 0.0
    change_pct: float = 0.0
    volume: int = 0
    # Open interest — only meaningful for F&O symbols. Kite's quote()
    # returns `oi` on futures + options responses (0 / absent on cash
    # equities). Surfaced on /pulse so the operator can read liquidity
    # alongside LTP without opening a separate chain view.
    oi: int = 0
    stale: bool = False


class BatchQuoteRequest(msgspec.Struct):
    """Body: {keys: ["NSE:NIFTY 50", "MCX:GOLD26JUNFUT", ...]}"""
    keys: list[str]


class BatchQuoteResponse(msgspec.Struct):
    refreshed_at: str
    items: list[BatchQuoteRow]


class QuoteController(Controller):
    path = "/api/quote"
    guards = [auth_or_demo_guard]

    @get("/")
    async def get_quote(
        self,
        exchange: str = Parameter(required=True),
        tradingsymbol: str = Parameter(required=True),
    ) -> QuoteResponse:
        try:
            return _fetch_ltp(exchange, tradingsymbol)
        except Exception as e:
            logger.error(f"Quote API error: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @post("/batch")
    async def batch_quote(self, data: BatchQuoteRequest) -> BatchQuoteResponse:
        """One batched broker.quote() across an arbitrary key list.
        Used by the unified market-pulse view on /watchlist to pull
        live LTP / day-change for positions + holdings + underlyings
        without N round-trips."""
        import asyncio
        from datetime import datetime, timezone
        from backend.shared.brokers.registry import get_price_broker

        keys = list({k.strip() for k in (data.keys or []) if k and ":" in k})
        # Soft cap — Kite quote() handles ~500 keys but the UI shouldn't
        # ask for more than this in one tab. Trim silently.
        keys = keys[:300]

        quote_data: dict = {}
        if keys:
            try:
                broker = get_price_broker()
                quote_data = await asyncio.to_thread(broker.quote, keys) or {}
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"Batch quote failed: {exc}")
                quote_data = {}

        items: list[BatchQuoteRow] = []
        for k in keys:
            q = quote_data.get(k) or {}
            try:
                exch, sym = k.split(":", 1)
            except ValueError:
                continue
            ltp    = float(q.get("last_price") or 0.0)
            ohlc   = q.get("ohlc") or {}
            close  = float(ohlc.get("close") or 0.0) or None
            open_  = float(ohlc.get("open")  or 0.0) or None
            depth  = q.get("depth") or {}
            buys   = depth.get("buy") or []
            sells  = depth.get("sell") or []
            bid    = float(buys[0]["price"])  if buys  and (buys[0].get("price") or 0)  else None
            ask    = float(sells[0]["price"]) if sells and (sells[0].get("price") or 0) else None
            change = (ltp - close) if (close and ltp) else 0.0
            chg_pct = (change / close * 100.0) if close else 0.0
            items.append(BatchQuoteRow(
                exchange=exch, tradingsymbol=sym,
                ltp=ltp, bid=bid, ask=ask, open=open_, close=close,
                change=change, change_pct=chg_pct,
                volume=int(q.get("volume") or 0),
                oi=int(q.get("oi") or 0),
                stale=(not q),
            ))
        return BatchQuoteResponse(
            refreshed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            items=items,
        )


# ── Sparkline cache ───────────────────────────────────────────────────────────
# Keyed by (tradingsymbol, exchange, days, ist_date_str) → list[float].
# TTL is end-of-trading-day: daily closes don't change intraday.
# Simple dict + lock; no need for the heavier get_or_fetch machinery
# (cache is keyed per-symbol, eviction is lazy on date change).

_spark_cache: dict[tuple, list[float]] = {}
_spark_lock  = threading.Lock()

def _ist_today() -> str:
    """Return today's date in IST as YYYY-MM-DD."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d")
    except Exception:
        # Fallback: UTC+5:30
        return (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d")


def _evict_stale(today: str) -> None:
    """Drop cache entries whose date != today (lazy eviction on each batch call)."""
    stale = [k for k in _spark_cache if k[3] != today]
    for k in stale:
        del _spark_cache[k]


# ── Sparkline schemas ─────────────────────────────────────────────────────────

class SparklineSymbol(msgspec.Struct):
    tradingsymbol: str
    exchange: str


class SparklineRequest(msgspec.Struct):
    symbols: list[SparklineSymbol]
    days: int = 5


class SparklineResponse(msgspec.Struct):
    data: dict[str, list[float]]   # tradingsymbol → [close1, …, closeN] oldest first
    refreshed_at: str


class SparklineController(Controller):
    path = "/api/quotes"
    guards = [auth_or_demo_guard]

    @post("/sparkline")
    async def batch_sparkline(self, data: SparklineRequest) -> SparklineResponse:
        """
        POST /api/quotes/sparkline

        Returns the last N daily close prices (oldest first) for each
        requested symbol. Used by the /pulse sparkline column.

        Instrument-token lookup uses the broker's instruments dump (same
        path as OptionsController.historical). kite.historical_data calls
        are parallelised via asyncio.gather with a concurrency cap of 5
        to respect Kite's rate limits. Daily closes are cached per
        (symbol, exchange, days, IST date) until midnight IST so repeated
        grid refreshes within the trading day are free.

        Missing / un-resolvable symbols are silently omitted (no 404).
        Broker unreachable → empty data dict instead of 502.
        """
        # Sparkline uses get_sparkline_broker (different Kite account
        # than chart-historical when two are loaded) so the two read
        # workloads don't contend for the same 3 req/sec budget.
        from backend.shared.brokers.registry import get_sparkline_broker

        syms = data.symbols or []
        if not syms:
            return SparklineResponse(data={}, refreshed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        if len(syms) > 100:
            raise HTTPException(status_code=400, detail="symbols cap is 100")

        days = max(1, min(int(data.days), 90))
        today = _ist_today()

        # Lazy cache eviction.
        with _spark_lock:
            _evict_stale(today)

        # Partition into cached vs. need-fetch.
        result: dict[str, list[float]] = {}
        to_fetch: list[SparklineSymbol] = []

        for sym_obj in syms:
            sym = sym_obj.tradingsymbol.upper().strip()
            exch = (sym_obj.exchange or "NSE").upper().strip()
            cache_key = (sym, exch, days, today)
            with _spark_lock:
                cached = _spark_cache.get(cache_key)
            if cached is not None:
                result[sym] = cached
            else:
                to_fetch.append(SparklineSymbol(tradingsymbol=sym, exchange=exch))

        if not to_fetch:
            return SparklineResponse(
                data=result,
                refreshed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )

        # Resolve instrument tokens for all un-cached symbols.
        try:
            broker = get_sparkline_broker()
        except Exception as exc:
            logger.warning(f"sparkline: broker unavailable: {exc}")
            return SparklineResponse(data=result, refreshed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))

        # Build token map: tradingsymbol → instrument_token.
        # Walk the preferred exchange first, then common fallbacks.
        token_map: dict[str, int] = {}
        exchange_order: dict[str, list[str]] = {}
        for sym_obj in to_fetch:
            sym = sym_obj.tradingsymbol
            exch = sym_obj.exchange
            # Walk: declared exchange → NFO → BFO → NSE → BSE
            order = [exch] + [e for e in ("NFO", "BFO", "NSE", "BSE") if e != exch]
            exchange_order[sym] = order

        try:
            # Collect distinct exchanges to query instruments once per exchange.
            needed_exchanges: set[str] = set()
            for order in exchange_order.values():
                needed_exchanges.update(order)

            inst_by_exch: dict[str, list] = {}
            for ex in needed_exchanges:
                try:
                    insts = await asyncio.to_thread(broker.instruments, ex) or []
                    inst_by_exch[ex] = insts
                except Exception:
                    inst_by_exch[ex] = []

            for sym_obj in to_fetch:
                sym = sym_obj.tradingsymbol
                for ex in exchange_order[sym]:
                    for inst in inst_by_exch.get(ex, []):
                        if str(inst.get("tradingsymbol") or "").upper() == sym:
                            token_map[sym] = int(inst["instrument_token"])
                            break
                    if sym in token_map:
                        break
        except Exception as exc:
            logger.warning(f"sparkline: instrument lookup failed: {exc}")
            return SparklineResponse(data=result, refreshed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))

        # Fetch historical data for all resolved tokens. Kite limits
        # historical_data to 3 req/sec; the prior Semaphore(5) easily
        # blew past that on cold-start (50+ symbols in Pulse → instant
        # rate-limit storm + cool-off cascade). Now: Semaphore(2) +
        # 350 ms pacing per completion → effective rate ≤ ~5 req/sec
        # across the 2 in-flight slots, well inside Kite's headroom.
        # broker-agnostic — `historical_data` is on the Broker ABC.
        to_d   = datetime.now()
        from_d = to_d - timedelta(days=days + 5)  # +5 buffer for weekends/holidays

        sem = asyncio.Semaphore(2)
        _KITE_PACE_S = 0.35  # sleep between completions inside each slot

        async def _fetch_closes(sym: str, token: int) -> tuple[str, list[float]]:
            async with sem:
                try:
                    raw = await asyncio.to_thread(
                        broker.historical_data, token, from_d, to_d, "day"
                    ) or []
                    closes = [float(b["close"]) for b in raw if b.get("close") is not None]
                    # Keep the last `days` closes (oldest first).
                    result = closes[-days:] if len(closes) > days else closes
                    await asyncio.sleep(_KITE_PACE_S)
                    return sym, result
                except Exception as exc:
                    logger.warning(f"sparkline historical_data failed for {sym}: {exc}")
                    await asyncio.sleep(_KITE_PACE_S)
                    return sym, []

        tasks = [
            _fetch_closes(sym_obj.tradingsymbol, token_map[sym_obj.tradingsymbol])
            for sym_obj in to_fetch
            if sym_obj.tradingsymbol in token_map
        ]

        if tasks:
            fetched = await asyncio.gather(*tasks)
            with _spark_lock:
                for sym, closes in fetched:
                    if closes:  # omit symbols that returned nothing
                        exch = next(
                            (s.exchange for s in to_fetch if s.tradingsymbol == sym), "NSE"
                        )
                        cache_key = (sym, exch, days, today)
                        _spark_cache[cache_key] = closes
                        result[sym] = closes

        return SparklineResponse(
            data=result,
            refreshed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
