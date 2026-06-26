"""
Market quote endpoint — returns LTP + tick-size for a single instrument.
Used by the frontend command bar to suggest LIMIT prices around current price.

GET  /api/quote/?exchange=NSE&tradingsymbol=RELIANCE  → { ltp, tick_size }
POST /api/quotes/sparkline                            → { data, refreshed_at }
GET  /api/quotes/stream                               → SSE LTP tick stream
"""

from __future__ import annotations

import asyncio
import json
import threading
from datetime import date, datetime, timedelta, timezone
from typing import AsyncGenerator, Optional

import msgspec
from litestar import Controller, Request, get, post
from litestar.exceptions import HTTPException
from litestar.params import Parameter
from litestar.response import ServerSentEvent

from backend.api.auth_guard import auth_or_demo_guard
from backend.shared.helpers.connections import Connections
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


# ── Instrument-token helper (shared by sparkline + watchlist Phase 2 hook) ───

async def _resolve_token_for_sym(tradingsymbol: str, exchange: str) -> int | None:
    """
    Resolve a single (tradingsymbol, exchange) pair to its Kite
    instrument_token. Used by the watchlist add-item hook to subscribe the
    new symbol to the TickerManager immediately after DB insert.

    Consults the day-cached token map (_get_today_token_map) first so the
    vast majority of calls return immediately without a broker.instruments()
    HTTP round-trip. Only falls back to a live broker call when the day-cache
    is cold (once per IST day, typically pre-warmed by batch_sparkline).

    Walks exchange → NFO → BFO → NSE → BSE in order. Returns None on any
    failure so callers can treat the subscription as best-effort.
    """
    try:
        from backend.shared.brokers.registry import get_sparkline_broker
        broker = get_sparkline_broker()
    except Exception:
        return None

    sym  = tradingsymbol.upper().strip()
    exch = exchange.upper().strip()
    order = [exch] + [e for e in ("MCX", "CDS", "NFO", "BFO", "NSE", "BSE") if e != exch]

    # Fast path: day-cached token map (built once per IST day by batch_sparkline).
    try:
        token_map = await asyncio.to_thread(_get_today_token_map, broker)
        for ex in order:
            tok = token_map.get((sym, ex))
            if tok is not None:
                return int(tok)
    except Exception:
        pass  # cache miss or broker unavailable — fall through to live walk

    # Slow path: live broker.instruments() walk (cache was cold or empty).
    for ex in order:
        try:
            insts = await asyncio.to_thread(broker.instruments, ex) or []
        except Exception:
            continue
        for inst in insts:
            if str(inst.get("tradingsymbol") or "").upper() == sym:
                return int(inst["instrument_token"])
    return None


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
        # Build (exch, sym) pairs alongside items so we can subscribe the
        # universe to the live ticker below — without this, /pulse's
        # winners/losers sparklines only get an SSE feed AFTER the next
        # loadSparklines call (up to 30s after the mover set rotates).
        # The sparkline tail in the renderer reads _liveLtpSnap[sym]; if
        # SSE never subscribed the symbol, the tail stays pinned at the
        # poll-time LTP and the curve looks frozen.
        seen_pairs: list[tuple[str, str]] = []
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
            seen_pairs.append((exch.upper(), sym.upper()))

        # Subscribe the queried universe to the live ticker so SSE starts
        # streaming LTP for these symbols immediately. subscribe_with_sym
        # is idempotent + cheap; safe to call on every batch request.
        # Mover symbols rotating into the winners/losers tabs need this so
        # their sparkline tail tracks live ticks without waiting for the
        # next sparkline endpoint round-trip.
        if seen_pairs:
            try:
                from backend.shared.brokers.registry import get_sparkline_broker
                _bk = get_sparkline_broker()
                _full_map = await asyncio.to_thread(_get_today_token_map, _bk)
                _sub_pairs: list[tuple[int, str]] = []
                for exch, sym in seen_pairs:
                    pref = [exch] + [e for e in _SPARKLINE_EXCHANGES if e != exch]
                    for _ex in pref:
                        tok = _full_map.get((sym, _ex))
                        if tok is not None:
                            _sub_pairs.append((tok, sym))
                            break
                if _sub_pairs:
                    from backend.shared.helpers.kite_ticker import get_ticker
                    get_ticker().subscribe_with_sym(_sub_pairs)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"batch_quote: ticker subscribe skipped: {exc}")

        return BatchQuoteResponse(
            refreshed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            items=items,
        )


# ── Sparkline warm-state (for /api/admin/health) ─────────────────────────────
# These two module-level vars are read by health.py; they are updated by
# warm_sparkline_cache after each warm cycle. The legacy in-process disk
# cache has been retired — historical bars are served by ohlcv_store
# and today's intraday bars by intraday_store.

_spark_warm_symbols: int = 0
_spark_warm_at: Optional[str] = None   # ISO-8601 UTC string

# ── Instrument token-map cache ────────────────────────────────────────────────
# broker.instruments(exchange) returns ~500 kB per exchange; fetching all
# exchanges on every sparkline request wasted 4-5 blocking HTTP round-trips
# even when the entire past-cache was warm and no historical fetch was needed.
# Cache the union map for the day; it only changes at midnight IST.
# Key: IST date string.  Value: {(tradingsymbol, exchange) → instrument_token}.
_TOKEN_MAP_CACHE: dict[str, dict[tuple[str, str], int]] = {}
_TOKEN_MAP_LOCK   = threading.Lock()

_SPARKLINE_EXCHANGES = ("NSE", "NFO", "BSE", "BFO", "MCX", "CDS")


def _get_today_token_map(broker) -> dict[tuple[str, str], int]:  # type: ignore[no-untyped-def]
    """Return the day-cached {(tradingsymbol, exchange) → token} map.

    Read priority:
      1. instruments_store._MEM_CACHE (Tier 1 of the persistent store) — O(1)
         sync read; populated by get_or_fetch_all_today() on the async path.
      2. Module-level _TOKEN_MAP_CACHE fallback — used when the persistent
         store has not been warmed yet (e.g. first cold call from ohlcv_store
         before background warm has run). Falls through to a live broker fetch
         and fires a background populate of the persistent store as a side-effect.

    This function is always called via asyncio.to_thread so it MUST remain
    synchronous. Awaiting inside here would deadlock the thread pool.
    """
    today = _ist_today()

    # ── Tier 1: check instruments_store memory cache (sync read) ──────────────
    # The store's _MEM_CACHE is keyed (date_str, exchange); we union all
    # exchanges into a single map matching the legacy return type.
    try:
        from backend.api.persistence.instruments_store import (
            _MEM_CACHE as _instr_mem,
            _SPARKLINE_EXCHANGES as _INSTR_EXCHS,
            _purge_stale,
        )
        _purge_stale(today)
        # Check if ALL exchanges have been loaded into the store's Tier 1.
        # A partial warm (some exchanges missing) falls through to the legacy
        # path rather than returning an incomplete map.
        warm_keys = {(today, exch) for exch in _INSTR_EXCHS}
        if warm_keys.issubset(_instr_mem.keys()):
            union: dict[tuple[str, str], int] = {}
            for exch in _INSTR_EXCHS:
                cached_exch = _instr_mem.get((today, exch))
                if cached_exch:
                    union.update(cached_exch)
            if union:
                # Mirror into _TOKEN_MAP_CACHE so any future sync-path callers
                # get the fast path without re-scanning _instr_mem.
                with _TOKEN_MAP_LOCK:
                    _TOKEN_MAP_CACHE[today] = union
                return union
    except Exception:
        pass  # persistent store not available — fall through

    # ── Legacy Tier 2: module-level _TOKEN_MAP_CACHE ───────────────────────────
    with _TOKEN_MAP_LOCK:
        cached = _TOKEN_MAP_CACHE.get(today)
        if cached is not None:
            return cached

    # ── Legacy Tier 3: live broker fetch ──────────────────────────────────────
    # Build outside the lock — broker.instruments calls are slow (~500 kB each).
    new_map: dict[tuple[str, str], int] = {}
    for exch in _SPARKLINE_EXCHANGES:
        try:
            for row in broker.instruments(exch) or []:
                ts  = row.get("tradingsymbol")
                tok = row.get("instrument_token")
                if ts and tok:
                    new_map[(str(ts).upper(), exch)] = int(tok)
        except Exception:
            continue
    with _TOKEN_MAP_LOCK:
        # Drop stale dates (only today's entry is valid).
        for k in list(_TOKEN_MAP_CACHE):
            if k != today:
                _TOKEN_MAP_CACHE.pop(k, None)
        _TOKEN_MAP_CACHE[today] = new_map

    # Fire-and-forget: populate the persistent store from the result we just
    # built so subsequent calls (and post-redeploy restarts) hit Tier 1 or
    # Tier 2 instead of calling the broker again.
    _trigger_instruments_store_populate()

    return new_map


def _trigger_instruments_store_populate() -> None:
    """Fire a background coroutine to populate instruments_store from broker.

    Safe to call from a thread (asyncio.to_thread context). Uses
    run_coroutine_threadsafe to schedule on the running event loop.
    If no loop is running, silently skips (test / import-only contexts).
    """
    try:
        import asyncio as _asyncio
        from backend.api.persistence.instruments_store import get_or_fetch_all_today as _g
        from backend.api.persistence.write_queue import get_main_loop as _get_loop
        loop = _get_loop()
        if loop is not None and loop.is_running():
            _asyncio.run_coroutine_threadsafe(_g(), loop)
    except Exception:
        pass  # never let a background-populate failure surface to the caller


def _ist_today() -> str:
    """Return today's date in IST as YYYY-MM-DD."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d")
    except Exception:
        # Fallback: UTC+5:30
        return (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d")


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
        requested symbol, with today's 30-minute intraday closes and the
        current LTP appended as the final points. Used by the /pulse
        sparkline column.

        Three-tier read path (via persistence stores):
        - Past (days-1) daily closes: ohlcv_store (Tier 1 mem → Tier 2 DB →
          Tier 3 broker). historical_data budget shared with the stores.
        - Today's 30-minute bars: intraday_store (5-min TTL, same tier stack).
        - Live LTP: ticker._tick_map (zero quota) → broker.ltp() fallback.

        Missing / un-resolvable symbols are silently omitted (no 404).
        Broker unreachable → empty data dict instead of 502.
        """
        syms = data.symbols or []
        if not syms:
            return SparklineResponse(data={}, refreshed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        if len(syms) > 100:
            raise HTTPException(status_code=400, detail="symbols cap is 100")

        days = max(1, min(int(data.days), 90))
        today = _ist_today()
        today_date = date.fromisoformat(today)
        # yesterday = most recent completed trading day for past-closes window
        yesterday = today_date - timedelta(days=1)
        from_daily = today_date - timedelta(days=days + 5)  # +5 buffer for weekends/holidays

        # Normalise symbol list. Bare MCX commodity names ("CRUDEOIL",
        # "GOLDM") + CDS currency names ("USDINR") aren't real Kite
        # instruments — the tradable contract is the front-month future
        # (CRUDEOIL26JUNFUT). The watchlist quotes endpoint already
        # resolves these via `_resolve_mcx_commodity` /
        # `_resolve_cds_currency`; the sparkline endpoint must do the
        # same or the token-lookup misses, the symbol is silently
        # dropped, and the operator sees an empty 5d sparkline column
        # on pinned MCX/CDS rows even when the rest of the row data
        # arrives via the watchlist REST poll.
        from backend.api.routes.watchlist import (
            _resolve_mcx_commodity,
            _resolve_cds_currency,
        )
        norm_syms: list[SparklineSymbol] = []
        for sym_obj in syms:
            sym  = sym_obj.tradingsymbol.upper().strip()
            exch = (sym_obj.exchange or "NSE").upper().strip()
            if exch == "MCX" and sym.isalpha() and len(sym) <= 12:
                resolved = await _resolve_mcx_commodity(sym)
                if resolved:
                    sym = resolved.upper().strip()
            elif exch == "CDS" and sym.isalpha() and len(sym) <= 12:
                resolved = await _resolve_cds_currency(sym)
                if resolved:
                    sym = resolved.upper().strip()
            norm_syms.append(SparklineSymbol(tradingsymbol=sym, exchange=exch))

        # ── Step 1: Past daily closes via ohlcv_store ────────────────────────
        # Request [from_daily, yesterday] from the store. The store handles
        # Tier 1 → Tier 2 → Tier 3 (broker) transparently and write-backs
        # to the queue. We then trim to the (days-1) most recent closes.
        from backend.api.persistence import ohlcv_store as _ohlcv_store
        from backend.api.persistence import intraday_store as _intraday_store

        async def _fetch_daily_closes(sym_obj: SparklineSymbol) -> tuple[str, list[float]]:
            try:
                bars = await _ohlcv_store.get_or_fetch_daily(
                    sym_obj.tradingsymbol, sym_obj.exchange,
                    from_d=from_daily, to_d=yesterday,
                )
                closes = [b["close"] for b in bars]
                if len(closes) > (days - 1):
                    closes = closes[-(days - 1):]
                return sym_obj.tradingsymbol, closes
            except Exception as exc:
                logger.debug(f"sparkline: ohlcv_store miss for {sym_obj.tradingsymbol}: {exc}")
                return sym_obj.tradingsymbol, []

        async def _fetch_today_bars(sym_obj: SparklineSymbol) -> tuple[str, list[float]]:
            try:
                bars = await _intraday_store.get_or_fetch_intraday(
                    sym_obj.tradingsymbol, sym_obj.exchange,
                    on_date=today_date, interval="30minute",
                )
                closes = [b["close"] for b in bars]
                return sym_obj.tradingsymbol, closes
            except Exception as exc:
                logger.debug(f"sparkline: intraday_store miss for {sym_obj.tradingsymbol}: {exc}")
                return sym_obj.tradingsymbol, []

        # Fan-out both daily + intraday fetches in parallel across all symbols.
        daily_tasks   = [_fetch_daily_closes(s) for s in norm_syms]
        intraday_tasks = [_fetch_today_bars(s) for s in norm_syms]
        daily_results, intraday_results = await asyncio.gather(
            asyncio.gather(*daily_tasks),
            asyncio.gather(*intraday_tasks),
        )

        past_result:  dict[str, list[float]] = dict(daily_results)
        today_result: dict[str, list[float]] = dict(intraday_results)

        # ── Step 2: Build token_map for LTP lookup + ticker subscription ─────
        # We need the instrument_token for every normalised symbol to:
        #   a) read live LTP from the ticker's _tick_map (zero quota)
        #   b) push tokens to the ticker for future SSE ticks
        #   c) fall back to broker.ltp() for symbols not yet in the tick stream
        token_map: dict[str, int] = {}
        try:
            from backend.shared.brokers.registry import get_sparkline_broker as _sb
            _bk = _sb()
            _full_map = await asyncio.to_thread(_get_today_token_map, _bk)
            for s in norm_syms:
                if s.tradingsymbol in token_map:
                    continue
                pref = [s.exchange] + [e for e in ("MCX", "CDS", "NFO", "BFO", "NSE", "BSE") if e != s.exchange]
                for _ex in pref:
                    tok = _full_map.get((s.tradingsymbol, _ex))
                    if tok is not None:
                        token_map[s.tradingsymbol] = tok
                        break
        except Exception as _exc:
            logger.warning(f"sparkline: token lookup failed: {_exc}")

        # ── Step 3: LTP for ALL symbols — tick map first, broker.ltp() fallback
        from backend.shared.helpers.kite_ticker import get_ticker
        ticker = get_ticker()

        # CRITICAL: use subscribe_with_sym so the ticker's _token_to_sym map
        # is populated — without it SSE tick payloads carry sym="" and the
        # frontend quoteStream filter silently drops every tick.
        if token_map:
            ticker.subscribe_with_sym(
                [(tok, sym) for sym, tok in token_map.items()]
            )

        key_to_token: dict[str, int] = {
            f"{s.exchange}:{s.tradingsymbol}": token_map[s.tradingsymbol]
            for s in norm_syms
            if s.tradingsymbol in token_map
        }
        quote_keys = [f"{s.exchange}:{s.tradingsymbol}" for s in norm_syms]
        ltp_map: dict[str, float] = {}

        # Pass 1 — tick map (zero Kite quota).
        ticker_hits: list[str] = []
        miss_keys: list[str] = []
        for qk in quote_keys:
            tok = key_to_token.get(qk)
            if tok is not None:
                ltp_val = ticker.get_ltp(tok)
                if ltp_val is not None:
                    ltp_map[qk] = ltp_val
                    ticker_hits.append(qk)
                else:
                    miss_keys.append(qk)
            else:
                miss_keys.append(qk)

        if ticker_hits:
            logger.debug(
                f"sparkline: {len(ticker_hits)} LTP(s) from tick_map, "
                f"{len(miss_keys)} fallback to broker.ltp()"
            )

        # Pass 2 — broker.ltp() for misses only.
        if miss_keys:
            try:
                from backend.shared.brokers.registry import get_sparkline_broker as _get_sp_broker
                ltp_broker = _get_sp_broker()
                raw_ltp = await asyncio.to_thread(ltp_broker.ltp, miss_keys) or {}
                for key, val in raw_ltp.items():
                    if isinstance(val, dict):
                        lp = val.get("last_price")
                    else:
                        lp = val
                    try:
                        ltp_map[key] = float(lp) if lp is not None else 0.0
                    except (TypeError, ValueError):
                        pass
            except Exception as exc:
                logger.warning(f"sparkline: ltp fallback batch failed: {exc}")

        # ── Step 4: Compose result series ────────────────────────────────────
        result: dict[str, list[float]] = {}
        for sym_obj in norm_syms:
            sym  = sym_obj.tradingsymbol
            past = past_result.get(sym, [])
            today_bars = today_result.get(sym, [])
            ltp_key = f"{sym_obj.exchange}:{sym}"
            ltp_val = ltp_map.get(ltp_key)
            tail_ltp = ltp_val if (ltp_val and ltp_val > 0) else None
            # Series order: past daily closes (oldest first), then today's
            # 30-min intraday closes, then current LTP. Off-hours today_bars
            # is empty → collapses to past + [ltp] (same as before).
            series = past + today_bars
            if tail_ltp is not None:
                series = series + [tail_ltp]
            # Frontend sparkline renderer needs ≥ 2 points (it draws a
            # polyline between consecutive closes; 1 point can't make a
            # line). When the operator's universe rotates a new mover in
            # and the broker rate-limits the historical_data call, we
            # end up with just [ltp]. Pad to [ltp, ltp] so the cell
            # renders a flat horizontal line — communicates "we have the
            # current price but no history" instead of an em-dash that
            # looks like "data missing entirely". Operator: "when the
            # data for sparkline comes from db and cache, why it is not
            # showing all sparklines in pulse?" — the answer was the
            # broker rate-limit + frontend's <2-point em-dash. Padding
            # here fixes the cell render without waiting for the warm
            # task to repopulate the cache.
            if len(series) == 1:
                series = series + series
            if series:
                result[sym] = series

        return SparklineResponse(
            data=result,
            refreshed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )

    @get("/stream")
    async def quote_stream(self, request: Request) -> ServerSentEvent:
        """
        GET /api/quotes/stream

        Server-Sent Events stream of LTP ticks from the KiteTicker
        WebSocket. Clients open this via the browser's EventSource API
        and receive per-tick deltas without polling.

        Protocol:
          event: snapshot   — sent once on connect; data is a JSON object
                              mapping token (string) → {ltp, sym}. Lets the
                              client populate its LTP map immediately.
          event: tick       — one per instrument per Kite tick frame; data
                              is {"tok":<int>, "sym":<str>, "ltp":<float>,
                              "ts":<unix-seconds>}.
          event: heartbeat  — sent every 30 s when no tick arrives, so
                              load-balancers / proxies don't kill the idle
                              connection. data is "1".

        Backpressure:
          Each SSE client owns a private asyncio.Queue(maxsize=1000).
          If the client reads slower than the tick rate, put_nowait silently
          drops ticks (QueueFull is swallowed in BroadcastBus._put_nowait).
          The client reconnects via EventSource retry — it will receive a
          fresh snapshot on reconnect and resume from current state.

        Security:
          Protected by auth_or_demo_guard at the controller level.
          Demo sessions receive the same tick stream as authenticated
          users — ticks carry no personally-identifiable information,
          only public market prices.
        """
        from backend.shared.helpers.kite_ticker import get_ticker

        ticker = get_ticker()

        async def _event_gen() -> AsyncGenerator:
            queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
            ticker.bus.register(queue)
            try:
                # Initial snapshot so the client has a starting LTP map
                # without waiting for the first tick.
                snap = ticker.snapshot()
                yield {"event": "snapshot", "data": json.dumps(snap)}

                while True:
                    # 30-second heartbeat timeout — keeps the connection
                    # alive through proxies / load-balancers that drop
                    # idle connections. Kite ticks every ~1 s during market
                    # hours so in practice heartbeats only fire off-hours.
                    try:
                        payload = await asyncio.wait_for(queue.get(), timeout=30.0)
                        yield {"event": "tick", "data": json.dumps(payload)}
                    except asyncio.TimeoutError:
                        yield {"event": "heartbeat", "data": "1"}
            finally:
                ticker.bus.unregister(queue)

        return ServerSentEvent(_event_gen())


# ── Background warm helper ────────────────────────────────────────────────────


def _ticker_seed_early(token_map: dict[str, int]) -> None:
    """Seed KiteTicker subscriptions BEFORE the historical-data warm
    fetches kick off. Hoists what used to be a post-warm step to a
    pre-warm step so the WebSocket reconnect + token subscribe run in
    parallel with the rate-limited historical fetches — instead of
    serially after them.

    Operator-visible effect: after a redeploy, the SSE tick stream
    starts pushing live LTPs roughly 5-15 s sooner because the WS
    handshake didn't have to wait for ~100 historical_data round-trips
    to complete first.

    subscribe_with_sym() is idempotent + non-blocking. Safe to call
    from anywhere in the warm flow; called once here at the top of
    `warm_sparkline_cache`. Errors swallowed silently — historical
    fetches still proceed, just without the early WS push.
    """
    if not token_map:
        return
    try:
        from backend.shared.helpers.kite_ticker import get_ticker
        ticker = get_ticker()
        # Deferred-start safety: the on_startup _start_kite_ticker()
        # hook may have run before Connections() finished restoring
        # the cached access_token. Retry the start here against the
        # same eligible Kite account preference order.
        if not ticker.status().get("started"):
            try:
                from backend.shared.brokers.registry import get_sparkline_broker
                spark_bk = get_sparkline_broker()
                for b in getattr(spark_bk, "_brokers", []):
                    kc = getattr(b, "_conn", None) or getattr(b, "kite", None)
                    api_key = getattr(kc, "api_key", None)
                    access_token = getattr(kc, "_access_token", None) or getattr(kc, "access_token", None)
                    if api_key and access_token:
                        if ticker.ensure_started(api_key, access_token):
                            logger.info(
                                f"sparkline warm: KiteTicker started "
                                f"(deferred retry, account={getattr(b, 'account', '?')})"
                            )
                        break
            except Exception as exc:
                logger.warning(f"sparkline warm: deferred ticker start failed: {exc}")
        # subscribe_with_sym pushes both (token, sym) so SSE tick
        # payloads carry the right sym key. Without it, the frontend's
        # quoteStream filter silently drops every tick.
        ticker.subscribe_with_sym(
            [(tok, sym) for sym, tok in token_map.items()]
        )
        logger.info(
            f"sparkline warm: pushed {len(token_map)} token(s) to TickerManager (pre-fetch)"
        )
    except Exception as exc:
        logger.warning(f"sparkline warm: ticker subscribe failed: {exc}")

async def warm_sparkline_cache(symbols: list[tuple[str, str]], days: int = 5) -> int:
    """
    Pre-warm ohlcv_store (past daily closes) + intraday_store (today's 30-min
    bars) for the given (tradingsymbol, exchange) pairs. Called by
    _task_sparkline_warm in background.py at market open and at app startup.

    Returns the count of symbols where at least daily data was loaded.

    Each store's own fetch-lock prevents redundant broker calls — if the warm
    task fires concurrently with a lazy batch_sparkline hit the stores dedup
    and return from whichever fetch landed first. Errors per-symbol are
    swallowed silently (the stores log warnings) so one bad symbol never aborts
    the whole warm run. The stores' write queues handle persistence to DB + disk.
    """
    global _spark_warm_symbols, _spark_warm_at

    if not symbols:
        return 0

    today = _ist_today()
    today_date = date.fromisoformat(today)
    yesterday  = today_date - timedelta(days=1)
    from_daily = today_date - timedelta(days=days + 5)

    from backend.api.persistence import ohlcv_store as _ohlcv_store
    from backend.api.persistence import intraday_store as _intraday_store

    norm: list[tuple[str, str]] = [
        (sym.upper().strip(), exch.upper().strip()) for sym, exch in symbols
    ]

    # Build the token map so we can seed the KiteTicker BEFORE the fetches.
    token_map: dict[str, int] = {}
    try:
        from backend.shared.brokers.registry import get_sparkline_broker
        broker = get_sparkline_broker()
        _full_map = await asyncio.to_thread(_get_today_token_map, broker)
        for sym_n, exch_n in norm:
            if sym_n in token_map:
                continue
            pref = [exch_n] + [e for e in ("MCX", "CDS", "NFO", "BFO", "NSE", "BSE") if e != exch_n]
            for ex in pref:
                tok = _full_map.get((sym_n, ex))
                if tok is not None:
                    token_map[sym_n] = tok
                    break
    except Exception as exc:
        logger.warning(f"sparkline warm: instrument lookup failed: {exc}")

    # Seed KiteTicker subscriptions BEFORE the stores' broker fetches start.
    if token_map:
        _ticker_seed_early(token_map)

    # Fan-out all daily + intraday store calls concurrently. Each store call is
    # rate-limited by its own fetch-lock — the Semaphore(2) + 0.35 s pace that
    # existed in the old warm loop is now inside the stores' Tier 3 paths.
    async def _warm_daily(sym: str, exch: str) -> bool:
        try:
            bars = await _ohlcv_store.get_or_fetch_daily(
                sym, exch, from_d=from_daily, to_d=yesterday,
            )
            return bool(bars)
        except Exception:
            return False

    async def _warm_intraday(sym: str, exch: str) -> bool:
        try:
            bars = await _intraday_store.get_or_fetch_intraday(
                sym, exch, on_date=today_date, interval="30minute",
            )
            return bool(bars)
        except Exception:
            return False

    daily_tasks   = [_warm_daily(sym, exch)   for sym, exch in norm]
    intraday_tasks = [_warm_intraday(sym, exch) for sym, exch in norm]

    daily_results, intraday_results = await asyncio.gather(
        asyncio.gather(*daily_tasks),
        asyncio.gather(*intraday_tasks),
    )

    cached_count   = sum(1 for ok in daily_results   if ok)
    intraday_count = sum(1 for ok in intraday_results if ok)

    if intraday_count:
        logger.info(
            f"sparkline warm: intraday warmed {intraday_count}/{len(norm)} symbols"
        )

    _spark_warm_symbols = cached_count
    _spark_warm_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    logger.info(f"sparkline warm: daily warmed {cached_count}/{len(norm)} symbols for {today}")
    return cached_count
