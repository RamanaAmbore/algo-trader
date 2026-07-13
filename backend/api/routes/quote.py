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
# _closed_hours_warm_day.
#
# _closed_hours_warm_in_progress is the SET of signatures currently mid-warm.
# Held between check-in and broker call so a second concurrent caller sees the
# sig as "already claimed" and exits without duplicating the broker.quote()
# fetch (TOCTOU dedup — the lock is released before the broker call so it
# cannot span the check + fetch atomically).
#
# _closed_hours_warm_failed_until throttles broker-storm during outages: a
# failed warm marks the sig unreachable for ~60 s so we don't hammer the
# broker on every 30 s poll during a sustained connection issue.
# All three are guarded by _closed_hours_warm_lock.
_closed_hours_warm_signatures: set[str] = set()
_closed_hours_warm_in_progress: set[str] = set()
_closed_hours_warm_failed_until: dict[str, float] = {}
_closed_hours_warm_day: str = ""
_closed_hours_warm_lock = threading.Lock()

# Cool-off window (seconds) after a failed warm attempt before we retry the
# broker.  Bounds broker-storm without leaving vol/oi blank all day.
_CLOSED_HOURS_WARM_FAIL_COOLOFF_S: float = 60.0


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


def _closed_hours_warm_claim(sig: str, today, now_epoch: float) -> bool:
    """Reserve the (day, sig) slot for the caller and return True when
    the caller should proceed with the broker fetch. Returns False when
    another coroutine is already warming this sig, when the sig was
    warmed successfully today, or when we're inside a broker-failure
    cool-off window.

    Called under `_closed_hours_warm_lock` internally — the caller MUST
    NOT hold the lock. On True the sig is placed in `_in_progress`; the
    caller MUST release it via the `finally` block after the broker
    round-trip.
    """
    global _closed_hours_warm_day
    with _closed_hours_warm_lock:
        if _closed_hours_warm_day != today:
            _closed_hours_warm_signatures.clear()
            _closed_hours_warm_in_progress.clear()
            _closed_hours_warm_failed_until.clear()
            _closed_hours_warm_day = today
        if sig in _closed_hours_warm_signatures:
            return False
        if sig in _closed_hours_warm_in_progress:
            return False
        fail_until = _closed_hours_warm_failed_until.get(sig, 0.0)
        if fail_until > now_epoch:
            return False
        _closed_hours_warm_in_progress.add(sig)
        return True


def _extract_top_price(side: list | None) -> float | None:
    """Extract the top-of-book price from one side of a depth payload.
    Returns None when the side is empty or the top row has no positive
    price. Wraps price coercion so callers can stay linear.
    """
    if not side:
        return None
    top = side[0] or {}
    price = top.get("price") or 0
    if not price:
        return None
    try:
        return float(price)
    except (TypeError, ValueError):
        return None


def _qt_parse_quote_scalars(q: dict) -> "tuple[float, float | None, float | None, int, int]":
    """Extract (ltp, close, open_, vol, oi) scalars from a raw broker quote dict."""
    ohlc = q.get("ohlc") or {}
    ltp   = float(q.get("last_price") or 0.0)
    close = float(ohlc.get("close") or 0.0) or None
    open_ = float(ohlc.get("open")  or 0.0) or None
    vol   = int(q.get("volume") or 0)
    oi    = int(q.get("oi") or 0)
    return ltp, close, open_, vol, oi


def _build_lkg_payload_from_quote(q: dict) -> dict:
    """Build the record_good_quote payload dict from a raw broker quote.
    Pure — no cache writes. Callers handle both LTP + quote persistence.
    """
    _ltp, _close, _open, _vol, _oi = _qt_parse_quote_scalars(q)
    _change = (_ltp - _close) if (_close and _ltp) else 0.0
    _chg_pct = (_change / _close * 100.0) if _close else 0.0
    depth = q.get("depth") or {}
    return {
        "last_price": _ltp,
        "open":       _open,
        "close":      _close,
        "volume":     _vol,
        "oi":         _oi,
        "change":     _change,
        "change_pct": _chg_pct,
        "bid":        _extract_top_price(depth.get("buy")),
        "ask":        _extract_top_price(depth.get("sell")),
    }


def _persist_one_closed_hours_quote(bkey: str, q: dict) -> bool:
    """Persist one broker quote payload into the LKG cache. Returns True
    on success, False when the payload was empty or malformed. Silently
    swallows per-symbol errors so a bad row can't derail the whole warm.
    """
    if not q:
        return False
    try:
        from backend.brokers.broker_apis import (
            record_good_ltp, record_good_quote,
        )
        _, sym_only = bkey.split(":", 1) if ":" in bkey else ("", bkey)
        payload = _build_lkg_payload_from_quote(q)
        _ltp = payload.pop("last_price")
        if _ltp > 0:
            record_good_ltp(sym_only, _ltp)
        record_good_quote(sym_only, payload)
        return True
    except Exception:
        return False


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
    if not key_map or not getattr(key_map, "broker_keys", None):
        return
    today = _ist_today()
    sig = ",".join(sorted(key_map.broker_keys))
    now_epoch = _time_mod.time()

    if not _closed_hours_warm_claim(sig, today, now_epoch):
        return

    try:
        try:
            from backend.brokers.registry import get_market_data_broker
            broker = get_market_data_broker()
            quote_data = await asyncio.to_thread(
                broker.quote, key_map.broker_keys,
            ) or {}
        except Exception as exc:
            # Broker failed — mark sig unreachable for ~60 s (bounds storm)
            # then bail.  Next request after cool-off will retry.
            with _closed_hours_warm_lock:
                _closed_hours_warm_failed_until[sig] = (
                    _time_mod.time() + _CLOSED_HOURS_WARM_FAIL_COOLOFF_S
                )
            logger.debug(f"batch_quote: closed-hours warm skipped: {exc}")
            return

        _persisted = sum(
            1 for bkey, q in quote_data.items()
            if _persist_one_closed_hours_quote(bkey, q)
        )
        if _persisted:
            # Promote sig only on successful warm — add to steady-state
            # dedup set in the same lock as the in-progress release below
            # so an incoming caller sees either (a) sig in _in_progress
            # (skip), or (b) sig in _signatures (skip), never a window
            # where both sets are empty and a duplicate broker fetch runs.
            with _closed_hours_warm_lock:
                _closed_hours_warm_signatures.add(sig)
            logger.info(
                f"batch_quote: closed-hours warm — persisted LKG for {_persisted}/{len(quote_data)} symbols"
            )
    finally:
        # Always release the in-progress claim.  Ordering guarantee: on
        # success, `_signatures.add(sig)` above ran BEFORE this discard,
        # so no window exists where the sig is absent from both sets.
        with _closed_hours_warm_lock:
            _closed_hours_warm_in_progress.discard(sig)


# ── Closed-hours helper ───────────────────────────────────────────────────────

def _exchanges_from_keys(keys: list[str]) -> set[str]:
    """Extract the set of exchange codes from 'EXCHANGE:SYMBOL' keys."""
    out: set[str] = set()
    for k in keys:
        if ":" in k:
            out.add(k.split(":", 1)[0].upper())
    return out


def _qt_is_seg_open(seg_cfg: dict, exch_upper: str, now, fetch_holidays) -> "bool | None":
    """Check whether a single segment config covers *exch_upper* and is currently open.

    Returns True (open), False (segment matches but closed), or None (segment
    does not cover this exchange — caller should skip).
    """
    from backend.shared.helpers.date_time_utils import is_market_open
    from datetime import time as dtime

    seg_exch = (seg_cfg.get("holiday_exchange") or "NSE").upper()
    seg_exchanges = [e.upper() for e in (seg_cfg.get("exchanges") or [seg_exch])]
    if exch_upper not in seg_exchanges and exch_upper != seg_exch:
        return None  # not a match for this exchange
    h_s, m_s = map(int, seg_cfg.get("hours_start", "09:15").split(":"))
    h_e, m_e = map(int, seg_cfg.get("hours_end",   "15:30").split(":"))
    try:
        holidays = fetch_holidays(seg_exch)
    except Exception:
        holidays = set()
    return is_market_open(now, holidays, dtime(h_s, m_s), dtime(h_e, m_e), exchange=seg_exch)


def _is_exchange_segment_closed(exchange: str) -> bool:
    """Return True if the given exchange's segment is currently closed.

    Reads the same `market_segments` config that `is_any_segment_open`
    uses so a single YAML edit is enough to change the covered segments.
    Falls back to `is_any_segment_open` for exchanges not listed — safe
    default that keeps the live path active.
    """
    try:
        from backend.shared.helpers.date_time_utils import timestamp_indian
        from backend.shared.helpers.utils import config as _cfg
        from backend.brokers.broker_apis import fetch_holidays

        now = timestamp_indian()
        exch_upper = exchange.upper()

        segments = _cfg.get("market_segments", {}) or {}
        for _seg_name, seg_cfg in segments.items():
            is_open = _qt_is_seg_open(seg_cfg, exch_upper, now, fetch_holidays)
            if is_open is True:
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

async def _qt_slow_walk_token(broker, sym: str, order: list) -> "int | None":
    """Slow-path broker.instruments() walk across *order* exchange list.

    Called when the day-cached token map is cold or empty. Returns the first
    matching instrument_token or None when no match is found.
    """
    for ex in order:
        try:
            insts = await asyncio.to_thread(broker.instruments, ex) or []
        except Exception:
            continue
        for inst in insts:
            if str(inst.get("tradingsymbol") or "").upper() == sym:
                return int(inst["instrument_token"])
    return None


async def _resolve_token_for_sym(tradingsymbol: str, exchange: str) -> int | None:
    """
    Resolve a single (tradingsymbol, exchange) pair to its Kite
    instrument_token. Used by the watchlist add-item hook to subscribe the
    new symbol to the TickerManager immediately after DB insert.

    Consults the day-cached token map (_get_today_token_map) first so the
    vast majority of calls return immediately without a broker.instruments()
    HTTP round-trip. Only falls back to a live broker call when the day-cache
    is cold (once per IST day, typically pre-warmed by batch_sparkline).

    Walk order — equity (NSE/BSE): exchange → companion → MCX → CDS → NFO → BFO.
    All others: exchange → MCX → CDS → NFO/BFO → NSE → BSE. Returns None on
    any failure so callers can treat the subscription as best-effort.
    """
    try:
        from backend.brokers.registry import get_sparkline_broker
        broker = get_sparkline_broker()
    except Exception:
        return None

    sym  = tradingsymbol.upper().strip()
    exch = exchange.upper().strip()
    order = _exchange_walk_order(exch)

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
    return await _qt_slow_walk_token(broker, sym, order)


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


def _qt_fetch_full_quote(
    broker,
    broker_key: str,
) -> "tuple[float, int, list[DepthLevel], list[DepthLevel], float | None, float | None]":
    """Fetch full quote depth for one broker key.

    Returns (ltp, volume, depth_buy, depth_sell, bid, ask).
    Raises on any broker error — caller catches and falls back.
    """
    full = broker.quote([broker_key]).get(broker_key) or {}
    ltp    = float(full.get("last_price") or 0.0)
    volume = int(full.get("volume") or 0)
    depth  = full.get("depth") or {}
    db     = _parse_depth_levels(depth.get("buy"))
    ds     = _parse_depth_levels(depth.get("sell"))
    bid    = db[0].price if db else None
    ask    = ds[0].price if ds else None
    return ltp, volume, db, ds, bid, ask


def _fetch_ltp(
    broker_exchange: str,
    broker_tradingsymbol: str,
    display_exchange: str | None = None,
    display_tradingsymbol: str | None = None,
) -> QuoteResponse:
    """Fetch LTP + depth for one symbol. Falls back to ltp-only on depth failure.

    broker_exchange / broker_tradingsymbol   — resolved contract key for broker.
    display_exchange / display_tradingsymbol — operator-facing symbol for response.
    """
    from backend.brokers.registry import get_market_data_broker

    broker_key    = f"{broker_exchange.upper()}:{broker_tradingsymbol.upper()}"
    disp_exchange = display_exchange or broker_exchange
    disp_sym      = display_tradingsymbol or broker_tradingsymbol
    broker        = get_market_data_broker()

    bid = ask = None
    depth_buy: list[DepthLevel]  = []
    depth_sell: list[DepthLevel] = []
    volume = 0
    ltp    = 0.0

    try:
        ltp, volume, depth_buy, depth_sell, bid, ask = _qt_fetch_full_quote(broker, broker_key)
    except Exception as e:
        logger.warning(f"Quote depth failed for {broker_key}: {e}")
        try:
            row = (broker.ltp([broker_key]) or {}).get(broker_key) or {}
            ltp = float(row.get("last_price") or 0.0)
        except Exception as e2:
            logger.error(f"Quote LTP fallback failed for {broker_key}: {e2}")

    return QuoteResponse(
        tradingsymbol=disp_sym,
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


# ── batch_quote helpers ───────────────────────────────────────────────────────

def _normalize_batch_keys(raw_keys: list[str]) -> list[str]:
    """Deduplicate, strip, filter to 'EXCHANGE:SYMBOL' shape, cap at 300."""
    seen: set[str] = set()
    out: list[str] = []
    for k in (raw_keys or []):
        k = k.strip()
        if k and ":" in k and k not in seen:
            seen.add(k)
            out.append(k)
    return out[:300]


def _qt_lkg_ltp_snap(sym: str, resolved_sym: str, get_last_good_ltp, get_last_good_quote) -> "tuple[float, dict]":
    """Fetch LKG ltp + snapshot dict for *sym*, trying *resolved_sym* first."""
    ltp = (
        get_last_good_ltp(resolved_sym, max_age_s=86400.0) or
        get_last_good_ltp(sym, max_age_s=86400.0) or
        0.0
    )
    snap: dict = (
        get_last_good_quote(resolved_sym, max_age_s=86400.0) or
        get_last_good_quote(sym, max_age_s=86400.0) or
        {}
    )
    return ltp, snap


def _qt_build_lkg_row(k: str, key_map, get_last_good_ltp, get_last_good_quote) -> "BatchQuoteRow | None":
    """Build a stale BatchQuoteRow from LKG cache for key *k*.

    Returns None when *k* cannot be split into exchange:symbol.
    """
    try:
        exch, sym = k.split(":", 1)
    except ValueError:
        return None
    broker_key = key_map.input_to_broker.get(k, k)
    _, resolved_sym = broker_key.split(":", 1) if ":" in broker_key else ("", sym)
    ltp, snap = _qt_lkg_ltp_snap(sym, resolved_sym, get_last_good_ltp, get_last_good_quote)
    return BatchQuoteRow(
        exchange=exch, tradingsymbol=sym,
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
    )


async def _serve_closed_hours_batch(
    keys: list[str],
    key_map,
) -> "BatchQuoteResponse":
    """Return a BatchQuoteResponse served entirely from LKG cache.

    Fires a one-shot broker.quote() warm (cold-start guard) then builds rows
    from get_last_good_ltp / get_last_good_quote.  All rows carry stale=True
    and the as_of field is set so the frontend can show a staleness hint.
    """
    from datetime import datetime, timezone
    from backend.brokers.broker_apis import (
        get_last_good_ltp, get_last_good_quote,
    )

    logger.debug(f"batch_quote: market closed — serving LKG for {len(keys)} keys")

    # Cold-start warm — one broker.quote() per IST day per key-set signature.
    await _maybe_warm_closed_hours_quotes(keys, key_map)

    as_of_str = datetime.now(timezone.utc).isoformat(timespec="seconds")
    items: list[BatchQuoteRow] = []
    for k in keys:
        row = _qt_build_lkg_row(k, key_map, get_last_good_ltp, get_last_good_quote)
        if row is not None:
            items.append(row)
    return BatchQuoteResponse(
        refreshed_at=as_of_str,
        items=items,
        as_of=as_of_str,
    )


def _extract_bid_ask(q: dict) -> tuple["Optional[float]", "Optional[float]"]:
    """Extract best bid and ask from a broker.quote() response dict.

    Returns (bid, ask) — both None when depth is absent or has zero price.
    """
    depth = q.get("depth") or {}
    buys  = depth.get("buy") or []
    sells = depth.get("sell") or []
    bid = float(buys[0]["price"])  if buys  and (buys[0].get("price") or 0)  else None
    ask = float(sells[0]["price"]) if sells and (sells[0].get("price") or 0) else None
    return bid, ask


def _qt_live_change(ltp: float, close: "float | None") -> "tuple[float, float]":
    """Compute absolute change and change-pct from ltp and previous close."""
    change  = (ltp - close) if (close and ltp) else 0.0
    chg_pct = (change / close * 100.0) if close else 0.0
    return change, chg_pct


def _build_live_batch_row(k: str, quote_data: dict, key_map) -> "BatchQuoteRow":
    """Build one BatchQuoteRow from the broker.quote() response dict.

    Returns a stale=True row with ltp=0 when the broker had no data for the key.
    """
    exch, sym = k.split(":", 1)
    broker_key = key_map.input_to_broker.get(k, k)
    q = quote_data.get(broker_key) or {}
    ltp, close, open_, vol, oi = _qt_parse_quote_scalars(q)
    bid, ask = _extract_bid_ask(q)
    change, chg_pct = _qt_live_change(ltp, close)
    return BatchQuoteRow(
        exchange=exch, tradingsymbol=sym,
        ltp=ltp, bid=bid, ask=ask, open=open_, close=close,
        change=change, change_pct=chg_pct,
        volume=vol,
        oi=oi,
        stale=(not q),
    )


def _record_live_batch_lkg(
    broker_key: str,
    sym: str,
    ltp: float,
    open_: "Optional[float]",
    close: "Optional[float]",
    vol: int,
    oi: int,
    change: float,
    chg_pct: float,
    bid: "Optional[float]",
    ask: "Optional[float]",
) -> None:
    """Persist LKG LTP + quote snapshot for the closed-hours fallback path."""
    _, resolved_sym_only = (
        broker_key.split(":", 1) if ":" in broker_key else ("", sym)
    )
    if ltp and ltp > 0:
        _record_good_ltp_live(resolved_sym_only, ltp)
    _record_good_quote_live(resolved_sym_only, {
        "open":       open_,
        "close":      close,
        "volume":     vol,
        "oi":         oi,
        "change":     change,
        "change_pct": chg_pct,
        "bid":        bid,
        "ask":        ask,
    })


async def _subscribe_batch_universe_to_ticker(seen_pairs: list[tuple[str, str]]) -> None:
    """Subscribe the queried universe to the live ticker.

    subscribe_with_sym is idempotent + cheap; safe to call on every batch
    request. Errors swallowed — ticker subscribe is best-effort.
    """
    if not seen_pairs:
        return
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
        from datetime import datetime, timezone
        from backend.brokers.registry import get_market_data_broker
        from backend.api.algo.symbol_resolver import resolve_market_data_keys

        # ── Normalise keys ─────────────────────────────────────────────────
        keys = _normalize_batch_keys(data.keys or [])

        # ── Virtual root resolution ────────────────────────────────────────
        # Resolve MCX/CDS bare roots to front-month contracts so broker calls
        # succeed. key_map carries both directions so we can emit response rows
        # keyed on the original operator symbol.
        key_map = await resolve_market_data_keys(keys)

        # ── Closed-hours fast-path ─────────────────────────────────────────
        req_exchanges = _exchanges_from_keys(keys)
        if _all_exchanges_closed(req_exchanges):
            return await _serve_closed_hours_batch(keys, key_map)

        # ── Live path (market open) ────────────────────────────────────────
        quote_data: dict = {}
        if key_map.broker_keys:
            try:
                broker = get_market_data_broker()
                quote_data = await asyncio.to_thread(broker.quote, key_map.broker_keys) or {}
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"Batch quote failed: {exc}")
                quote_data = {}

        # Build rows + seen_pairs (for ticker subscribe) in one pass.
        # seen_pairs tracks ORIGINAL (operator-facing) symbols so the
        # ticker subscribe uses the same sym key that SSE listeners
        # registered for.
        items: list[BatchQuoteRow] = []
        seen_pairs: list[tuple[str, str]] = []
        for k in keys:
            if ":" not in k:
                continue
            row = _build_live_batch_row(k, quote_data, key_map)
            items.append(row)
            exch, sym = k.split(":", 1)
            seen_pairs.append((exch.upper(), sym.upper()))

            # Record LKG for closed-hours fallback.  Key by the RESOLVED
            # broker symbol so virtual roots (MCX:CRUDEOIL → CRUDEOIL26JUNFUT)
            # persist under the same key both live-path and closed-hours
            # readers use.
            broker_key = key_map.input_to_broker.get(k, k)
            if quote_data.get(broker_key):
                _record_live_batch_lkg(
                    broker_key, sym, row.ltp, row.open, row.close,
                    row.volume, row.oi, row.change, row.change_pct, row.bid, row.ask,
                )

        # Subscribe the queried universe to the live ticker so SSE starts
        # streaming LTP for these symbols immediately.
        await _subscribe_batch_universe_to_ticker(seen_pairs)

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


def _qt_broker_token_map(broker) -> "dict[tuple[str, str], int]":  # type: ignore[no-untyped-def]
    """Fetch {(tradingsymbol, exchange) → token} from broker.instruments.

    Walks all sparkline exchanges. Called by _get_today_token_map on cache miss.
    Build outside any lock — broker.instruments calls are slow (~500 kB each).
    """
    new_map: dict[tuple[str, str], int] = {}
    for exch in _SPARKLINE_EXCHANGES:
        try:
            rows = broker.instruments(exch) or []
            if not rows:
                logger.warning(
                    f"_get_today_token_map: {exch} returned 0 instruments from broker "
                    "(BSE-only symbols on this exchange will not get live LTP)"
                )
                continue
            for row in rows:
                ts  = row.get("tradingsymbol")
                tok = row.get("instrument_token")
                if ts and tok:
                    new_map[(str(ts).upper(), exch)] = int(tok)
        except Exception as _exc:
            logger.warning(f"_get_today_token_map: {exch} instruments fetch failed: {_exc}")
    return new_map


def _qt_read_instr_store_tier1(today: str) -> "dict[tuple[str, str], int] | None":
    """Read the instruments_store Tier-1 memory cache.

    Returns the merged token map when ALL sparkline exchanges are warmed,
    or None when incomplete or unavailable.
    """
    try:
        from backend.api.persistence.instruments_store import (
            _MEM_CACHE as _instr_mem,
            _SPARKLINE_EXCHANGES as _INSTR_EXCHS,
            _purge_stale,
        )
        _purge_stale(today)
        warm_keys = {(today, exch) for exch in _INSTR_EXCHS}
        if not warm_keys.issubset(_instr_mem.keys()):
            return None
        union: dict[tuple[str, str], int] = {}
        for exch in _INSTR_EXCHS:
            cached_exch = _instr_mem.get((today, exch))
            if cached_exch:
                union.update(cached_exch)
        return union if union else None
    except Exception:
        return None


def _get_today_token_map(broker) -> dict[tuple[str, str], int]:  # type: ignore[no-untyped-def]
    """Return the day-cached {(tradingsymbol, exchange) → token} map.

    Read priority:
      1. instruments_store._MEM_CACHE (Tier 1) — O(1) sync read.
      2. Module-level _TOKEN_MAP_CACHE — used before persistent store is warmed.
      3. Live broker.instruments() walk — fires background store populate.

    Always called via asyncio.to_thread — must stay synchronous.
    """
    today = _ist_today()

    # ── Tier 1: instruments_store memory cache ─────────────────────────────────
    union = _qt_read_instr_store_tier1(today)
    if union:
        with _TOKEN_MAP_LOCK:
            _TOKEN_MAP_CACHE[today] = union
        return union

    # ── Tier 2: module-level cache ────────────────────────────────────────────
    with _TOKEN_MAP_LOCK:
        cached = _TOKEN_MAP_CACHE.get(today)
        if cached is not None:
            return cached

    # ── Tier 3: live broker fetch ─────────────────────────────────────────────
    new_map = _qt_broker_token_map(broker)
    with _TOKEN_MAP_LOCK:
        for k in list(_TOKEN_MAP_CACHE):
            if k != today:
                _TOKEN_MAP_CACHE.pop(k, None)
        _TOKEN_MAP_CACHE[today] = new_map

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


def _exchange_walk_order(exch: str) -> list[str]:
    """Priority-ordered exchange walk for token resolution.

    Equity (NSE/BSE): pair the companion exchange immediately so a BSE-only
    symbol is found on the first fallback without walking derivatives exchanges.
    All others: start with the requested exchange then walk the remaining list.
    """
    if exch in ("NSE", "BSE"):
        companion = "BSE" if exch == "NSE" else "NSE"
        return [exch, companion] + [e for e in ("MCX", "CDS", "NFO", "BFO") if e not in (exch, companion)]
    return [exch] + [e for e in ("MCX", "CDS", "NFO", "BFO", "NSE", "BSE") if e != exch]


def _parse_depth_levels(side: list | None) -> list["DepthLevel"]:
    """Parse one side of a broker depth payload into a list of DepthLevel structs.

    Iterates up to 5 levels, coercing price/quantity/orders to the correct types.
    Levels with zero or missing price are excluded. Pure function — no I/O.
    """
    out: list[DepthLevel] = []
    for level in (side or [])[:5]:
        p = float(level.get("price") or 0)
        q = int(level.get("quantity") or 0)
        o = int(level.get("orders") or 0)
        if p > 0:
            out.append(DepthLevel(price=p, quantity=q, orders=o))
    return out


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


def _qt_empty_reason(
    past: list[float],
    today_bars: list[float],
    market_closed: bool,
) -> str:
    """Return the failure reason label when no sparkline series can be built."""
    if len(past) == 0 and len(today_bars) == 0:
        return "warm_universe_empty" if market_closed else "historical_fetch_fail"
    if len(past) == 0:
        return "spark_past_cache_miss"
    if len(today_bars) == 0:
        return "spark_today_cache_miss"
    return "unknown"


def _qt_build_spark_series(
    past: list[float],
    today_bars: list[float],
    ltp_val: "Optional[float]",
    market_closed: bool,
) -> "tuple[list[float], str] | None":
    """Build primary sparkline series when historical data is available.

    Appends the LTP tail when positive. Returns (series, reason) when at
    least one of past/today_bars is non-empty; else None.
    """
    tail_ltp = ltp_val if (ltp_val and ltp_val > 0) else None
    series: list[float] = list(past) + list(today_bars)
    if tail_ltp is not None:
        series = series + [tail_ltp]
    if series and (past or today_bars):
        reason = "snapshot" if market_closed else "live"
        if len(series) == 1:
            series = series + series
            reason = "single_point_pad"
        return series, reason
    return None


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
        → past + today_bars + [ltp_val], reason='live' (open) or 'snapshot' (closed)
      past >= 1 AND market closed AND ltp_val is None:
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

    Reason 'live' == "actually rendered from live-ish data (market open)".
    Reason 'snapshot' == "rendered from frozen data (market closed)" — still
    displayable but the frontend can label it "as of <time>".

    When market_closed=True and ltp_val is provided (sourced from daily_book.ltp
    or 24h LKG cache), it IS appended as the final data point. This is the last
    known price from when the market closed — valid frozen data that prevents
    the frontend from rejecting the series as "too short" and falling back to
    yesterday's cached curve shape.
    """
    primary = _qt_build_spark_series(past, today_bars, ltp_val, market_closed)
    if primary is not None:
        return primary

    tail_ltp = ltp_val if (ltp_val and ltp_val > 0) else None
    series: list[float] = list(past) + list(today_bars)
    if tail_ltp is not None:
        series = series + [tail_ltp]

    if not series and ltp_val and ltp_val > 0:
        return [ltp_val, ltp_val], "ltp_only_flat_pad"

    if len(series) == 1:
        return series + series, "single_point_pad"

    return [], _qt_empty_reason(past, today_bars, market_closed)


# ── batch_sparkline helpers ───────────────────────────────────────────────────

async def _qt_resolve_mcx_sym(sym: str, root: str, is_next: bool) -> str:
    """Resolve an MCX bare root or _NEXT symbol to its front-month contract."""
    from backend.api.routes.watchlist import _resolve_mcx_commodity
    from backend.api.algo.symbol_resolver import resolve_symbol
    if is_next:
        resolved = await resolve_symbol(sym, "MCX")
        if resolved and resolved != sym:
            return resolved.upper().strip()
    else:
        resolved = await _resolve_mcx_commodity(root)
        if resolved:
            return resolved.upper().strip()
    return sym


async def _qt_resolve_cds_sym(sym: str, root: str, is_next: bool) -> str:
    """Resolve a CDS bare root or _NEXT symbol to its front-month contract."""
    from backend.api.routes.watchlist import _resolve_cds_currency
    from backend.api.algo.symbol_resolver import resolve_symbol
    if is_next:
        resolved = await resolve_symbol(sym, "CDS")
        if resolved and resolved != sym:
            return resolved.upper().strip()
    else:
        resolved = await _resolve_cds_currency(root)
        if resolved:
            return resolved.upper().strip()
    return sym


async def _normalize_sparkline_symbols(
    syms: list["SparklineSymbol"],
) -> tuple[list["SparklineSymbol"], dict[str, str]]:
    """Resolve MCX/CDS bare roots to front-month contracts.

    Returns ``(norm_syms, orig_to_resolved)`` where orig_to_resolved maps
    original bare symbol → resolved contract symbol for dual-write in Step 4.
    """
    from backend.api.algo.symbol_resolver import _strip_next

    norm_syms: list[SparklineSymbol] = []
    orig_to_resolved: dict[str, str] = {}
    for sym_obj in syms:
        sym  = sym_obj.tradingsymbol.upper().strip()
        exch = (sym_obj.exchange or "NSE").upper().strip()
        original_sym = sym
        root, is_next = _strip_next(sym)
        is_bare_root = root.isalpha() and len(root) <= 12
        if exch == "MCX" and is_bare_root:
            sym = await _qt_resolve_mcx_sym(sym, root, is_next)
        elif exch == "CDS" and is_bare_root:
            sym = await _qt_resolve_cds_sym(sym, root, is_next)
        if sym != original_sym:
            orig_to_resolved[original_sym] = sym
        norm_syms.append(SparklineSymbol(tradingsymbol=sym, exchange=exch))
    return norm_syms, orig_to_resolved


async def _fetch_bars_parallel(
    norm_syms: list["SparklineSymbol"],
    from_daily: "date",
    yesterday: "date",
    today_date: "date",
    days: int,
    db_only: bool,
) -> tuple[dict[str, list[float]], dict[str, list[float]]]:
    """Fetch daily closes and today's 30-min bars in parallel for all symbols.

    Returns ``(past_result, today_result)`` dicts keyed by tradingsymbol.
    """
    from backend.api.persistence import ohlcv_store as _ohlcv_store
    from backend.api.persistence import intraday_store as _intraday_store

    async def _fetch_daily(sym_obj: SparklineSymbol) -> tuple[str, list[float]]:
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

    async def _fetch_intraday(sym_obj: SparklineSymbol) -> tuple[str, list[float]]:
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

    # Cap concurrency at 3 so a cold cache with 100 symbols doesn't fire 200
    # simultaneous broker calls and saturate Kite's 3 req/s quota.
    _req_sem = asyncio.Semaphore(3)

    async def _daily_throttled(s: SparklineSymbol) -> tuple[str, list[float]]:
        async with _req_sem:
            return await _fetch_daily(s)

    async def _intraday_throttled(s: SparklineSymbol) -> tuple[str, list[float]]:
        async with _req_sem:
            return await _fetch_intraday(s)

    daily_res, intraday_res = await asyncio.gather(
        asyncio.gather(*[_daily_throttled(s) for s in norm_syms]),
        asyncio.gather(*[_intraday_throttled(s) for s in norm_syms]),
    )
    return dict(daily_res), dict(intraday_res)


async def _qt_heal_daily_one(
    sym_obj: "SparklineSymbol",
    ohlcv_store,
    from_daily: "date",
    yesterday: "date",
    days: int,
) -> "tuple[str, list[float]]":
    """Fetch self-heal OHLCV daily closes for one symbol (bypass cache)."""
    try:
        bars = await ohlcv_store.get_or_fetch_daily(
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


async def _qt_heal_intraday_one(
    sym_obj: "SparklineSymbol",
    intraday_store,
    today_date: "date",
) -> "tuple[str, list[float]]":
    """Fetch self-heal intraday 30-min closes for one symbol (bypass cache)."""
    try:
        bars = await intraday_store.get_or_fetch_intraday(
            sym_obj.tradingsymbol, sym_obj.exchange,
            on_date=today_date, interval="30minute",
            bypass_cache=True,
        )
        closes = [b["close"] for b in bars]
        return sym_obj.tradingsymbol, closes
    except Exception as exc:
        logger.debug(f"sparkline self-heal: intraday miss for {sym_obj.tradingsymbol}: {exc}")
        return sym_obj.tradingsymbol, []


async def _qt_run_heal_tasks(
    heal_syms: "list[SparklineSymbol]",
    past_result: dict,
    today_result: dict,
    ohlcv_store,
    intraday_store,
    from_daily: "date",
    yesterday: "date",
    today_date: "date",
    days: int,
) -> None:
    """Run throttled self-heal OHLCV + intraday fetches and apply results in-place."""
    _heal_sem = asyncio.Semaphore(2)

    async def _throttled_daily(s: "SparklineSymbol") -> "tuple[str, list[float]]":
        async with _heal_sem:
            return await _qt_heal_daily_one(s, ohlcv_store, from_daily, yesterday, days)

    async def _throttled_intraday(s: "SparklineSymbol") -> "tuple[str, list[float]]":
        async with _heal_sem:
            return await _qt_heal_intraday_one(s, intraday_store, today_date)

    daily_res, intraday_res = await asyncio.gather(
        asyncio.gather(*[_throttled_daily(s) for s in heal_syms]),
        asyncio.gather(*[_throttled_intraday(s) for s in heal_syms]),
    )
    for sym_str, closes in daily_res:
        if closes:
            past_result[sym_str] = closes
    for sym_str, closes in intraday_res:
        if closes:
            today_result[sym_str] = closes


async def _self_heal_empty_bars(
    norm_syms: list["SparklineSymbol"],
    past_result: dict[str, list[float]],
    today_result: dict[str, list[float]],
    from_daily: "date",
    yesterday: "date",
    today_date: "date",
    days: int,
) -> None:
    """Step 1b: bypass db_only guard for symbols with no data at all.

    Mutates past_result / today_result in-place. Fires broker calls only when
    broker is not in rate-limit cool-off. Guards: one-time structured log per
    (sym, exch) per 60 s via _self_heal_log_once.
    """
    from backend.api.helpers.self_heal_log import _self_heal_log_once
    from backend.api.persistence.backfill import _price_broker_in_cooloff
    from backend.api.persistence import ohlcv_store as _ohlcv_store
    from backend.api.persistence import intraday_store as _intraday_store

    if await asyncio.to_thread(_price_broker_in_cooloff):
        return

    heal_syms = [
        s for s in norm_syms
        if not past_result.get(s.tradingsymbol)
        and not today_result.get(s.tradingsymbol)
    ]
    if not heal_syms:
        return

    await _qt_run_heal_tasks(
        heal_syms, past_result, today_result,
        _ohlcv_store, _intraday_store,
        from_daily, yesterday, today_date, days,
    )
    for s in heal_syms:
        _self_heal_log_once(s.tradingsymbol, s.exchange, 0, days)


def _build_sparkline_candidate_syms(
    miss_tradingsymbols: list[str],
    orig_to_resolved: dict[str, str],
) -> set[str]:
    """Build the set of DB candidate symbols for the Tier 4 daily_book query.

    Includes the normalised (resolved) tradingsymbol for each miss plus any
    bare-root / contract-name variants so we can match when daily_book holds
    the contract name (CRUDEOIL26JULFUT) but the request used the virtual
    bare root (CRUDEOIL), or vice-versa.

    Args:
        miss_tradingsymbols: tradingsymbol strings for symbols missing after Tiers 1-3.
        orig_to_resolved:    bare-root → resolved-contract mapping from Step 1.

    Returns a set[str] ready for SQL ``ANY(:symbols)`` (caller wraps in list()).
    """
    resolved_to_bare: dict[str, str] = {v: k for k, v in orig_to_resolved.items()}
    candidates: set[str] = set(miss_tradingsymbols)
    for sym in miss_tradingsymbols:
        bare = resolved_to_bare.get(sym)
        if bare:
            candidates.add(bare)
        resolved = orig_to_resolved.get(sym)
        if resolved:
            candidates.add(resolved)
    return candidates


def _resolve_sparkline_db_key(
    db_sym: str,
    miss_syms_set: set[str],
    orig_to_resolved: dict[str, str],
    resolved_to_bare: dict[str, str],
) -> str | None:
    """Map a DB row's symbol back to the key used in ``past_result``.

    Three possible directions (in priority order):
    1. Direct hit   — ``db_sym`` is itself a requested symbol.
    2. Bare→resolved — ``db_sym`` is a bare root; its resolved contract is requested.
    3. Resolved→bare — ``db_sym`` is a resolved contract; its bare root is requested.

    Returns the matching key (a tradingsymbol from ``miss_syms_set``), or ``None``
    if no mapping exists.  In practice ``None`` is unreachable because the SQL
    ``WHERE symbol = ANY(:symbols)`` constrains ``db_sym`` to be a value derived
    from ``miss_syms_set`` via ``_build_sparkline_candidate_syms``; ``None`` is
    returned as a safety net rather than a silent write to an unrecognised key.
    """
    if db_sym in miss_syms_set:
        return db_sym
    resolved = orig_to_resolved.get(db_sym)
    if resolved and resolved in miss_syms_set:
        return resolved
    bare = resolved_to_bare.get(db_sym)
    if bare and bare in miss_syms_set:
        return bare
    return None


def _qt_parse_sparkline_payload(payload_raw) -> "list[float]":
    """Parse a raw daily_book sparkline payload into LTP floats."""
    try:
        payload = json.loads(payload_raw) if isinstance(payload_raw, str) else payload_raw
        points: list[dict] = payload.get("points") or []
        return [float(p["ltp"]) for p in points if p.get("ltp") is not None]
    except Exception:
        return []


def _qt_apply_db_sparkline_rows(
    db_rows: list,
    miss_syms_set: set,
    orig_to_resolved: dict,
    resolved_to_bare: dict,
    past_result: dict,
) -> None:
    """Write daily_book sparkline rows into past_result where data is missing."""
    for (db_sym, payload_raw) in db_rows:
        if not payload_raw:
            continue
        ltp_series = _qt_parse_sparkline_payload(payload_raw)
        if not ltp_series:
            continue
        target_key = _resolve_sparkline_db_key(
            db_sym, miss_syms_set, orig_to_resolved, resolved_to_bare
        )
        if target_key is None:
            continue
        if not past_result.get(target_key):
            past_result[target_key] = ltp_series
            logger.debug(
                f"sparkline tier4: daily_book fallback hit "
                f"sym={db_sym} → key={target_key} points={len(ltp_series)}"
            )


async def _qt_query_daily_book_sparkline(candidate_syms: list) -> list:
    """Run the daily_book sparkline DISTINCT-ON query and return raw rows."""
    from backend.api.database import async_session
    from sqlalchemy import text as sa_text

    async with async_session() as sess:
        q = sa_text("""
            SELECT DISTINCT ON (symbol)
                symbol, payload_json
            FROM daily_book
            WHERE kind    = 'sparkline'
              AND account = '__firm__'
              AND symbol  = ANY(:symbols)
            ORDER BY symbol,
                     date DESC,
                     (CASE WHEN payload_json::jsonb->>'settled' = 'true'
                           THEN 1 ELSE 0 END) DESC
        """)
        return (await sess.execute(q, {"symbols": candidate_syms})).all()


async def _fill_from_daily_book_sparkline(
    miss_syms: list["SparklineSymbol"],
    past_result: dict[str, list[float]],
    orig_to_resolved: dict[str, str],
) -> None:
    """Tier 4 fallback: read daily_book sparkline snapshots when stores are cold.

    Mutates past_result in-place. Never raises. Only called in db_only mode.
    """
    if not miss_syms:
        return
    miss_tradingsymbols = [s.tradingsymbol for s in miss_syms]
    miss_syms_set = set(miss_tradingsymbols)
    resolved_to_bare: dict[str, str] = {v: k for k, v in orig_to_resolved.items()}
    candidate_syms = list(_build_sparkline_candidate_syms(miss_tradingsymbols, orig_to_resolved))
    if not candidate_syms:
        return
    try:
        db_rows = await _qt_query_daily_book_sparkline(candidate_syms)
        _qt_apply_db_sparkline_rows(
            db_rows, miss_syms_set, orig_to_resolved, resolved_to_bare, past_result
        )
    except Exception as exc:
        logger.warning(f"sparkline tier4: daily_book fallback failed: {exc}")


async def _build_spark_token_map(
    norm_syms: list["SparklineSymbol"],
) -> dict[str, int]:
    """Step 2: build {tradingsymbol → instrument_token} map and subscribe to ticker.

    CRITICAL: uses subscribe_with_sym so the ticker's _token_to_sym map is
    populated — without it SSE tick payloads carry sym="" and the frontend
    quoteStream filter silently drops every tick.
    Returns an empty dict on any failure (best-effort).
    """
    from backend.brokers.kite_ticker import get_ticker

    token_map: dict[str, int] = {}
    try:
        from backend.brokers.registry import get_sparkline_broker as _sb
        _bk = _sb()
        _full_map = await asyncio.to_thread(_get_today_token_map, _bk)
        for s in norm_syms:
            if s.tradingsymbol in token_map:
                continue
            pref = _exchange_walk_order(s.exchange)
            for _ex in pref:
                tok = _full_map.get((s.tradingsymbol, _ex))
                if tok is not None:
                    token_map[s.tradingsymbol] = tok
                    break
    except Exception as _exc:
        logger.warning(f"sparkline: token lookup failed: {_exc}")

    ticker = get_ticker()
    if token_map:
        ticker.subscribe_with_sym(
            [(tok, sym) for sym, tok in token_map.items()]
        )
    return token_map


def _qt_apply_ltp_val(key: str, val, ltp_map: dict, record_ltp) -> None:
    """Parse one raw broker.ltp() entry and store into *ltp_map*. Swallows bad values."""
    lp = val.get("last_price") if isinstance(val, dict) else val
    try:
        lp_f = float(lp) if lp is not None else 0.0
        ltp_map[key] = lp_f
        if lp_f > 0:
            record_ltp(key.split(":", 1)[-1], lp_f)
    except (TypeError, ValueError):
        pass


async def _fill_ltp_from_broker(
    miss_keys: list[str],
    spark_market_closed: bool,
    ltp_map: dict[str, float],
) -> None:
    """Step 3 Pass 2: broker.ltp() for tick-map misses.

    Mutates ltp_map in-place. Skipped when market is closed.
    """
    from backend.brokers.broker_apis import record_good_ltp as _record_ltp

    if not miss_keys or spark_market_closed:
        if miss_keys and spark_market_closed:
            logger.debug(
                f"sparkline: market closed — skipping broker.ltp() for {len(miss_keys)} misses"
            )
        return
    try:
        from backend.brokers.registry import get_sparkline_broker as _get_sp_broker
        ltp_broker = _get_sp_broker()
        raw_ltp = await asyncio.to_thread(ltp_broker.ltp, miss_keys) or {}
        for key, val in raw_ltp.items():
            _qt_apply_ltp_val(key, val, ltp_map, _record_ltp)
    except Exception as exc:
        logger.warning(f"sparkline: ltp fallback batch failed: {exc}")


async def _resolve_spark_ltps(
    norm_syms: list["SparklineSymbol"],
    spark_market_closed: bool,
) -> dict[str, float]:
    """Steps 2+3 orchestrator: token map → tick_map pass → broker.ltp fallback.

    Returns ``ltp_map`` keyed by ``'EXCHANGE:SYMBOL'`` strings.
    """
    from backend.brokers.kite_ticker import get_ticker
    from backend.brokers.broker_apis import record_good_ltp as _record_ltp

    # Step 2: build token map + subscribe ticker
    token_map = await _build_spark_token_map(norm_syms)

    key_to_token: dict[str, int] = {
        f"{s.exchange}:{s.tradingsymbol}": token_map[s.tradingsymbol]
        for s in norm_syms
        if s.tradingsymbol in token_map
    }
    quote_keys = [f"{s.exchange}:{s.tradingsymbol}" for s in norm_syms]
    ltp_map: dict[str, float] = {}

    # Step 3 Pass 1: tick map (zero Kite quota)
    ticker = get_ticker()
    ticker_hits: list[str] = []
    miss_keys: list[str] = []
    for qk in quote_keys:
        tok = key_to_token.get(qk)
        if tok is not None:
            ltp_val = ticker.get_ltp(tok)
            if ltp_val is not None:
                ltp_map[qk] = ltp_val
                ticker_hits.append(qk)
                _record_ltp(qk.split(":", 1)[-1], ltp_val)
            else:
                miss_keys.append(qk)
        else:
            miss_keys.append(qk)

    if ticker_hits:
        logger.debug(
            f"sparkline: {len(ticker_hits)} LTP(s) from tick_map, "
            f"{len(miss_keys)} fallback to broker.ltp()"
        )

    # Step 3 Pass 2: broker.ltp() for misses
    await _fill_ltp_from_broker(miss_keys, spark_market_closed, ltp_map)
    return ltp_map


def _qt_log_spark_empty(
    sym: str,
    exchange: str,
    reason: str,
    past: list,
    today_bars: list,
    ltp_val: "float | None",
    market_closed: bool,
) -> None:
    """Emit a throttled [SPARK-EMPTY] structured log line (at most once per hour per symbol)."""
    _layer_map = {
        "warm_universe_empty":    "tier1_2_cache",
        "historical_fetch_fail":  "tier3_broker",
        "spark_past_cache_miss":  "tier1_past_cache",
        "spark_today_cache_miss": "tier1_today_cache",
    }
    _layer = _layer_map.get(reason, "unknown")
    _now = _time_mod.monotonic()
    _key = (sym, exchange)
    if _now - _spark_empty_last_log.get(_key, 0.0) >= 3600:
        _spark_empty_last_log[_key] = _now
        logger.info(
            f"[SPARK-EMPTY] symbol={exchange}:{sym} "
            f"reason={reason} cache_layer={_layer} "
            f"past={len(past)} today={len(today_bars)} "
            f"ltp={ltp_val} market_closed={market_closed}"
        )


def _qt_resolve_ltp_val(sym: str, ltp_key: str, ltp_map: dict, spark_market_closed: bool, get_last_ltp) -> "float | None":
    """Return LTP for *sym*: ltp_map entry or LKG cache when closed and map is empty/zero."""
    ltp_val = ltp_map.get(ltp_key)
    if (ltp_val is None or ltp_val == 0) and spark_market_closed:
        _cached_ltp = get_last_ltp(sym, max_age_s=86400.0)
        if _cached_ltp and _cached_ltp > 0:
            return _cached_ltp
    return ltp_val


def _compose_and_dual_write(
    norm_syms: list["SparklineSymbol"],
    past_result: dict[str, list[float]],
    today_result: dict[str, list[float]],
    ltp_map: dict[str, float],
    orig_to_resolved: dict[str, str],
    spark_market_closed: bool,
) -> dict[str, list[float]]:
    """Step 4: compose result series and dual-write original bare names.

    Returns the result dict keyed by tradingsymbol (including aliases).
    Emits throttled [SPARK-EMPTY] structured log when a symbol has no data.
    """
    from backend.brokers.broker_apis import get_last_good_ltp as _get_last_ltp

    result: dict[str, list[float]] = {}
    for sym_obj in norm_syms:
        sym  = sym_obj.tradingsymbol
        past = past_result.get(sym, [])
        today_bars = today_result.get(sym, [])
        ltp_val = _qt_resolve_ltp_val(
            sym, f"{sym_obj.exchange}:{sym}", ltp_map, spark_market_closed, _get_last_ltp
        )
        series, _compose_reason = compose_sparkline_series(
            past=past,
            today_bars=today_bars,
            ltp_val=ltp_val,
            market_closed=spark_market_closed,
        )
        if not series:
            _qt_log_spark_empty(sym, sym_obj.exchange, _compose_reason, past, today_bars, ltp_val, spark_market_closed)
        if series:
            result[sym] = series
            # Dual-write: also store under the original bare watchlist name
            # so the frontend renderer's sparklines[row.tradingsymbol] lookup
            # hits for MCX/CDS symbols whose tradingsymbol on the grid is the
            # bare commodity/currency name, not the resolved front-month contract.
            for bare, resolved_name in orig_to_resolved.items():
                if resolved_name == sym:
                    result[bare] = series
    return result


def _qt_batch_spark_log_db_only() -> None:
    """Emit a rate-limited log line when sparkline is running in db_only mode."""
    global _spark_db_only_last_log
    _now_ts = _time_mod.time()
    if _now_ts - _spark_db_only_last_log >= 60.0:
        _spark_db_only_last_log = _now_ts
        logger.info(
            "sparkline: db_only active — market closed, "
            "serving Tier 1+2 only (no broker calls)"
        )


async def _qt_batch_spark_db_fallback(
    db_only: bool,
    norm_syms: list,
    past_result: dict,
    today_result: dict,
    orig_to_resolved: dict,
) -> None:
    """Run daily_book Tier-4 + self-heal fallbacks when db_only is active."""
    if not db_only:
        return
    miss_syms = [
        s for s in norm_syms
        if not past_result.get(s.tradingsymbol)
        and not today_result.get(s.tradingsymbol)
    ]
    if miss_syms:
        await _fill_from_daily_book_sparkline(miss_syms, past_result, orig_to_resolved)


class SparklineController(Controller):
    path = "/api/quotes"
    guards = [auth_or_demo_guard]

    @post("/sparkline", status_code=200)
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
        yesterday  = today_date - timedelta(days=1)
        from_daily = today_date - timedelta(days=days + 5)  # +5 buffer for weekends/holidays

        # ── Normalise + resolve virtual MCX/CDS roots ─────────────────────
        norm_syms, orig_to_resolved = await _normalize_sparkline_symbols(syms)

        # ── Determine db_only mode ─────────────────────────────────────────
        _mkt_open: bool = await asyncio.to_thread(_any_segment_open)
        db_only: bool = not _mkt_open
        if db_only:
            _qt_batch_spark_log_db_only()

        # ── Step 1: Past daily closes + today's intraday bars (parallel) ──
        past_result, today_result = await _fetch_bars_parallel(
            norm_syms, from_daily, yesterday, today_date, days, db_only,
        )

        # ── Step 1b+c: Tier 4 + self-heal when db_only ───────────────────
        await _qt_batch_spark_db_fallback(
            db_only, norm_syms, past_result, today_result, orig_to_resolved,
        )
        if db_only:
            await _self_heal_empty_bars(
                norm_syms, past_result, today_result,
                from_daily, yesterday, today_date, days,
            )

        # ── Steps 2+3: token map + LTP (tick_map → broker.ltp fallback) ───
        req_exchs_spark = {s.exchange.upper() for s in norm_syms}
        spark_market_closed = _all_exchanges_closed(req_exchs_spark)
        ltp_map = await _resolve_spark_ltps(norm_syms, spark_market_closed)

        sparkline_as_of: Optional[str] = (
            datetime.now(timezone.utc).isoformat(timespec="seconds")
            if spark_market_closed else None
        )

        # ── Step 4: Compose result series + dual-write bare names ─────────
        result = _compose_and_dual_write(
            norm_syms, past_result, today_result,
            ltp_map, orig_to_resolved, spark_market_closed,
        )

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


def _qt_try_start_ticker_deferred(ticker) -> None:
    """Attempt a deferred KiteTicker start by walking the sparkline broker pool.

    Called when the ticker wasn't started on initial startup (Connections may
    not have restored the access_token yet). Silently swallows errors.
    """
    try:
        from backend.brokers.registry import get_sparkline_broker
        from backend.brokers.client import is_cutover_on
        spark_bk = get_sparkline_broker()
        for b in getattr(spark_bk, "_brokers", []):
            api_key: str | None = None
            access_token: str | None = None
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


def _ticker_seed_early(token_map: dict[str, int]) -> None:
    """Seed KiteTicker subscriptions BEFORE the historical-data warm fetches.

    Errors swallowed silently — historical fetches still proceed without WS push.
    """
    if not token_map:
        return
    try:
        from backend.brokers.kite_ticker import get_ticker
        ticker = get_ticker()
        if not ticker.status().get("started"):
            _qt_try_start_ticker_deferred(ticker)
        ticker.subscribe_with_sym([(tok, sym) for sym, tok in token_map.items()])
        logger.info(
            f"sparkline warm: pushed {len(token_map)} token(s) to TickerManager (pre-fetch)"
        )
    except Exception as exc:
        logger.warning(f"sparkline warm: ticker subscribe failed: {exc}")

async def _qt_warm_build_token_map(
    norm: list[tuple[str, str]],
) -> "dict[str, int]":
    """Build {tradingsymbol → token} map for the warm run. Best-effort."""
    token_map: dict[str, int] = {}
    try:
        from backend.brokers.registry import get_sparkline_broker
        broker = get_sparkline_broker()
        _full_map = await asyncio.to_thread(_get_today_token_map, broker)
        for sym_n, exch_n in norm:
            if sym_n in token_map:
                continue
            for ex in _exchange_walk_order(exch_n):
                tok = _full_map.get((sym_n, ex))
                if tok is not None:
                    token_map[sym_n] = tok
                    break
    except Exception as exc:
        logger.warning(f"sparkline warm: instrument lookup failed: {exc}")
    return token_map


async def _qt_warm_run_stores(
    norm: list[tuple[str, str]],
    ohlcv_store,
    intraday_store,
    from_daily: "date",
    yesterday: "date",
    today_date: "date",
) -> "tuple[int, int]":
    """Run daily + intraday store fetches with a rate-limiting semaphore.

    Uses concurrency=2 + 0.4s sleep to stay within the Kite 3 req/s budget.
    Runs daily gather first, then intraday — halves peak broker load.
    Returns (daily_count, intraday_count).
    """
    _warm_sem = asyncio.Semaphore(2)

    async def _fetch_daily(sym: str, exch: str) -> bool:
        async with _warm_sem:
            try:
                return bool(await ohlcv_store.get_or_fetch_daily(
                    sym, exch, from_d=from_daily, to_d=yesterday,
                ))
            except Exception:
                return False
            finally:
                await asyncio.sleep(0.4)

    async def _fetch_intraday(sym: str, exch: str) -> bool:
        async with _warm_sem:
            try:
                return bool(await intraday_store.get_or_fetch_intraday(
                    sym, exch, on_date=today_date, interval="30minute",
                ))
            except Exception:
                return False
            finally:
                await asyncio.sleep(0.4)

    daily_res = await asyncio.gather(*[_fetch_daily(s, e) for s, e in norm])
    intraday_res = await asyncio.gather(*[_fetch_intraday(s, e) for s, e in norm])
    return sum(1 for ok in daily_res if ok), sum(1 for ok in intraday_res if ok)


async def warm_sparkline_cache(symbols: list[tuple[str, str]], days: int = 5) -> int:
    """Pre-warm ohlcv_store + intraday_store for the given (tradingsymbol, exchange) pairs.

    Called by _task_sparkline_warm at market open and app startup.
    Returns the count of symbols where at least daily data was loaded.
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

    token_map = await _qt_warm_build_token_map(norm)
    if token_map:
        _ticker_seed_early(token_map)

    cached_count, intraday_count = await _qt_warm_run_stores(
        norm, _ohlcv_store, _intraday_store, from_daily, yesterday, today_date,
    )

    if intraday_count:
        logger.info(f"sparkline warm: intraday warmed {intraday_count}/{len(norm)} symbols")

    _spark_warm_symbols = cached_count
    _spark_warm_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    logger.info(f"sparkline warm: daily warmed {cached_count}/{len(norm)} symbols for {today}")
    return cached_count
