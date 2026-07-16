"""
Pure helper functions extracted from options.py to reduce cyclomatic complexity
of the three top hotspots: historical (cc=61), chain_snapshot (cc=52),
_resolve_spot (cc=35).

Extraction seams
----------------
_resolve_spot helpers
  _resolve_spot_from_sim        — step 2: active SimDriver check
  _resolve_spot_ticker          — step 3: NSE spot ticker (non-commodity)
  _resolve_commodity_spot       — step 4a+4b: MCX futures lookup + walk-forward

historical helpers
  _historical_ohlcv_store       — Tier-1/2/3 daily store path
  _historical_intraday_store    — Tier-1/2/3 intraday store path
  _historical_closed_guard      — closed-hours guard for intraday intervals
  _historical_broker_loop       — multi-account broker fan-out

chain_snapshot helpers
  _chain_snapshot_instruments   — instruments fetch + window computation
  _chain_snapshot_batch_quote   — batch broker quote for the window
  _chain_snapshot_compute_rows  — per-strike IV + greeks computation

All helpers are pure/async functions (no ``self``).  The route handlers in
options.py remain the thin orchestrators that own request validation,
cache I/O, and response construction.
"""

from __future__ import annotations

import asyncio
import threading as _threading
from datetime import date, datetime, timedelta
from typing import Optional, TYPE_CHECKING

from litestar.exceptions import HTTPException

from backend.api.algo.derivatives import (
    DEFAULT_IV,
    DEFAULT_RISK_FREE,
    days_to_expiry,
    futures_symbol_for_expiry,
    greeks,
    implied_vol,
    is_mcx_underlying,
    lookup_future_for_option,
    lookup_mcx_front_month_future,
    lookup_mcx_future_for_expiry,
    option_quote_key,
    underlying_ltp_key,
)
from backend.shared.helpers.ramboq_logger import get_logger

if TYPE_CHECKING:
    from backend.api.routes.options import (
        ChainSnapshotLeg,
        ChainSnapshotRow,
        HistoricalBar,
        HistoricalResponse,
    )

logger = get_logger(__name__)

_DEMAND_FILL_ACTIVE: set[tuple[str, str]] = set()


# ---------------------------------------------------------------------------
# _resolve_spot sub-helpers
# ---------------------------------------------------------------------------

async def _resolve_spot_from_sim(underlying: str) -> Optional[float]:
    """Return the sim-driver LTP for *underlying* if a SimDriver is active
    and the underlying is tracked, else None.

    Swallows all import/attribute errors so a stale or missing driver never
    breaks the live analytics path.
    """
    try:
        from backend.api.algo.sim.driver import get_driver
        drv = get_driver()
        if drv.active and underlying in drv._underlyings:
            return float(drv._underlyings[underlying])
    except Exception:
        pass
    return None


async def _resolve_spot_ticker(
    underlying: str,
    broker: object,
    spot_cache_put: object,
    ltp_from_quote: object,
    prev_close_from_quote: object,
) -> Optional[tuple[float, str, Optional[float], None]]:
    """Attempt an NSE spot-ticker quote for a non-commodity underlying.

    Returns ``(price, source, prev_close, None)`` on success, or None
    when the quote fails or returns zero.
    """
    key = underlying_ltp_key(underlying)
    try:
        resp = await asyncio.to_thread(broker.quote, [key]) or {}  # type: ignore[attr-defined]
        quote_dict = resp.get(key) or {}
        px, src = ltp_from_quote(quote_dict)  # type: ignore[operator]
        if px is not None:
            prev = prev_close_from_quote(quote_dict)  # type: ignore[operator]
            spot_cache_put(underlying, px, src, prev, None)  # type: ignore[operator]
            return (px, src, prev, None)
    except Exception as exc:
        logger.warning("options spot quote for %s failed: %s", underlying, exc)
    return None


async def _commodity_spot_4a(
    underlying: str,
    resolved_sym: str,
    broker: object,
    spot_cache_put: object,
    ltp_from_quote: object,
    prev_close_from_quote: object,
) -> Optional[tuple]:
    """Step 4a: quote MCX:{resolved_sym}; return result tuple on success or None."""
    full_key = f"MCX:{resolved_sym}"
    try:
        resp = await asyncio.to_thread(broker.quote, [full_key]) or {}  # type: ignore[attr-defined]
        quote_dict = resp.get(full_key) or {}
        px, _src = ltp_from_quote(quote_dict)  # type: ignore[operator]
        if px is not None:
            prev = prev_close_from_quote(quote_dict)  # type: ignore[operator]
            spot_cache_put(underlying, px, "futures", prev, resolved_sym)  # type: ignore[operator]
            return (px, "futures", prev, resolved_sym, resolved_sym)
    except Exception as exc:
        logger.warning("options MCX spot for %s (%s) failed: %s", underlying, full_key, exc)
    return None


async def _commodity_spot_4b(
    underlying: str,
    resolved_sym: Optional[str],
    expiry_hint: date,
    broker: object,
    spot_cache_put: object,
    ltp_from_quote: object,
    prev_close_from_quote: object,
) -> Optional[tuple]:
    """Step 4b: walk forward up to 3 months trying constructed futures symbols."""
    exchanges = ("MCX", "NFO") if is_mcx_underlying(underlying) else ("NFO", "MCX")
    cursor = expiry_hint
    for _step in range(3):
        fut_sym = futures_symbol_for_expiry(underlying, cursor)
        for ex in exchanges:
            full_key = f"{ex}:{fut_sym}"
            try:
                resp = await asyncio.to_thread(broker.quote, [full_key]) or {}  # type: ignore[attr-defined]
                quote_dict = resp.get(full_key) or {}
                px, _src = ltp_from_quote(quote_dict)  # type: ignore[operator]
                if px is not None:
                    prev = prev_close_from_quote(quote_dict)  # type: ignore[operator]
                    anchor = fut_sym if is_mcx_underlying(underlying) else None
                    spot_cache_put(underlying, px, "futures", prev, anchor)  # type: ignore[operator]
                    return (px, "futures", prev, resolved_sym, anchor)
            except Exception as exc:
                logger.warning(
                    "options futures-spot quote for %s (%s) failed: %s",
                    underlying, full_key, exc,
                )
        cursor = (cursor.replace(day=1) + timedelta(days=32)).replace(day=1)
    return None


async def _resolve_commodity_spot(
    underlying: str,
    expiry_hint: Optional[date],
    option_symbol: Optional[str],
    broker: object,
    spot_cache_put: object,
    ltp_from_quote: object,
    prev_close_from_quote: object,
) -> tuple[Optional[float], str, Optional[float], Optional[str], Optional[str]]:
    """Look up the MCX commodity spot through two sub-steps (4a + 4b).

    Returns ``(price_or_None, source, prev_close, resolved_sym, anchor)``.
    """
    resolved_sym: Optional[str] = None

    # 4a. Instruments-cache lookup.
    if option_symbol:
        resolved_sym = await lookup_future_for_option(option_symbol)
    if not resolved_sym and expiry_hint is not None:
        resolved_sym = await lookup_mcx_future_for_expiry(underlying, expiry_hint)
    if not resolved_sym:
        resolved_sym = await lookup_mcx_front_month_future(underlying)

    if resolved_sym:
        hit = await _commodity_spot_4a(
            underlying, resolved_sym, broker, spot_cache_put,
            ltp_from_quote, prev_close_from_quote,
        )
        if hit is not None:
            return hit  # type: ignore[return-value]

    # 4b. Walk-forward.
    if expiry_hint is not None:
        hit = await _commodity_spot_4b(
            underlying, resolved_sym, expiry_hint, broker, spot_cache_put,
            ltp_from_quote, prev_close_from_quote,
        )
        if hit is not None:
            return hit  # type: ignore[return-value]

    return (None, "none", None, resolved_sym, None)


async def _ohlcv_demand_fill(sym: str, exch: str, days: int) -> None:
    """Background demand backfill — integrates partial-chart signal into broker refresh cycle.

    Fires as an asyncio task when the chart route returns partial data.
    Uses backfill_ohlcv_daily (same pipeline as scheduled refresh) so the
    DB is updated for the next frontend retry.  Debounced per (sym, exch).
    """
    key = (sym, exch)
    if key in _DEMAND_FILL_ACTIVE:
        return
    _DEMAND_FILL_ACTIVE.add(key)
    try:
        from backend.api.persistence.backfill import backfill_ohlcv_daily
        await backfill_ohlcv_daily([(sym, exch)], target_days=days + 5, max_concurrent=1)
        logger.info("ohlcv demand fill complete: %s/%s target=%d", sym, exch, days + 5)
    except Exception as exc:
        logger.warning("ohlcv demand fill %s/%s: %s", sym, exch, exc)
    finally:
        _DEMAND_FILL_ACTIVE.discard(key)


# ---------------------------------------------------------------------------
# historical sub-helpers
# ---------------------------------------------------------------------------

async def _ohlcv_self_heal_if_needed(
    sym: str,
    resolved_exch: str,
    from_d: object,
    to_d: object,
    store_bars: list,
    days: int,
    self_heal_threshold: float,
    self_heal_log_once: object,
    get_or_fetch_daily: object,
) -> tuple[list, bool]:
    """Run bypass-cache retry when store_bars is below self-heal threshold.

    Returns ``(bars, heal_attempted)`` — unchanged when threshold not exceeded
    or no historical brokers are available.
    """
    if len(store_bars) >= self_heal_threshold * days:  # type: ignore[operator]
        return store_bars, False
    from backend.brokers.registry import get_historical_brokers as _ghb
    if not bool(_ghb()):
        return store_bars, False
    self_heal_log_once(sym, resolved_exch, len(store_bars), days)  # type: ignore[operator]
    healed = await get_or_fetch_daily(sym, resolved_exch, from_d, to_d, bypass_cache=True)
    return healed, True


async def _historical_ohlcv_store(
    sym: str,
    exchange: str,
    days: int,
    hist_cache_put: object,
    ohlcv_trace_enabled: object,
    self_heal_log_once: object,
    self_heal_threshold: float,
    cache_ttl_ok: int,
    cache_ttl_empty: int,
    HistoricalBarCls: type,
    HistoricalResponseCls: type,
) -> Optional[object]:
    """Tier-1/2/3 daily OHLCV store path.

    Returns a ``HistoricalResponse`` when the store (or self-heal retry)
    yields bars, or ``None`` to fall through to the broker loop.  The
    cache key must be written by the caller BEFORE calling this function
    so that the cache check happens in the handler.
    """
    from datetime import date as _date, timedelta as _td
    from backend.api.persistence.ohlcv_store import get_or_fetch_daily
    resolved_exch = (exchange or "NFO").upper()
    to_d = _date.today() - _td(days=1)
    from_d = to_d - _td(days=days + 5)

    try:
        store_bars = await get_or_fetch_daily(sym, resolved_exch, from_d, to_d)
        store_bars, _heal_attempted = await _ohlcv_self_heal_if_needed(
            sym, resolved_exch, from_d, to_d, store_bars, days,
            self_heal_threshold, self_heal_log_once, get_or_fetch_daily,
        )

        if store_bars:
            bars = [
                HistoricalBarCls(  # type: ignore[operator]
                    ts=b["date"],
                    open=float(b["open"]),
                    high=float(b["high"]),
                    low=float(b["low"]),
                    close=float(b["close"]),
                    volume=int(b["volume"]),
                )
                for b in store_bars
            ]
            _still_partial = len(store_bars) < int(self_heal_threshold * days)  # type: ignore[operator]
            result = HistoricalResponseCls(  # type: ignore[operator]
                symbol=sym, instrument_token=None, interval="day",
                bars=bars, partial=_still_partial,
            )
            cache_key_local = (sym, resolved_exch, days, "day")
            hist_cache_put(  # type: ignore[operator]
                cache_key_local, result,
                cache_ttl_empty if _still_partial else cache_ttl_ok,
            )
            if _still_partial:
                asyncio.create_task(_ohlcv_demand_fill(sym, resolved_exch, days))
            if ohlcv_trace_enabled():  # type: ignore[operator]
                logger.info(
                    "[ohlcv-route] symbol=%s exch=%s from=%s to=%s bars=%d "
                    "source=ohlcv_store heal=%s",
                    sym, resolved_exch, from_d, to_d, len(bars), _heal_attempted,
                )
            return result

        if ohlcv_trace_enabled():  # type: ignore[operator]
            logger.info(
                "[ohlcv-route] symbol=%s exch=%s from=%s to=%s bars=0 "
                "source=ohlcv_store — falling through to broker loop",
                sym, resolved_exch, from_d, to_d,
            )
    except Exception as exc:
        logger.warning(
            "options historical: ohlcv_store failed for %s/%s: %s "
            "— falling through to broker",
            sym, resolved_exch, exc,
        )
    return None


async def _intraday_day_range(
    sym: str,
    resolved_exch: str,
    from_d: object,
    to_d: object,
    interval: str,
    HistoricalBarCls: type,
    bypass_cache: bool = False,
) -> list:
    """Sweep from_d..to_d (inclusive) collecting intraday bars."""
    from datetime import timedelta as _td
    from backend.api.persistence.intraday_store import get_or_fetch_intraday

    def _bar(b: dict) -> object:
        return HistoricalBarCls(  # type: ignore[operator]
            ts=str(b["bar_ts"]),
            open=float(b["open"]),
            high=float(b["high"]),
            low=float(b["low"]),
            close=float(b["close"]),
            volume=int(b.get("volume", 0)),
        )

    merged: list = []
    cur = from_d
    while cur <= to_d:
        day_bars = await get_or_fetch_intraday(
            sym, resolved_exch, cur, interval=interval, bypass_cache=bypass_cache,
        )
        merged.extend(_bar(b) for b in (day_bars or []))
        cur += _td(days=1)
    return merged


async def _historical_intraday_store(
    sym: str,
    exchange: str,
    days: int,
    interval: str,
    hist_cache_put: object,
    self_heal_log_once: object,
    cache_ttl_ok: int,
    HistoricalBarCls: type,
    HistoricalResponseCls: type,
) -> Optional[object]:
    """Tier-1/2/3 intraday store path for 5/15/30/60-minute intervals.

    Returns a ``HistoricalResponse`` on success, or ``None`` to fall
    through to the broker loop.
    """
    from datetime import date as _date, timedelta as _td
    resolved_exch = (exchange or "NFO").upper()
    to_d = _date.today()
    from_d = to_d - _td(days=days)

    try:
        merged = await _intraday_day_range(
            sym, resolved_exch, from_d, to_d, interval, HistoricalBarCls,
        )

        if not merged:
            from backend.brokers.registry import get_historical_brokers as _ghb
            if _ghb():
                self_heal_log_once(sym, resolved_exch, 0, days)  # type: ignore[operator]
                merged = await _intraday_day_range(
                    sym, resolved_exch, from_d, to_d, interval, HistoricalBarCls,
                    bypass_cache=True,
                )

        if merged:
            result = HistoricalResponseCls(  # type: ignore[operator]
                symbol=sym, instrument_token=None, interval=interval, bars=merged,
            )
            cache_key_local = (sym, resolved_exch, days, interval)
            hist_cache_put(cache_key_local, result, cache_ttl_ok)  # type: ignore[operator]
            return result
    except Exception as exc:
        logger.warning(
            "options historical: intraday_store failed for %s/%s/%s: %s "
            "— falling through to broker",
            sym, resolved_exch, interval, exc,
        )
    return None


def _historical_closed_guard(
    sym: str,
    interval: str,
    hist_cache_put: object,
    cache_key: tuple,
    cache_ttl_empty: int,
    HistoricalResponseCls: type,
) -> Optional[object]:
    """Return an empty ``HistoricalResponse`` when markets are closed and the
    interval is intraday, to avoid wasting broker quota.

    Returns the response object when the guard fires, or ``None`` (markets
    open or interval is daily) so the caller proceeds to the broker loop.
    """
    if interval not in ("5minute", "15minute", "30minute", "60minute"):
        return None
    try:
        from backend.shared.helpers.date_time_utils import (
            is_any_segment_open, timestamp_indian,
        )
        if not is_any_segment_open(timestamp_indian()):
            logger.debug(
                "options historical: market closed — skipping broker "
                "for intraday %s/%s",
                sym, interval,
            )
            result = HistoricalResponseCls(  # type: ignore[operator]
                symbol=sym, instrument_token=None, interval=interval, bars=[],
            )
            hist_cache_put(cache_key, result, cache_ttl_empty)  # type: ignore[operator]
            return result
    except Exception:
        pass  # fail-open: proceed to broker loop
    return None


async def _resolve_token_for_broker(
    broker: object,
    sym: str,
    exchange_arms: tuple[str, ...],
    instruments_cache_get: object,
    instruments_cache_put: object,
) -> Optional[int]:
    """Walk ``exchange_arms`` until the trading-symbol is found in the
    instruments cache (fetching from broker when stale).  Returns the
    integer token or ``None`` when the symbol isn't listed on any arm.
    """
    for ex in exchange_arms:
        token_map = instruments_cache_get(broker.account, ex)  # type: ignore[operator]
        if token_map is None:
            insts = await asyncio.to_thread(broker.instruments, ex) or []
            token_map = {}
            for inst in insts:
                ts = str(inst.get("tradingsymbol") or "").upper()
                tk = inst.get("instrument_token")
                if ts and tk:
                    token_map[ts] = int(tk)
            instruments_cache_put(broker.account, ex, token_map)  # type: ignore[operator]
        tk = token_map.get(sym)
        if tk is not None:
            return int(tk)
    return None


def _raw_to_hist_bars(raw: list, HistoricalBarCls: type) -> list:
    """Convert raw broker dicts to HistoricalBar instances."""
    return [
        HistoricalBarCls(  # type: ignore[operator]
            ts=str(b["date"]) if not isinstance(b.get("date"), datetime)
                              else b["date"].isoformat(),
            open=float(b.get("open") or 0),
            high=float(b.get("high") or 0),
            low=float(b.get("low") or 0),
            close=float(b.get("close") or 0),
            volume=int(b.get("volume") or 0),
        )
        for b in raw
    ]


def _build_and_cache_hist_result(
    sym: str,
    interval: str,
    token: Optional[int],
    raw: list,
    cache_key: tuple,
    cache_ttl_ok: int,
    cache_ttl_empty: int,
    hist_cache_put: object,
    record_first_cold_empty: object,
    HistoricalBarCls: type,
    HistoricalResponseCls: type,
    ohlcv_trace_enabled: object,
    exchange: str,
    days: int,
    broker_account: str,
) -> object:
    """Convert raw broker rows → HistoricalBar list, build the
    HistoricalResponse, write it to the cache, and return it.
    Partial=True when no bars came back.
    """
    bars = _raw_to_hist_bars(raw, HistoricalBarCls)
    result = HistoricalResponseCls(  # type: ignore[operator]
        symbol=sym, instrument_token=token, interval=interval,
        bars=bars, partial=not bool(bars),
    )
    hist_cache_put(  # type: ignore[operator]
        cache_key, result,
        cache_ttl_ok if bars else cache_ttl_empty,
    )
    if not bars:
        record_first_cold_empty(sym)  # type: ignore[operator]
    if ohlcv_trace_enabled():  # type: ignore[operator]
        logger.info(
            "[ohlcv-route] symbol=%s exchange=%s days=%d interval=%s "
            "bars=%d source=broker_loop broker=%s",
            sym, exchange or "auto", days, interval, len(bars), broker_account,
        )
    return result


def _make_empty_hist_result(
    sym: str,
    interval: str,
    cache_key: tuple,
    cache_ttl_empty: int,
    hist_cache_put: object,
    record_first_cold_empty: object,
    HistoricalResponseCls: type,
) -> object:
    """Build a zero-bar partial result, cache it, and return it."""
    result = HistoricalResponseCls(  # type: ignore[operator]
        symbol=sym, instrument_token=None, interval=interval,
        bars=[], partial=True,
    )
    hist_cache_put(cache_key, result, cache_ttl_empty)  # type: ignore[operator]
    record_first_cold_empty(sym)  # type: ignore[operator]
    return result


async def _try_one_broker(
    broker: object,
    sym: str,
    exchange_arms: tuple,
    interval: str,
    from_d: object,
    to_d: object,
    cache_key: tuple,
    cache_ttl_ok: int,
    cache_ttl_empty: int,
    hist_cache_put: object,
    record_first_cold_empty: object,
    HistoricalBarCls: type,
    HistoricalResponseCls: type,
    ohlcv_trace_enabled: object,
    exchange: str,
    days: int,
    instruments_cache_get: object,
    instruments_cache_put: object,
    mark_rate_limited: object,
) -> Optional[object]:
    """Attempt historical fetch from a single broker; return result or None on failure."""
    broker_key = f"{broker.broker_id}/{broker.account}"  # type: ignore[attr-defined]
    try:
        token = await _resolve_token_for_broker(
            broker, sym, exchange_arms, instruments_cache_get, instruments_cache_put,
        )
        if not token:
            return None
        raw = await asyncio.to_thread(
            broker.historical_data, token, from_d, to_d, interval,  # type: ignore[attr-defined]
        ) or []
    except Exception as exc:
        msg = str(exc).lower()
        if "too many requests" in msg:
            mark_rate_limited(broker_key)  # type: ignore[operator]
            logger.warning(
                "options historical: %s rate-limited, falling through to next eligible account",
                broker.account,  # type: ignore[attr-defined]
            )
        else:
            logger.warning(
                "options historical: %s error for %s: %s",
                broker.account, sym, str(exc)[:160],  # type: ignore[attr-defined]
            )
        return None
    return _build_and_cache_hist_result(
        sym, interval, token, raw,
        cache_key, cache_ttl_ok, cache_ttl_empty,
        hist_cache_put, record_first_cold_empty,
        HistoricalBarCls, HistoricalResponseCls,
        ohlcv_trace_enabled, exchange, days, broker.account,  # type: ignore[attr-defined]
    )


async def _historical_broker_loop(
    sym: str,
    exchange: str,
    days: int,
    interval: str,
    hist_cache_put: object,
    ohlcv_trace_enabled: object,
    record_first_cold_empty: object,
    cache_key: tuple,
    cache_ttl_ok: int,
    cache_ttl_empty: int,
    HistoricalBarCls: type,
    HistoricalResponseCls: type,
    instruments_cache_get: object,
    instruments_cache_put: object,
) -> object:
    """Multi-account broker fan-out for historical bars.

    Iterates `get_historical_brokers()`, resolves the instrument token
    via the per-(account, exchange) instruments cache, and fetches
    `broker.historical_data`.  Stops at the first successful broker and
    caches the result.  Returns a ``HistoricalResponse`` (partial=True
    when all brokers failed).
    """
    from backend.brokers.registry import (
        get_historical_brokers, _mark_rate_limited,
    )

    exchange_arms: tuple[str, ...] = (
        (exchange,) if exchange else ("NFO", "BFO", "NSE", "BSE", "MCX", "CDS")
    )

    brokers = get_historical_brokers()
    if not brokers:
        logger.warning(
            "options historical: no eligible brokers for %s "
            "(all historical_data_enabled=False or in rate-limit cool-off)",
            sym,
        )
        return _make_empty_hist_result(
            sym, interval, cache_key, cache_ttl_empty,
            hist_cache_put, record_first_cold_empty, HistoricalResponseCls,
        )

    to_d = datetime.now()
    from_d = to_d - timedelta(days=days)

    for broker in brokers:
        result = await _try_one_broker(
            broker, sym, exchange_arms, interval, from_d, to_d,
            cache_key, cache_ttl_ok, cache_ttl_empty,
            hist_cache_put, record_first_cold_empty,
            HistoricalBarCls, HistoricalResponseCls,
            ohlcv_trace_enabled, exchange, days,
            instruments_cache_get, instruments_cache_put,
            _mark_rate_limited,
        )
        if result is not None:
            return result

    # All brokers tried and none succeeded.
    _tried = exchange if exchange else "NFO/BFO/NSE/BSE/MCX/CDS"
    logger.info(
        "options historical: '%s' not found or all brokers failed "
        "(exchanges=%s, brokers tried=%s)",
        sym, _tried, [b.account for b in brokers],
    )
    result = _make_empty_hist_result(
        sym, interval, cache_key, cache_ttl_empty,
        hist_cache_put, record_first_cold_empty, HistoricalResponseCls,
    )
    if ohlcv_trace_enabled():  # type: ignore[operator]
        logger.info(
            "[ohlcv-route] symbol=%s exchange=%s days=%d interval=%s "
            "bars=0 source=broker_loop status=all_brokers_failed",
            sym, exchange or "auto", days, interval,
        )
    return result


# ---------------------------------------------------------------------------
# chain_snapshot sub-helpers
# ---------------------------------------------------------------------------

async def _chain_snapshot_instruments(
    und: str,
    exp: str,
    spot: float,
    atm_window: int,
) -> tuple[dict, float, list]:
    """Fetch the instruments cache, filter to (underlying, expiry) contracts,
    compute the ATM window, and return ``(sym_by_strike, atm_strike, window_strikes)``.

    Returns an empty dict for ``sym_by_strike`` when no contracts are found.
    Raises ``HTTPException(502)`` on instruments-cache failure.
    """
    from backend.api.cache import get_or_fetch
    from backend.api.routes.instruments import _fetch_instruments
    try:
        inst_resp = await get_or_fetch(
            "instruments", _fetch_instruments, ttl_seconds=86400,
        )
    except Exception as exc:
        logger.warning("chain-snapshot instruments fetch failed: %s", exc)
        raise HTTPException(status_code=502, detail="instruments cache unavailable")

    sym_by_strike: dict[float, dict[str, str]] = {}
    for inst in inst_resp.items:
        if (inst.u or "").upper() != und:
            continue
        if inst.x != exp:
            continue
        if inst.t not in ("CE", "PE"):
            continue
        if inst.k is None:
            continue
        sym_by_strike.setdefault(float(inst.k), {"CE": "", "PE": ""})[inst.t] = inst.s

    if not sym_by_strike:
        return sym_by_strike, 0.0, []

    all_strikes = sorted(sym_by_strike.keys())
    atm_idx = min(range(len(all_strikes)), key=lambda i: abs(all_strikes[i] - spot))
    atm_strike = all_strikes[atm_idx]
    lo = max(0, atm_idx - atm_window)
    hi = min(len(all_strikes), atm_idx + atm_window + 1)
    window_strikes = all_strikes[lo:hi]
    return sym_by_strike, atm_strike, window_strikes


async def _chain_snapshot_batch_quote(
    und: str,
    exp: str,
    sym_by_strike: dict,
    window_strikes: list,
) -> tuple[dict, dict]:
    """Build the batch quote keys and fire a single ``broker.quote()`` call.

    Returns ``(quote_resp, key_meta)`` where *key_meta* maps each quote key
    to ``(strike, side)``.
    """
    from backend.brokers.registry import get_market_data_broker
    keys: list[str] = []
    key_meta: dict[str, tuple[float, str]] = {}
    for strike in window_strikes:
        for side, sym in sym_by_strike[strike].items():
            if not sym:
                continue
            qk = option_quote_key(sym)
            keys.append(qk)
            key_meta[qk] = (strike, side)

    quote_resp: dict = {}
    if keys:
        try:
            quote_resp = await asyncio.to_thread(
                get_market_data_broker().quote, keys,
            ) or {}
        except Exception as exc:
            logger.warning(
                "chain-snapshot quote() failed for %s/%s: %s", und, exp, exc,
            )
    return quote_resp, key_meta


def _chain_snapshot_best_depth(book: list) -> Optional[float]:
    """Top-of-book price from a ``depth.buy`` / ``depth.sell`` list.
    Returns None when every level is empty or zero."""
    for level in (book or []):
        p = level.get("price")
        if p not in (None, 0, 0.0):
            return float(p)
    return None


def _chain_snapshot_iv_greeks(
    ltp: float,
    spot: float,
    strike: float,
    T_yrs: float,
    side: str,
) -> tuple[Optional[float], Optional[dict]]:
    """Compute (iv, greeks_dict) from market LTP via bisection + BS.
    Returns (None, None) when ltp is absent or T_yrs is zero.
    """
    if not (ltp and ltp > 0 and T_yrs > 0):
        return None, None
    try:
        iv: Optional[float] = implied_vol(ltp, spot, strike, T_yrs, DEFAULT_RISK_FREE, side)
    except Exception:
        iv = None
    sigma_eff = iv if iv else DEFAULT_IV
    try:
        g: Optional[dict] = greeks(spot, strike, T_yrs, DEFAULT_RISK_FREE, sigma_eff, side)
    except Exception:
        g = None
    return iv, g


def _chain_snapshot_compute_leg(
    sym: str,
    quote_resp: dict,
    spot: float,
    strike: float,
    T_yrs: float,
    side: str,
    ChainSnapshotLegCls: type,
) -> object:
    """Compute one ChainSnapshotLeg (CE or PE) for a given strike+side."""
    qk = option_quote_key(sym) if sym else None
    q = (quote_resp.get(qk) if qk else None) or {}
    depth = q.get("depth") or {}
    ltp = q.get("last_price") or None
    bid = _chain_snapshot_best_depth(depth.get("buy"))
    ask = _chain_snapshot_best_depth(depth.get("sell"))
    iv, g = _chain_snapshot_iv_greeks(ltp, spot, strike, T_yrs, side)
    gd = g or {}
    return ChainSnapshotLegCls(  # type: ignore[operator]
        ltp=ltp, bid=bid, ask=ask, iv=iv,
        delta=gd.get("delta"),
        gamma=gd.get("gamma"),
        theta=gd.get("theta"),
        vega=gd.get("vega"),
        rho=gd.get("rho"),
    )


def _chain_snapshot_compute_rows(
    sym_by_strike: dict,
    window_strikes: list,
    quote_resp: dict,
    spot: float,
    T_yrs: float,
    ChainSnapshotLegCls: type,
    ChainSnapshotRowCls: type,
) -> list:
    """Compute per-strike ChainSnapshotRow objects with IV + Greeks.

    For each strike in *window_strikes*, computes IV via bisection and
    per-share Greeks using Black-Scholes.  Gracefully handles missing
    LTPs and zero T_yrs.
    """
    rows = []
    for strike in window_strikes:
        sides: dict[str, object] = {
            side: _chain_snapshot_compute_leg(
                sym_by_strike[strike].get(side) or "",
                quote_resp, spot, float(strike), T_yrs, side, ChainSnapshotLegCls,
            )
            for side in ("CE", "PE")
        }
        rows.append(ChainSnapshotRowCls(  # type: ignore[operator]
            k=float(strike),
            atm_distance=float(strike) - spot,
            ce=sides["CE"],
            pe=sides["PE"],
        ))
    return rows
