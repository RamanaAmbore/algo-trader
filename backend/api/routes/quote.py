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
from backend.api.helpers.snapshot_gate import _any_segment_open
from backend.brokers.connections import Connections
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)

# Rate-limited "sparkline: db_only active" log — emit at most once per 60 s so
# the operator can grep the api_log_file without noise during a closed-hours
# night. Uses a module-level timestamp (float) guarded by the GIL; no Lock
# needed — worst case two nearly-simultaneous requests both log, which is fine.
import time as _time_mod
_spark_db_only_last_log: float = 0.0

# Per-symbol rate-limited "empty series" diagnostic log.  Maps (sym, exch) →
# last-logged epoch.  Emitted at most once per 3600 s per symbol so the
# operator can grep "sparkline: no data" to identify the failing symbols
# without log flooding on every 60 s poll.
_spark_empty_last_log: dict[tuple[str, str], float] = {}


# ── Closed-hours cold-start warm state ────────────────────────────────────────
# _closed_hours_warm_signatures is the SET of key-set signatures already warmed
# in the current IST day.  A signature is the concatenation of resolved broker
# keys (sorted, joined by ',').  Signatures reset at midnight IST via
# _closed_hours_warm_day.  Guarded by _closed_hours_warm_lock.
_closed_hours_warm_signatures: set[str] = set()
_closed_hours_warm_day: str = ""
_closed_hours_warm_lock = threading.Lock()


def _record_good_ltp_live(sym: str, ltp: float) -> None:
    """Thin wrapper so batch_quote's live path can persist LKG LTP without
    importing broker_apis at module top (circular-import risk)."""
    try:
        from backend.brokers.broker_apis import record_good_ltp
        record_good_ltp(sym, ltp)
    except Exception:
        pass


def _record_good_quote_live(sym: str, payload: dict) -> None:
    """Thin wrapper so batch_quote's live path can persist LKG non-LTP fields
    without importing broker_apis at module top (circular-import risk)."""
    try:
        from backend.brokers.broker_apis import record_good_quote
        record_good_quote(sym, payload)
    except Exception:
        pass


async def _maybe_warm_closed_hours_quotes(keys: list[str], key_map) -> None:
    """One-shot broker.quote() warm for closed-hours cold-start scenarios.

    When the process restarts during closed hours (typical after a redeploy
    that lands post-15:30 IST), the in-memory LKG quote cache is empty and
    every /api/quote/batch response would drop open/close/volume/oi to null.
    This helper fires ONE broker.quote() per (day, key-set signature) so the
    LKG cache warms up for every subsequent closed-hours poll.

    The signature is per key-set so distinct pages (pinned universe vs.
    positions universe) each get their own warm without cross-blocking.
    Guarded by _closed_hours_warm_lock + a per-IST-day reset so we don't
    burn broker quota on every 30 s /pulse poll.

    Silently no-ops on any error (broker unreachable, resolver failure) —
    the caller falls through to empty-fields rows, same as the pre-warm
    baseline behaviour.
    """
    global _closed_hours_warm_day
    if not key_map or not getattr(key_map, "broker_keys", None):
        return
    today = _ist_today()
    sig = ",".join(sorted(key_map.broker_keys))
    with _closed_hours_warm_lock:
        if _closed_hours_warm_day != today:
            _closed_hours_warm_signatures.clear()
            _closed_hours_warm_day = today
        if sig in _closed_hours_warm_signatures:
            return
        # NOTE: do NOT add sig here — only add after a successful warm so a
        # broker failure or empty response doesn't poison the signature for
        # the rest of the IST day (preventing all future retry attempts).

    try:
        from backend.brokers.registry import get_market_data_broker
        from backend.brokers.broker_apis import record_good_ltp, record_good_quote
        broker = get_market_data_broker()
        quote_data = await asyncio.to_thread(broker.quote, key_map.broker_keys) or {}
    except Exception as exc:
        logger.debug(f"batch_quote: closed-hours warm skipped: {exc}")
        return

    _persisted = 0
    for bkey, q in quote_data.items():
        if not q:
            continue
        try:
            _, sym_only = bkey.split(":", 1) if ":" in bkey else ("", bkey)
            ohlc = q.get("ohlc") or {}
            _ltp = float(q.get("last_price") or 0.0)
            _close = float(ohlc.get("close") or 0.0) or None
            _open  = float(ohlc.get("open")  or 0.0) or None
            _vol   = int(q.get("volume") or 0)
            _oi    = int(q.get("oi") or 0)
            _change = (_ltp - _close) if (_close and _ltp) else 0.0
            _chg_pct = (_change / _close * 100.0) if _close else 0.0
            depth = q.get("depth") or {}
            buys  = depth.get("buy") or []
            sells = depth.get("sell") or []
            _bid = float(buys[0]["price"])  if buys  and (buys[0].get("price") or 0)  else None
            _ask = float(sells[0]["price"]) if sells and (sells[0].get("price") or 0) else None
            if _ltp > 0:
                record_good_ltp(sym_only, _ltp)
            record_good_quote(sym_only, {
                "open":       _open,
                "close":      _close,
                "volume":     _vol,
                "oi":         _oi,
                "change":     _change,
                "change_pct": _chg_pct,
                "bid":        _bid,
                "ask":        _ask,
            })
            _persisted += 1
        except Exception:
            continue
    if _persisted:
        # Mark warm ONLY after at least one symbol persisted successfully.
        # Marking before the broker call (previous behaviour) poisoned the
        # signature on broker failure — no retry until IST day rollover,
        # leaving vol/oi blank all day for pinned MCX symbols.
        with _closed_hours_warm_lock:
            _closed_hours_warm_signatures.add(sig)
        logger.info(
            f"batch_quote: closed-hours warm — persisted LKG for {_persisted}/{len(quote_data)} symbols"
        )


# ── Closed-hours helper ───────────────────────────────────────────────────────

def _exchanges_from_keys(keys: list[str]) -> set[str]:
    """Extract the set of exchange codes from 'EXCHANGE:SYMBOL' keys."""
    out: set[str] = set()
    for k in keys:
        if ":" in k:
            out.add(k.split(":", 1)[0].upper())
    return out


def _is_exchange_segment_closed(exchange: str) -> bool:
    """Return True if the given exchange's segment is currently closed.

    Reads the same `market_segments` config that `is_any_segment_open`
    uses so a single YAML edit is enough to change the covered segments.
    Falls back to `is_any_segment_open` for exchanges not listed — safe
    default that keeps the live path active.
    """
    try:
        from backend.shared.helpers.date_time_utils import (
            is_market_open, timestamp_indian,
        )
        from backend.shared.helpers.utils import config as _cfg
        from backend.brokers.broker_apis import fetch_holidays
        from datetime import time as dtime

        now = timestamp_indian()
        exch_upper = exchange.upper()

        segments = _cfg.get("market_segments", {}) or {}
        for _seg_name, seg_cfg in segments.items():
            seg_exch = (seg_cfg.get("holiday_exchange") or "NSE").upper()
            # Match exchange against both the holiday_exchange key and any
            # "exchanges" list if present.  A requested NSE or NFO key maps to
            # the equity segment whose holiday_exchange="NSE".
            seg_exchanges = [e.upper() for e in (seg_cfg.get("exchanges") or [seg_exch])]
            if exch_upper not in seg_exchanges and exch_upper != seg_exch:
                continue
            h_s, m_s = map(int, seg_cfg.get("hours_start", "09:15").split(":"))
            h_e, m_e = map(int, seg_cfg.get("hours_end",   "15:30").split(":"))
            try:
                holidays = fetch_holidays(seg_exch)
            except Exception:
                holidays = set()
            if is_market_open(now, holidays, dtime(h_s, m_s), dtime(h_e, m_e), exchange=seg_exch):
                return False  # this segment IS open
        # No matching segment found open → closed
        return True
    except Exception:
        return False  # fail-open: assume open


def _all_exchanges_closed(exchanges: set[str]) -> bool:
    """Return True when EVERY exchange in the set is currently closed."""
    if not exchanges:
        return False
    return all(_is_exchange_segment_closed(e) for e in exchanges)


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
        from backend.brokers.registry import get_sparkline_broker
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


def _fetch_ltp(
    broker_exchange: str,
    broker_tradingsymbol: str,
    display_exchange: str | None = None,
    display_tradingsymbol: str | None = None,
) -> QuoteResponse:
    # Shared market-data fetch — route through get_market_data_broker() so
    # the operator's `connections.price_account` setting decides which
    # account's API handle services chart-data calls. Broker-agnostic
    # path; any vendor's adapter will work the same.
    #
    # Virtual MCX/CDS root resolution is handled by the ASYNC caller
    # (get_quote) which resolves before calling here.  _fetch_ltp is
    # purely sync — no event-loop interaction allowed.  Scheduling async
    # work from here would deadlock (the calling thread IS the event loop).
    #
    # broker_exchange / broker_tradingsymbol   — resolved contract key sent to broker.
    # display_exchange / display_tradingsymbol — original operator-facing symbol
    #   returned in QuoteResponse so the frontend row-lookup still matches.
    #   Defaults to broker_* when not supplied (non-virtual symbols).
    from backend.brokers.registry import get_market_data_broker

    broker_key = f"{broker_exchange.upper()}:{broker_tradingsymbol.upper()}"
    disp_exchange = (display_exchange or broker_exchange)
    disp_sym      = (display_tradingsymbol or broker_tradingsymbol)
    broker = get_market_data_broker()

    bid = ask = None
    depth_buy: list[DepthLevel] = []
    depth_sell: list[DepthLevel] = []
    volume = 0
    ltp = 0.0

    try:
        full = broker.quote([broker_key]).get(broker_key) or {}
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
        logger.warning(f"Quote depth failed for {broker_key}: {e}")
        try:
            data = broker.ltp([broker_key])
            row = data.get(broker_key) or {}
            ltp = float(row.get("last_price") or 0.0)
        except Exception as e2:
            logger.error(f"Quote LTP fallback failed for {broker_key}: {e2}")

    return QuoteResponse(
        tradingsymbol=disp_sym,    # always the operator-facing symbol
        exchange=disp_exchange,
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
    # ISO-8601 UTC timestamp of the snapshot that was returned.
    # Non-null only when serving a persisted off-hours snapshot so the
    # frontend can show a staleness hint instead of a live label.
    as_of: Optional[str] = None


class QuoteController(Controller):
    path = "/api/quote"
    guards = [auth_or_demo_guard]

    @get("/")
    async def get_quote(
        self,
        exchange: str = Parameter(required=True),
        tradingsymbol: str = Parameter(required=True),
    ) -> QuoteResponse:
        from backend.api.algo.symbol_resolver import resolve_market_data_keys
        try:
            input_key = f"{exchange.upper()}:{tradingsymbol.upper()}"
            key_map = await resolve_market_data_keys([input_key])
            broker_key = key_map.input_to_broker.get(input_key, input_key)
            b_exch, b_sym = broker_key.split(":", 1) if ":" in broker_key else (exchange, tradingsymbol)
            return await asyncio.to_thread(
                _fetch_ltp, b_exch, b_sym, exchange, tradingsymbol
            )
        except Exception as e:
            logger.error(f"Quote API error: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @post("/batch")
    async def batch_quote(self, data: BatchQuoteRequest) -> BatchQuoteResponse:
        """One batched broker.quote() across an arbitrary key list.
        Used by the unified market-pulse view on /watchlist to pull
        live LTP / day-change for positions + holdings + underlyings
        without N round-trips.

        Virtual MCX/CDS root symbols (e.g. ``MCX:CRUDEOIL``) are resolved
        to their front-month futures contract before the broker call.
        Response rows are keyed on the ORIGINAL operator-facing symbol so
        the frontend row-lookup (``cMap[exchange:tradingsymbol]``) always
        matches, regardless of which contract is live.

        When all requested exchanges are closed the route skips the broker
        call and returns the last-known-good LTP from the in-process
        _LAST_GOOD_LTP cache (populated during the last market session).
        The `as_of` field in the response is set so the frontend can
        show a staleness hint.
        """
        import asyncio
        from datetime import datetime, timezone
        from backend.brokers.registry import get_market_data_broker
        from backend.api.algo.symbol_resolver import resolve_market_data_keys

        keys = list({k.strip() for k in (data.keys or []) if k and ":" in k})
        # Soft cap — Kite quote() handles ~500 keys but the UI shouldn't
        # ask for more than this in one tab. Trim silently.
        keys = keys[:300]

        # ── Virtual root resolution ────────────────────────────────────────
        # Resolve MCX/CDS bare roots to front-month contracts so broker calls
        # succeed. key_map carries both directions so we can emit response rows
        # keyed on the original operator symbol.
        key_map = await resolve_market_data_keys(keys)

        # ── Closed-hours fast-path ─────────────────────────────────────────
        # When every exchange in the requested set is currently closed, skip
        # the broker.quote() call and serve last-known-good LTPs from the
        # in-process cache instead.  Ticker + broker calls zero; the cache
        # already holds EOD values from the prior session.
        req_exchanges = _exchanges_from_keys(keys)
        market_closed = _all_exchanges_closed(req_exchanges)

        as_of_str: Optional[str] = None
        quote_data: dict = {}

        if market_closed:
            # Read last-known-good LTP + non-LTP snapshot fields; mark rows as
            # stale. LKG lookup uses the RESOLVED symbol since that's what was
            # recorded during the live session.
            #
            # Cold-start warm: if the LKG quote cache is empty for the
            # requested universe (process restarted during closed hours, or
            # the previous session ended before the batch endpoint ran), do a
            # ONE-SHOT broker.quote() to populate open/close/volume/oi/change
            # for this response and every subsequent closed-hours request.
            # Guarded by a module-level "warmed today" flag so we don't burn
            # broker quota on every closed-hours poll.
            from backend.brokers.broker_apis import (
                get_last_good_ltp, get_last_good_quote, record_good_quote, record_good_ltp,
            )
            logger.debug(f"batch_quote: market closed — serving LKG for {len(keys)} keys")

            # Cold-start warm — one broker.quote() per IST day per key-set signature.
            await _maybe_warm_closed_hours_quotes(keys, key_map)

            items: list[BatchQuoteRow] = []
            as_of_str = datetime.now(timezone.utc).isoformat(timespec="seconds")
            for k in keys:
                try:
                    exch, sym = k.split(":", 1)
                except ValueError:
                    continue
                # Prefer LKG from the resolved contract symbol, fall back to
                # the raw input symbol (for non-virtual pass-throughs).
                broker_key = key_map.input_to_broker.get(k, k)
                _, resolved_sym = broker_key.split(":", 1) if ":" in broker_key else ("", sym)
                ltp = (
                    get_last_good_ltp(resolved_sym, max_age_s=86400.0) or
                    get_last_good_ltp(sym, max_age_s=86400.0) or
                    0.0
                )
                # LKG non-LTP snapshot — open/close/volume/oi/change/change_pct/bid/ask.
                # Prefer resolved contract symbol (matches live-path record key);
                # fall back to raw input symbol for non-virtual pass-throughs.
                snap = (
                    get_last_good_quote(resolved_sym, max_age_s=86400.0) or
                    get_last_good_quote(sym, max_age_s=86400.0) or
                    {}
                )
                items.append(BatchQuoteRow(
                    exchange=exch, tradingsymbol=sym,  # always original key
                    ltp=ltp,
                    bid=snap.get("bid"),
                    ask=snap.get("ask"),
                    open=snap.get("open"),
                    close=snap.get("close"),
                    change=float(snap.get("change") or 0.0),
                    change_pct=float(snap.get("change_pct") or 0.0),
                    volume=int(snap.get("volume") or 0),
                    oi=int(snap.get("oi") or 0),
                    stale=True,
                ))
            return BatchQuoteResponse(
                refreshed_at=as_of_str,
                items=items,
                as_of=as_of_str,
            )

        # ── Live path (market open) ────────────────────────────────────────
        if key_map.broker_keys:
            try:
                broker = get_market_data_broker()
                quote_data = await asyncio.to_thread(broker.quote, key_map.broker_keys) or {}
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"Batch quote failed: {exc}")
                quote_data = {}

        items = []
        # Build (exch, sym) pairs alongside items so we can subscribe the
        # universe to the live ticker below — without this, /pulse's
        # winners/losers sparklines only get an SSE feed AFTER the next
        # loadSparklines call (up to 30s after the mover set rotates).
        # The sparkline tail in the renderer reads _liveLtpSnap[sym]; if
        # SSE never subscribed the symbol, the tail stays pinned at the
        # poll-time LTP and the curve looks frozen.
        # seen_pairs tracks ORIGINAL (operator-facing) symbols so the
        # ticker subscribe uses the same sym key that SSE listeners
        # registered for.
        seen_pairs: list[tuple[str, str]] = []
        for k in keys:
            try:
                exch, sym = k.split(":", 1)
            except ValueError:
                continue
            # Look up broker data via the resolved key.
            broker_key = key_map.input_to_broker.get(k, k)
            q = quote_data.get(broker_key) or {}
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
            _vol = int(q.get("volume") or 0)
            _oi  = int(q.get("oi") or 0)
            items.append(BatchQuoteRow(
                exchange=exch, tradingsymbol=sym,  # always original operator-facing key
                ltp=ltp, bid=bid, ask=ask, open=open_, close=close,
                change=change, change_pct=chg_pct,
                volume=_vol,
                oi=_oi,
                stale=(not q),
            ))
            seen_pairs.append((exch.upper(), sym.upper()))

            # Record LKG for closed-hours fallback.  Key by the RESOLVED
            # broker symbol so virtual roots (MCX:CRUDEOIL → CRUDEOIL26JUNFUT)
            # persist under the same key both live-path and closed-hours
            # readers use.  record_good_ltp uses the sym-only key (no
            # exchange) to match the existing LTP cache convention.
            if q:
                _, resolved_sym_only = (
                    broker_key.split(":", 1) if ":" in broker_key else ("", sym)
                )
                if ltp and ltp > 0:
                    _record_good_ltp_live(resolved_sym_only, ltp)
                _record_good_quote_live(resolved_sym_only, {
                    "open":       open_,
                    "close":      close,
                    "volume":     _vol,
                    "oi":         _oi,
                    "change":     change,
                    "change_pct": chg_pct,
                    "bid":        bid,
                    "ask":        ask,
                })

        # Subscribe the queried universe to the live ticker so SSE starts
        # streaming LTP for these symbols immediately. subscribe_with_sym
        # is idempotent + cheap; safe to call on every batch request.
        # Mover symbols rotating into the winners/losers tabs need this so
        # their sparkline tail tracks live ticks without waiting for the
        # next sparkline endpoint round-trip.
        if seen_pairs:
            try:
                from backend.brokers.registry import get_sparkline_broker
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
                    from backend.brokers.kite_ticker import get_ticker
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
    # ISO-8601 UTC timestamp of the snapshot that was returned.
    # Non-null only when all requested exchanges were closed and no
    # live LTP was appended — so the frontend can label the data
    # "as of <time>" instead of showing a live indicator.
    as_of: Optional[str] = None


# ---------------------------------------------------------------------------
# Unified sparkline series composer (hardening: single-source truth + reason)
# ---------------------------------------------------------------------------

def compose_sparkline_series(
    past: list[float],
    today_bars: list[float],
    ltp_val: Optional[float],
    market_closed: bool,
) -> tuple[list[float], str]:
    """Compose the final sparkline series for a single symbol + return the
    reason label attributing the outcome.

    Pure function so unit tests can hand it fixture inputs (no I/O, no
    logger, no cache lookups). The batch endpoint is responsible for
    populating the three input slots via ohlcv_store / intraday_store /
    ticker respectively; this helper owns the fallback ladder that turns
    them into a renderable series.

    Ladder (top wins):
      past >= 1 AND (today_bars >= 1 OR ltp_val > 0):
        → past + today_bars + [ltp_val if open], reason='live'
      past >= 1 AND market closed:
        → past + today_bars, reason='snapshot'
      past == 0 AND ltp_val > 0:
        → [ltp_val, ltp_val] flat baseline, reason='ltp_only_flat_pad'
      len(series) == 1:
        → [x, x] pad, reason='single_point_pad'
      empty everywhere:
        → [], reason attributes to which tier failed:
          - warm_universe_empty (market closed, no cache, no LTP)
          - historical_fetch_fail (market open, no cache, no LTP)
          - spark_past_cache_miss (today ok, past empty)
          - spark_today_cache_miss (past ok, today empty, no LTP)

    Reason 'live' == "actually rendered from live-ish data".
    Reason 'snapshot' == "actually rendered from stale cache" — still
    displayable but the frontend can label it "as of <time>".
    """
    # Live LTP tail only when market is open (mirrors existing batch endpoint
    # semantics: appending stale LTP overnight would create a misleading
    # "current" data point on top of good history).
    tail_ltp = (ltp_val if (ltp_val and ltp_val > 0) else None) if not market_closed else None

    series = list(past) + list(today_bars)
    if tail_ltp is not None:
        series = series + [tail_ltp]

    if series and (past or today_bars):
        # Have real historical data — that's the primary render.
        reason = "snapshot" if market_closed else "live"
        # Pad single-point (broker rate-limit → past=[], only tail_ltp) to 2.
        if len(series) == 1:
            series = series + series
            reason = "single_point_pad"
        return series, reason

    # No historical data. See if LTP fallback can produce a flat baseline.
    if not series and ltp_val and ltp_val > 0:
        return [ltp_val, ltp_val], "ltp_only_flat_pad"

    # Rare guard: series has exactly 1 point (past=[], today=[ltp] combo).
    if len(series) == 1:
        return series + series, "single_point_pad"

    # Truly empty: attribute reason to which tier failed.
    if len(past) == 0 and len(today_bars) == 0:
        reason = "warm_universe_empty" if market_closed else "historical_fetch_fail"
    elif len(past) == 0:
        reason = "spark_past_cache_miss"
    elif len(today_bars) == 0:
        reason = "spark_today_cache_miss"
    else:
        reason = "unknown"
    return [], reason


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
        # orig_to_resolved maps bare/virtual watchlist name → resolved contract
        # e.g. "CRUDEOIL" → "CRUDEOIL26JUNFUT", "GOLDM_NEXT" → "GOLDM26AUGFUT".
        # Used in Step 4 to also store the result under the original key so the
        # frontend renderer can look up sparklines[row.tradingsymbol] and find
        # the series even when the row carries the virtual name from the watchlist.
        from backend.api.algo.symbol_resolver import resolve_symbol, _strip_next
        orig_to_resolved: dict[str, str] = {}
        for sym_obj in syms:
            sym  = sym_obj.tradingsymbol.upper().strip()
            exch = (sym_obj.exchange or "NSE").upper().strip()
            original_sym = sym
            # Strip _NEXT suffix for the alpha/length guard so back-month
            # virtual roots (GOLDM_NEXT, CRUDEOIL_NEXT) are also resolved.
            root, is_next = _strip_next(sym)
            is_bare_root = root.isalpha() and len(root) <= 12
            if exch == "MCX" and is_bare_root:
                if is_next:
                    resolved = await resolve_symbol(sym, "MCX")
                    if resolved and resolved != sym:
                        sym = resolved.upper().strip()
                        orig_to_resolved[original_sym] = sym
                else:
                    resolved = await _resolve_mcx_commodity(root)
                    if resolved:
                        sym = resolved.upper().strip()
                        orig_to_resolved[original_sym] = sym
            elif exch == "CDS" and is_bare_root:
                if is_next:
                    resolved = await resolve_symbol(sym, "CDS")
                    if resolved and resolved != sym:
                        sym = resolved.upper().strip()
                        orig_to_resolved[original_sym] = sym
                else:
                    resolved = await _resolve_cds_currency(root)
                    if resolved:
                        sym = resolved.upper().strip()
                        orig_to_resolved[original_sym] = sym
            norm_syms.append(SparklineSymbol(tradingsymbol=sym, exchange=exch))

        # ── Step 1: Past daily closes via ohlcv_store ────────────────────────
        # Request [from_daily, yesterday] from the store. The store handles
        # Tier 1 → Tier 2 → Tier 3 (broker) transparently and write-backs
        # to the queue. We then trim to the (days-1) most recent closes.
        from backend.api.persistence import ohlcv_store as _ohlcv_store
        from backend.api.persistence import intraday_store as _intraday_store

        # Determine db_only mode: when no market segment is open, skip all
        # broker calls in the store fetchers — the DB already holds today's
        # bars and yesterday's closes; no new data would arrive from a broker
        # call during closed hours, so the round-trip only burns rate-limit
        # budget.  The flag is computed once here and shared by both closures.
        _mkt_open: bool = await asyncio.to_thread(_any_segment_open)
        db_only: bool = not _mkt_open
        if db_only:
            global _spark_db_only_last_log
            _now_ts = _time_mod.time()
            if _now_ts - _spark_db_only_last_log >= 60.0:
                _spark_db_only_last_log = _now_ts
                logger.info(
                    "sparkline: db_only active — market closed, "
                    "serving Tier 1+2 only (no broker calls)"
                )

        async def _fetch_daily_closes(sym_obj: SparklineSymbol) -> tuple[str, list[float]]:
            try:
                bars = await _ohlcv_store.get_or_fetch_daily(
                    sym_obj.tradingsymbol, sym_obj.exchange,
                    from_d=from_daily, to_d=yesterday,
                    db_only=db_only,
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
                    db_only=db_only,
                )
                closes = [b["close"] for b in bars]
                return sym_obj.tradingsymbol, closes
            except Exception as exc:
                logger.debug(f"sparkline: intraday_store miss for {sym_obj.tradingsymbol}: {exc}")
                return sym_obj.tradingsymbol, []

        # Fan-out both daily + intraday fetches in parallel across all symbols.
        # Cap concurrency at 6 (3 daily + 3 intraday in parallel) so a cold
        # cache with 100 symbols doesn't fire 200 simultaneous broker
        # historical_data calls and saturate Kite's 3 req/s quota.  On a warm
        # cache (Tier 1 hits), each task resolves in <1 ms so the semaphore
        # never queues; response time is unaffected for the common hot path.
        _req_sem = asyncio.Semaphore(3)

        async def _fetch_daily_closes_throttled(sym_obj: SparklineSymbol) -> tuple[str, list[float]]:
            async with _req_sem:
                return await _fetch_daily_closes(sym_obj)

        async def _fetch_today_bars_throttled(sym_obj: SparklineSymbol) -> tuple[str, list[float]]:
            async with _req_sem:
                return await _fetch_today_bars(sym_obj)

        daily_tasks    = [_fetch_daily_closes_throttled(s) for s in norm_syms]
        intraday_tasks = [_fetch_today_bars_throttled(s) for s in norm_syms]
        daily_results, intraday_results = await asyncio.gather(
            asyncio.gather(*daily_tasks),
            asyncio.gather(*intraday_tasks),
        )

        past_result:  dict[str, list[float]] = dict(daily_results)
        today_result: dict[str, list[float]] = dict(intraday_results)

        # ── Step 1b: Self-heal — Tier 1+2 empty AND closed hours ─────────────
        # During closed hours `db_only=True` prevents broker calls in the store
        # fetchers.  When BOTH past closes AND today's intraday bars are empty
        # for a symbol (fresh install, cleared DB, prior db_worker write bug)
        # the db_only guard is counter-productive: the sparkline stays blank
        # forever.  Self-heal: retry those symbols with `bypass_cache=True`
        # (full 3-tier: Tier 1 → Tier 2 → Tier 3/broker) so the broker fills
        # both stores and the write-back queue heals the DB.
        #
        # Guard: only fire when broker is NOT in rate-limit cool-off so we
        # don't amplify a throttle event.  If the broker call fails (Kite 502
        # etc.) we fall through silently — the symbol stays empty this request
        # and the next request retries.
        if db_only:
            from backend.api.helpers.self_heal_log import _self_heal_log_once
            from backend.api.persistence.backfill import _price_broker_in_cooloff

            _broker_in_cooloff: bool = await asyncio.to_thread(_price_broker_in_cooloff)

            if not _broker_in_cooloff:
                _heal_syms = [
                    s for s in norm_syms
                    if not past_result.get(s.tradingsymbol)
                    and not today_result.get(s.tradingsymbol)
                ]

                if _heal_syms:
                    async def _self_heal_daily(sym_obj: SparklineSymbol) -> tuple[str, list[float]]:
                        try:
                            bars = await _ohlcv_store.get_or_fetch_daily(
                                sym_obj.tradingsymbol, sym_obj.exchange,
                                from_d=from_daily, to_d=yesterday,
                                bypass_cache=True,
                            )
                            closes = [b["close"] for b in bars]
                            if len(closes) > (days - 1):
                                closes = closes[-(days - 1):]
                            return sym_obj.tradingsymbol, closes
                        except Exception as exc:
                            logger.debug(f"sparkline self-heal: ohlcv miss for {sym_obj.tradingsymbol}: {exc}")
                            return sym_obj.tradingsymbol, []

                    async def _self_heal_intraday(sym_obj: SparklineSymbol) -> tuple[str, list[float]]:
                        try:
                            bars = await _intraday_store.get_or_fetch_intraday(
                                sym_obj.tradingsymbol, sym_obj.exchange,
                                on_date=today_date, interval="30minute",
                                bypass_cache=True,
                            )
                            closes = [b["close"] for b in bars]
                            return sym_obj.tradingsymbol, closes
                        except Exception as exc:
                            logger.debug(f"sparkline self-heal: intraday miss for {sym_obj.tradingsymbol}: {exc}")
                            return sym_obj.tradingsymbol, []

                    # Rate-limit guard: cap at 2 concurrent heals.
                    _heal_sem = asyncio.Semaphore(2)

                    async def _heal_daily_throttled(s: SparklineSymbol) -> tuple[str, list[float]]:
                        async with _heal_sem:
                            return await _self_heal_daily(s)

                    async def _heal_intraday_throttled(s: SparklineSymbol) -> tuple[str, list[float]]:
                        async with _heal_sem:
                            return await _self_heal_intraday(s)

                    _heal_daily_res, _heal_intraday_res = await asyncio.gather(
                        asyncio.gather(*[_heal_daily_throttled(s) for s in _heal_syms]),
                        asyncio.gather(*[_heal_intraday_throttled(s) for s in _heal_syms]),
                    )

                    for sym_str, closes in _heal_daily_res:
                        if closes:
                            past_result[sym_str] = closes
                    for sym_str, closes in _heal_intraday_res:
                        if closes:
                            today_result[sym_str] = closes

                    # Log once per (sym, exch) per 60 s — throttled by shared helper.
                    for s in _heal_syms:
                        combined = len(past_result.get(s.tradingsymbol, [])) + len(today_result.get(s.tradingsymbol, []))
                        _self_heal_log_once(s.tradingsymbol, s.exchange, 0, days)

        # ── Step 2: Build token_map for LTP lookup + ticker subscription ─────
        # We need the instrument_token for every normalised symbol to:
        #   a) read live LTP from the ticker's _tick_map (zero quota)
        #   b) push tokens to the ticker for future SSE ticks
        #   c) fall back to broker.ltp() for symbols not yet in the tick stream
        token_map: dict[str, int] = {}
        try:
            from backend.brokers.registry import get_sparkline_broker as _sb
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
        from backend.brokers.kite_ticker import get_ticker
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
        from backend.brokers.broker_apis import record_good_ltp as _record_ltp, get_last_good_ltp as _get_last_ltp
        ticker_hits: list[str] = []
        miss_keys: list[str] = []
        for qk in quote_keys:
            tok = key_to_token.get(qk)
            if tok is not None:
                ltp_val = ticker.get_ltp(tok)
                if ltp_val is not None:
                    ltp_map[qk] = ltp_val
                    ticker_hits.append(qk)
                    # Persist so closed-hours / cold-cache path can use last-known price.
                    sym_only = qk.split(":", 1)[-1]
                    _record_ltp(sym_only, ltp_val)
                else:
                    miss_keys.append(qk)
            else:
                miss_keys.append(qk)

        if ticker_hits:
            logger.debug(
                f"sparkline: {len(ticker_hits)} LTP(s) from tick_map, "
                f"{len(miss_keys)} fallback to broker.ltp()"
            )

        # ── Closed-hours guard: skip broker.ltp() when market is closed ──────
        # Determine whether all requested exchanges are currently closed.
        # If so: skip Pass 2 (broker.ltp()); do not append a live-LTP tail;
        # set `as_of` in the response so the frontend can show a staleness hint.
        # Daily closes (ohlcv_store) and intraday bars (intraday_store) are
        # already served from DB — they never triggered a broker call here.
        req_exchs_spark = {s.exchange.upper() for s in norm_syms}
        spark_market_closed = _all_exchanges_closed(req_exchs_spark)

        # Pass 2 — broker.ltp() for misses only.  Skipped when market is closed.
        if miss_keys and not spark_market_closed:
            try:
                from backend.brokers.registry import get_sparkline_broker as _get_sp_broker
                ltp_broker = _get_sp_broker()
                raw_ltp = await asyncio.to_thread(ltp_broker.ltp, miss_keys) or {}
                for key, val in raw_ltp.items():
                    if isinstance(val, dict):
                        lp = val.get("last_price")
                    else:
                        lp = val
                    try:
                        lp_f = float(lp) if lp is not None else 0.0
                        ltp_map[key] = lp_f
                        if lp_f > 0:
                            # Persist so closed-hours / cold-cache path can use last-known price.
                            sym_only = key.split(":", 1)[-1]
                            _record_ltp(sym_only, lp_f)
                    except (TypeError, ValueError):
                        pass
            except Exception as exc:
                logger.warning(f"sparkline: ltp fallback batch failed: {exc}")
        elif miss_keys and spark_market_closed:
            logger.debug(
                f"sparkline: market closed — skipping broker.ltp() for {len(miss_keys)} misses"
            )

        sparkline_as_of: Optional[str] = (
            datetime.now(timezone.utc).isoformat(timespec="seconds")
            if spark_market_closed else None
        )

        # ── Step 4: Compose result series ────────────────────────────────────
        # Delegate to compose_sparkline_series() — pure helper owns the
        # fallback ladder and the reason attribution. This route stays
        # responsible only for the closed-hours last-good-LTP lookup (which
        # needs the last-good store) and the empty-branch structured log
        # (which needs the throttled dict).
        result: dict[str, list[float]] = {}
        for sym_obj in norm_syms:
            sym  = sym_obj.tradingsymbol
            past = past_result.get(sym, [])
            today_bars = today_result.get(sym, [])
            ltp_key = f"{sym_obj.exchange}:{sym}"
            ltp_val = ltp_map.get(ltp_key)
            # Closed-hours: if ltp_map has no entry (ticker not subscribed,
            # broker.ltp() skipped) try the 24-hour last-good-LTP cache.
            # This covers pure mover symbols that never pass through
            # positions/holdings enrichment and thus never get recorded there.
            if (ltp_val is None or ltp_val == 0) and spark_market_closed:
                _cached_ltp = _get_last_ltp(sym, max_age_s=86400.0)
                if _cached_ltp and _cached_ltp > 0:
                    ltp_val = _cached_ltp

            series, _compose_reason = compose_sparkline_series(
                past=past,
                today_bars=today_bars,
                ltp_val=ltp_val,
                market_closed=spark_market_closed,
            )

            if not series:
                # Structured [SPARK-EMPTY] tag — reason attributed by the
                # canonical compose_sparkline_series() helper above so the
                # route + helper can never drift on reason semantics. The
                # cache_layer label is derived from reason for grep-ability.
                _layer_map = {
                    "warm_universe_empty":    "tier1_2_cache",
                    "historical_fetch_fail":  "tier3_broker",
                    "spark_past_cache_miss":  "tier1_past_cache",
                    "spark_today_cache_miss": "tier1_today_cache",
                }
                _layer = _layer_map.get(_compose_reason, "unknown")
                _now = _time_mod.monotonic()
                _key = (sym, sym_obj.exchange)
                if _now - _spark_empty_last_log.get(_key, 0.0) >= 3600:
                    _spark_empty_last_log[_key] = _now
                    logger.info(
                        f"[SPARK-EMPTY] symbol={sym_obj.exchange}:{sym} "
                        f"reason={_compose_reason} cache_layer={_layer} "
                        f"past={len(past)} today={len(today_bars)} "
                        f"ltp={ltp_val} market_closed={spark_market_closed}"
                    )
            if series:
                result[sym] = series
                # Dual-write: also store under the original bare watchlist name
                # (e.g. "CRUDEOIL") so the frontend renderer's
                # sparklines[row.tradingsymbol] lookup hits for MCX/CDS symbols
                # whose tradingsymbol on the grid row is the bare commodity/
                # currency name, not the resolved front-month contract.
                # Without this, sparklines["CRUDEOIL"] is always undefined while
                # sparklines["CRUDEOIL26JUNFUT"] is populated — causing the
                # sparkline cell to show "—" for every MCX/CDS watchlist row.
                # The frontend prune (active Set built from pairs.tradingsymbol)
                # also correctly retains the bare-name entry on subsequent calls
                # because pairs carry the original bare name from unifiedRows.
                for bare, resolved_name in orig_to_resolved.items():
                    if resolved_name == sym:
                        result[bare] = series

        return SparklineResponse(
            data=result,
            refreshed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            as_of=sparkline_as_of,
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
        from backend.brokers.kite_ticker import get_ticker

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
        from backend.brokers.kite_ticker import get_ticker
        ticker = get_ticker()
        # Deferred-start safety: the on_startup _start_kite_ticker()
        # hook may have run before Connections() finished restoring
        # the cached access_token. Retry the start here against the
        # same eligible Kite account preference order.
        if not ticker.status().get("started"):
            try:
                from backend.brokers.registry import get_sparkline_broker
                from backend.brokers.client import is_cutover_on
                spark_bk = get_sparkline_broker()
                for b in getattr(spark_bk, "_brokers", []):
                    api_key: str | None = None
                    access_token: str | None = None
                    # Cutover branch — when flag is on, the broker is a
                    # RemoteBroker with no local KiteConnect handle. Fetch
                    # the live token from conn_service over UDS.
                    if is_cutover_on() and b.broker_id in ("zerodha_kite", "kite"):
                        from backend.brokers.client.remote_broker import fetch_access_token
                        api_key, access_token = fetch_access_token(b.account)
                    else:
                        kc = getattr(b, "_conn", None) or getattr(b, "kite", None)
                        api_key = getattr(kc, "api_key", None)
                        access_token = (
                            getattr(kc, "_access_token", None)
                            or getattr(kc, "access_token", None)
                        )
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
        from backend.brokers.registry import get_sparkline_broker
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

    # Kite historical_data is a 3 req/sec budget per account. Two fan-outs
    # (daily + intraday) share the same 3 req/sec quota, so run them
    # sequentially with a SINGLE semaphore capped at 2 coroutines and a
    # 0.4s inter-task sleep. Prior shape ran both gather groups concurrently
    # inside an outer asyncio.gather — that allowed up to 6 simultaneous
    # broker calls (3 daily + 3 intraday), routinely triggering "too many
    # requests" on both Kite accounts within the first few seconds of the
    # warm run, which locked both brokers out for 30s each — well past the
    # warm window. The fix:
    #   (a) Single shared semaphore at concurrency=2 (leaves 1 slot spare
    #       for regular API calls during the warm run).
    #   (b) Intraday tasks only start AFTER the daily gather completes —
    #       halves peak concurrency against the broker.
    #   (c) Sleep bumped to 0.4s to fit within the 3 req/sec budget with
    #       a safety margin (2 concurrent × 1/0.4s = 5 req/s peak — but
    #       the semaphore gate means steady-state is ~2 req/s).
    _warm_sem = asyncio.Semaphore(2)

    async def _warm_daily(sym: str, exch: str) -> bool:
        async with _warm_sem:
            try:
                bars = await _ohlcv_store.get_or_fetch_daily(
                    sym, exch, from_d=from_daily, to_d=yesterday,
                )
                return bool(bars)
            except Exception:
                return False
            finally:
                await asyncio.sleep(0.4)

    async def _warm_intraday(sym: str, exch: str) -> bool:
        async with _warm_sem:
            try:
                bars = await _intraday_store.get_or_fetch_intraday(
                    sym, exch, on_date=today_date, interval="30minute",
                )
                return bool(bars)
            except Exception:
                return False
            finally:
                await asyncio.sleep(0.4)

    daily_tasks    = [_warm_daily(sym, exch)   for sym, exch in norm]
    intraday_tasks = [_warm_intraday(sym, exch) for sym, exch in norm]

    # Run daily first, then intraday — never both concurrently.
    # This halves peak broker load vs the prior asyncio.gather(gather, gather).
    daily_results    = await asyncio.gather(*daily_tasks)
    intraday_results = await asyncio.gather(*intraday_tasks)

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
