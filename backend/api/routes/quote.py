"""
Market quote endpoint — returns LTP + tick-size for a single instrument.
Used by the frontend command bar to suggest LIMIT prices around current price.

GET  /api/quote/?exchange=NSE&tradingsymbol=RELIANCE  → { ltp, tick_size }
POST /api/quotes/sparkline                            → { data, refreshed_at }
GET  /api/quotes/stream                               → SSE LTP tick stream
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
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
# Split into two tiers:
#
#   _spark_past_cache  — keyed (tradingsymbol, exchange, days, ist_date_str)
#                        stores the PAST (days-1) daily closes only.
#                        Populated by historical_data calls (3 req/sec budget).
#                        Only refreshed on a cache miss or date rollover.
#
#   LTP append        — today's running price fetched live via broker.ltp()
#                        (10 req/sec quote budget) on every response.
#                        Never stored; composed into the response on the fly.
#
# The split eliminates the cold-start cost on the critical path: after market
# open the background warm task pre-fills _spark_past_cache for all watchlist
# + held symbols so the operator's first Pulse load is free of historical_data
# calls. Subsequent sparkline requests: past from cache + one batched ltp() —
# no historical_data hit at all until midnight IST date rollover.
#
# _spark_warm_state tracks when the last warm completed (for /api/admin/health).

_spark_past_cache: dict[tuple, list[float]] = {}
# Today's intraday closes per symbol, keyed (sym, exch, today_ist).
# Stored separately from `_spark_past_cache` because:
#   - TTL is short (5 min, vs daily for past closes)
#   - Refresh cadence is independent (every batch_sparkline call checks
#     whether today's cache is older than `_TODAY_TTL_S` and re-fetches
#     when stale; past closes are static once cached for the day).
# Value shape: (epoch_seconds_when_cached, list[float] of intraday closes).
# Combined with past closes downstream so the sparkline shows
# "4 daily closes → N intraday 30-min closes for today" — operator
# sees today's actual path instead of a single end-of-line dot.
_spark_today_cache: dict[tuple, tuple[float, list[float]]] = {}
_TODAY_TTL_S = 300   # 5 min — refresh today's intraday at this cadence
_TODAY_INTERVAL = "30minute"
# Track the last-attempt epoch for past-cache entries that came back
# incomplete (`len(closes) < days - 1`). Used by `_past_cache_is_complete`
# to allow a re-fetch every `_INCOMPLETE_RETRY_S` seconds without
# hammering Kite on genuinely-newly-listed symbols (which will always
# return fewer bars than the requested window). Same lock as the data
# caches above.
_spark_past_attempt: dict[tuple, float] = {}
_INCOMPLETE_RETRY_S = 300   # 5 min — re-attempt incomplete past fetches at this cadence
_spark_lock       = threading.Lock()

# Warm-state for health endpoint.
_spark_warm_symbols: int = 0
_spark_warm_at: Optional[str] = None   # ISO-8601 UTC string

# ── On-disk persistence ──────────────────────────────────────────────────────
# Survives process redeployment so the operator's first /pulse load after a
# deploy reads from a hot cache instead of waiting ~30 s for the startup warm
# (or paying ~0.3 s per symbol for lazy fetches). File lives alongside the
# broker token caches in .log/; same fcntl-based cross-process lock so prod +
# dev sharing the path don't trample each other.
#
# Shape on disk:
#   {
#     "ist_date": "2026-06-12",
#     "past": {"<sym>|<exch>|<days>": [closes...]},
#     "today": {"<sym>|<exch>": [<cached_at_epoch>, [closes...]]},
#     "past_attempt": {"<sym>|<exch>|<days>": <epoch>},
#   }
# `ist_date` is checked on load — entries are dropped wholesale if the file
# is from a prior IST date (matches the in-memory `_evict_stale` semantics).

_PERSIST_PATH = Path(__file__).resolve().parents[3] / ".log" / "sparkline_cache.json"
_persist_lock = threading.Lock()
_last_save_at: float = 0.0
_SAVE_THROTTLE_S = 5.0   # at most one disk write every 5 s — bounds I/O
# Content hash of the last successfully-persisted payload. Set by
# `_save_caches_to_disk` after every write attempt and compared on the
# next call — when the past-cache content hasn't changed (operator
# idle, sparkline data already cached, request handlers reading not
# writing), we skip the write entirely. Eliminates the steady-state
# "rewrite the same 100-symbol payload every 5 s" pattern.
_last_save_hash: str = ""


def _persist_file_lock():
    """Cross-process exclusive lock around the sparkline cache file —
    same fcntl pattern the broker token caches use. The path resolves
    per-deployment (prod under /opt/ramboq/.log/, dev under
    /opt/ramboq_dev/.log/) so prod + dev write separate files; the
    lock guards against multiple workers OF THE SAME deployment
    racing each other (Litestar runs uvicorn --workers 1 in prod, so
    this is defence-in-depth more than strictly necessary)."""
    import contextlib
    @contextlib.contextmanager
    def _ctx():
        lock_path = _PERSIST_PATH.with_suffix(".lock")
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        fp = None
        try:
            fp = open(lock_path, "a+")
            fcntl.flock(fp.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            if fp is not None:
                try:
                    fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
                fp.close()
    return _ctx()


def _key_to_str(key: tuple) -> str:
    """Serialise a cache key tuple as a delimiter-joined string. JSON
    object keys must be strings; `|` doesn't appear in any
    tradingsymbol or exchange code we use, so a simple join survives
    parse round-trip without collisions."""
    return "|".join(str(p) for p in key)


def _save_caches_to_disk(force: bool = False) -> None:
    """Persist sparkline caches to `_PERSIST_PATH`. Atomic via
    write-tmp-then-rename so a crashed write never leaves a
    half-written JSON file that breaks load.

    What we persist:
      - `_spark_past_cache` (past daily closes) — high value across
        redeploys. Daily resolution, full rebuild costs ~30 s.
      - `_spark_today_cache` (today's intraday 30-min closes) — also
        persisted now. Operator: mid-day redeploys were losing the
        intraday tier and the warm task had to re-fetch ~100 symbols
        through the Kite historical_data budget. The 30-min bars
        don't change once a candle closes; the most recent bar may
        be a few minutes stale, but `_today_cache_get` enforces the
        5-min TTL on read so a stale entry quietly falls through to
        a fresh fetch. Net effect: mid-day redeploys keep most of
        today's intraday tier hot from disk, and the next lazy
        fetch refreshes only what's actually stale.

    What we DON'T persist:
      - `_spark_past_attempt` (last-attempt timestamps) — throttle
        metadata. Losing it means at-most-one extra re-attempt per
        symbol post-redeploy, which is exactly the desired behaviour
        (try again now that we're running).

    Throttling:
      - `force=False` (default — called from request handlers):
        skips when the last save was within `_SAVE_THROTTLE_S` seconds.
      - `force=True` (warm task): bypasses the throttle so the
        post-warm snapshot lands immediately.

    Change-detection:
      - Compares the SHA-256 of the past-snapshot to the hash of the
        last successfully-persisted payload. Identical content → skip
        the disk write entirely. In the steady state (operator idle,
        cache static), per-request `_save_caches_to_disk` calls
        return without touching disk.
    """
    global _last_save_at, _last_save_hash
    import time as _t
    now = _t.time()
    if not force and (now - _last_save_at) < _SAVE_THROTTLE_S:
        return

    # Shallow-copy the past-cache under the lock, then build the
    # serialisable snapshot OUTSIDE the lock. The previous shape held
    # `_spark_lock` for the duration of dict comprehensions which
    # blocked every concurrent batch_sparkline cache read for that
    # full window. dict() copy is O(n) but constant-factor cheap and
    # releases the lock immediately.
    today = _ist_today()
    with _spark_lock:
        past_copy  = dict(_spark_past_cache)
        today_copy = dict(_spark_today_cache)
    past_snapshot = {
        _key_to_str(k): list(v)
        for k, v in past_copy.items()
        if k[3] == today
    }
    # Today cache key shape: (sym, exch, today). Value shape:
    # (cached_at_epoch, list[float]). Serialised as [epoch, closes]
    # so the restore reads cleanly without dict shape gymnastics.
    today_snapshot = {
        _key_to_str(k): [float(v[0]), list(v[1])]
        for k, v in today_copy.items()
        if k[2] == today
    }

    payload = {
        "ist_date": today,
        "past":     past_snapshot,
        "today":    today_snapshot,
    }

    # Change-detection. Compute the hash of the serialised payload
    # and compare to the last write. Identical → no write needed.
    import hashlib
    serialised = json.dumps(payload, sort_keys=True)
    content_hash = hashlib.sha256(serialised.encode()).hexdigest()
    if not force and content_hash == _last_save_hash:
        # Steady-state path — burst of identical writes returns here
        # without touching disk. Still update the throttle timestamp
        # so the next change actually fires.
        _last_save_at = now
        return

    _last_save_at = now
    _last_save_hash = content_hash

    def _do_write():
        with _persist_file_lock():
            _PERSIST_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = _PERSIST_PATH.with_suffix(".tmp")
            try:
                with open(tmp_path, "w") as f:
                    f.write(serialised)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, _PERSIST_PATH)
            except Exception as e:
                logger.warning(f"sparkline cache save failed: {e}")
                try:
                    tmp_path.unlink(missing_ok=True)
                except Exception:
                    pass

    # Avoid holding the data lock while writing to disk — kick the write
    # to a background thread so request handlers stay responsive even on
    # a slow disk.
    threading.Thread(target=_do_write, daemon=True).start()


def load_sparkline_cache_from_disk() -> int:
    """Restore `_spark_past_cache` from disk on process startup.
    Returns the count of past-cache entries loaded.

    Only past-cache is persisted (see `_save_caches_to_disk` docstring
    for rationale). Today-cache and past-attempt are rebuilt naturally
    by the startup warm task + lazy fetches.

    Skipped wholesale when the file is from a prior IST date — the
    cached values would just be evicted on the first batch_sparkline
    call anyway, so loading them is pure overhead.

    Idempotent and exception-safe: no file / corrupted file / wrong
    schema all silently no-op, and the startup warm task fills the
    gap in ~30 s.

    Tolerates older on-disk schemas that included `today` /
    `past_attempt` keys — those are silently ignored.
    """
    try:
        if not _PERSIST_PATH.exists():
            return 0
        with _persist_file_lock():
            with open(_PERSIST_PATH, "r") as f:
                payload = json.load(f)
    except Exception as e:
        logger.warning(f"sparkline cache load failed (will rebuild via warm): {e}")
        return 0

    today = _ist_today()
    if payload.get("ist_date") != today:
        logger.info(
            f"sparkline cache on disk is from {payload.get('ist_date')!r}, "
            f"today is {today!r} — skipping reload, startup warm will rebuild"
        )
        return 0

    past_loaded = 0
    today_loaded = 0
    with _spark_lock:
        for k_str, closes in (payload.get("past") or {}).items():
            try:
                parts = k_str.split("|")
                if len(parts) != 4:
                    continue
                sym, exch, days_s, date_s = parts
                key = (sym, exch, int(days_s), date_s)
                if isinstance(closes, list):
                    _spark_past_cache[key] = [float(c) for c in closes]
                    past_loaded += 1
            except Exception:
                continue
        # Restore today_cache too. The on-disk shape is
        # `[cached_at_epoch, [closes]]`. `_today_cache_get` enforces
        # the 5-min TTL on read, so any entry older than that quietly
        # falls through to a fresh fetch — restoring all-of-today is
        # safe; the stale ones just don't get served. Net win: a
        # mid-day restart within the 5-min window keeps every
        # intraday bar hot from disk.
        for k_str, entry in (payload.get("today") or {}).items():
            try:
                parts = k_str.split("|")
                if len(parts) != 3:
                    continue
                sym, exch, date_s = parts
                if (
                    isinstance(entry, list) and len(entry) == 2
                    and isinstance(entry[1], list)
                ):
                    cached_at = float(entry[0])
                    closes = [float(c) for c in entry[1]]
                    _spark_today_cache[(sym, exch, date_s)] = (cached_at, closes)
                    today_loaded += 1
            except Exception:
                continue

    if past_loaded or today_loaded:
        logger.info(
            f"sparkline cache restored from disk: {past_loaded} past + "
            f"{today_loaded} today entries (ist_date={today})"
        )
    return past_loaded + today_loaded

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

    First call per IST day: fetches all sparkline exchanges (blocking HTTP).
    Subsequent calls: O(1) dict lookup under the lock.
    """
    today = _ist_today()
    with _TOKEN_MAP_LOCK:
        cached = _TOKEN_MAP_CACHE.get(today)
        if cached is not None:
            return cached
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
    return new_map


def _ist_today() -> str:
    """Return today's date in IST as YYYY-MM-DD."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d")
    except Exception:
        # Fallback: UTC+5:30
        return (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d")


def _evict_stale(today: str) -> None:
    """Drop past-cache + today-cache entries whose date != today
    (lazy eviction on each batch call)."""
    stale = [k for k in _spark_past_cache if k[3] != today]
    for k in stale:
        del _spark_past_cache[k]
        _spark_past_attempt.pop(k, None)
    stale_today = [k for k in _spark_today_cache if k[2] != today]
    for k in stale_today:
        del _spark_today_cache[k]


def _past_cache_is_complete(
    cached: Optional[list[float]],
    key: tuple,
    days: int,
) -> bool:
    """Decide whether a past-cache hit can be served as-is, or whether
    the caller should fall through to a fresh historical_data fetch.

    Hit semantics:
      - cached is None                 → MISS (key not present at all)
      - len(cached) >= (days - 1)      → HIT, full window present
      - shorter AND last-attempt was   → HIT (throttled re-attempt window),
        within _INCOMPLETE_RETRY_S       serve the partial result
      - shorter AND last-attempt was   → MISS (allow re-fetch — handles
        > _INCOMPLETE_RETRY_S            redeploy with stale partial, plus
                                          newly-listed symbols whose history
                                          grows over time)

    Without the completeness check, a partial cache entry (e.g. Kite
    returned only 2 bars because of a long holiday weekend, or the
    fetch raced a token refresh and got an empty list) sticks for the
    rest of the day. After redeploy the warm task refills the cache,
    but ANY lazy fetch that produced a short result locks in that
    shortness — operator sees a sparkline that never grows back to
    the full window. The retry-window throttle prevents hammering
    on symbols that are GENUINELY only N days old (newly listed)
    where every fetch will be short.
    """
    if cached is None:
        return False
    if len(cached) >= (days - 1):
        return True
    import time as _t
    last_attempt = _spark_past_attempt.get(key, 0.0)
    return (_t.time() - last_attempt) < _INCOMPLETE_RETRY_S


def _past_cache_record_attempt(key: tuple) -> None:
    """Stamp the last-attempt timestamp on a past-cache key. Called
    after every historical_data fetch (success or empty) so the
    throttle in `_past_cache_is_complete` knows when to allow the
    next attempt."""
    import time as _t
    _spark_past_attempt[key] = _t.time()


def _today_intraday_closes(
    raw_bars: list[dict],
    today: str,
) -> list[float]:
    """Pull the close prices for TODAY's bars only from a Kite
    historical_data response. Filters by bar date so an over-broad
    range (e.g. from_d set to today-1 to cover NSE 09:15 vs MCX
    09:00 timing differences) doesn't pollute the today series with
    yesterday's bars. Keeps the running last bar — that's today's
    most recent 30-minute close (or in-progress aggregate)."""
    out: list[float] = []
    for b in raw_bars or []:
        if _bar_date_iso(b) != today:
            continue
        c = b.get("close")
        if c is None:
            continue
        try:
            out.append(float(c))
        except (TypeError, ValueError):
            continue
    return out


def _fetch_today_intraday_sync(
    broker, token: int, today: str,
) -> list[float]:
    """Blocking helper — fetches today's 30-minute closes for one
    instrument and returns the list of close prices. Off-hours
    (pre-market, post-close, weekend / holiday) returns []; the
    caller falls back to just the past-closes window in that case.

    Run via asyncio.to_thread by both batch_sparkline and
    warm_sparkline_cache."""
    try:
        from_d = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        to_d   = datetime.now()
        raw = broker.historical_data(token, from_d, to_d, _TODAY_INTERVAL) or []
        return _today_intraday_closes(raw, today)
    except Exception as exc:
        logger.warning(f"sparkline: today intraday fetch failed for token={token}: {exc}")
        return []


def _today_cache_get(sym: str, exch: str, today: str) -> Optional[list[float]]:
    """Return today's intraday closes for (sym, exch) when cached AND
    the entry is fresher than `_TODAY_TTL_S` seconds; else None.
    Eviction of stale-date entries is handled by `_evict_stale`."""
    import time as _t
    key = (sym, exch, today)
    with _spark_lock:
        entry = _spark_today_cache.get(key)
    if entry is None:
        return None
    cached_at, closes = entry
    if (_t.time() - cached_at) > _TODAY_TTL_S:
        return None
    return closes


def _today_cache_put(sym: str, exch: str, today: str, closes: list[float]) -> None:
    """Store today's intraday closes. Empty lists are NOT cached —
    that way the next call gets another chance during the day (e.g.,
    we tried at 09:14:55 and got no bars yet; the 09:15 NSE-open
    boundary or the next request after 09:15 succeeds)."""
    if not closes:
        return
    import time as _t
    with _spark_lock:
        _spark_today_cache[(sym, exch, today)] = (_t.time(), closes)


def _bar_date_iso(bar: dict) -> str:
    """Return the bar's date as YYYY-MM-DD in IST.

    Kite's historical_data bars carry `date` as a tz-aware datetime
    (IST) when interval=day. Older SDKs sometimes hand back an ISO
    string. Handle both shapes defensively so the today-bar drop
    rule below works either way."""
    raw = bar.get("date")
    if raw is None:
        return ""
    if hasattr(raw, "strftime"):
        try:
            return raw.strftime("%Y-%m-%d")
        except Exception:
            pass
    s = str(raw)
    # Pull the first 10 chars — works for "2026-06-12", "2026-06-12T...", etc.
    return s[:10] if len(s) >= 10 else ""


def _trim_past_closes(raw_bars: list[dict], days: int, today: str) -> list[float]:
    """Build the sparkline's past-closes window from Kite's
    historical_data bars. Returns the most recent (days-1) closed-day
    closes, oldest first. Caller appends today's live LTP on top of
    this to produce the final `days`-point sparkline.

    Critical: drop the LAST bar only when its date matches today
    (intraday-running value that we'll replace with live LTP).
    Off-hours / pre-market the last bar IS yesterday's settled close
    — dropping it leaves the operator looking at D-5..D-2 with
    today's LTP appended, with yesterday's close missing entirely.
    That looked like "5d sparkline not updating" — the most recent
    settled close was just gone.

    Falls back to keeping every bar when bar dates can't be parsed
    (defensive — preserves the old shape).
    """
    if not raw_bars:
        return []
    closes_with_date: list[tuple[str, float]] = []
    for b in raw_bars:
        close = b.get("close")
        if close is None:
            continue
        try:
            closes_with_date.append((_bar_date_iso(b), float(close)))
        except (TypeError, ValueError):
            continue
    if not closes_with_date:
        return []
    last_date, _ = closes_with_date[-1]
    if last_date == today:
        # Today's intraday-running bar — drop. The live-LTP append
        # downstream replaces it with a fresh quote.
        closes_with_date = closes_with_date[:-1]
    closes = [c for (_d, c) in closes_with_date]
    if len(closes) > (days - 1):
        closes = closes[-(days - 1):]
    return closes


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
        requested symbol, with today's running LTP appended as the final
        point. Used by the /pulse sparkline column.

        Two-tier cache:
        - Past (days-1) closes served from _spark_past_cache (populated by
          historical_data on first miss; pre-warmed at market-open by
          _task_sparkline_warm). historical_data budget: 3 req/sec.
        - Today's running price appended per-response via a single batched
          broker.ltp() call (10 req/sec quota). Never cached; always fresh.

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

        # Lazy past-cache eviction.
        with _spark_lock:
            _evict_stale(today)

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
            # Bare-commodity heuristic (matches `_build_quote_key` in
            # watchlist.py): MCX/CDS + all-alpha + ≤ 12 chars. Real
            # futures ("CRUDEOIL26JUNFUT") carry digits → pass through.
            if exch == "MCX" and sym.isalpha() and len(sym) <= 12:
                resolved = await _resolve_mcx_commodity(sym)
                if resolved:
                    sym = resolved.upper().strip()
            elif exch == "CDS" and sym.isalpha() and len(sym) <= 12:
                resolved = await _resolve_cds_currency(sym)
                if resolved:
                    sym = resolved.upper().strip()
            norm_syms.append(SparklineSymbol(tradingsymbol=sym, exchange=exch))

        past_result: dict[str, list[float]] = {}
        to_fetch: list[SparklineSymbol] = []
        # token_map is populated during the historical-fetch step when
        # to_fetch is non-empty. Initialise here so Step 2 (ticker
        # subscription + tick-map read) can reference it regardless of
        # whether the historical step ran.
        token_map: dict[str, int] = {}

        for sym_obj in norm_syms:
            cache_key = (sym_obj.tradingsymbol, sym_obj.exchange, days, today)
            with _spark_lock:
                cached = _spark_past_cache.get(cache_key)
                # Hit only when the cached window is COMPLETE (or recently
                # re-attempted). A partial entry — Kite returned 2 bars
                # because of a holiday weekend, the warm task hit a token
                # refresh mid-flight, etc. — falls through to to_fetch so
                # the next fetch tops up the window. Without this, an
                # incomplete entry sticks for the day and the sparkline
                # silently stays short.
                is_hit = _past_cache_is_complete(cached, cache_key, days)
            if is_hit:
                past_result[sym_obj.tradingsymbol] = cached  # type: ignore[arg-type]
            else:
                to_fetch.append(sym_obj)

        # ── Step 1: Historical fetch for cache-miss symbols ──────────────────
        if to_fetch:
            try:
                broker = get_sparkline_broker()
            except Exception as exc:
                logger.warning(f"sparkline: broker unavailable: {exc}")
                broker = None

            if broker is not None:
                # Build token map from the day-cache (one fetch per IST day
                # across all sparkline exchanges; subsequent calls are O(1)).
                try:
                    _full_map = await asyncio.to_thread(_get_today_token_map, broker)
                    token_map = {}
                    for sym_obj in to_fetch:
                        if sym_obj.tradingsymbol in token_map:
                            continue
                        pref = [sym_obj.exchange] + [e for e in ("MCX", "CDS", "NFO", "BFO", "NSE", "BSE") if e != sym_obj.exchange]
                        for ex in pref:
                            tok = _full_map.get((sym_obj.tradingsymbol, ex))
                            if tok is not None:
                                token_map[sym_obj.tradingsymbol] = tok
                                break
                except Exception as exc:
                    logger.warning(f"sparkline: instrument lookup failed: {exc}")
                    token_map = {}

                if token_map:
                    # Fetch historical data (past days-1 closes only — drop
                    # today's intraday-running bar which Kite returns as the
                    # last entry; today's price comes from the LTP step below).
                    to_d   = datetime.now()
                    from_d = to_d - timedelta(days=days + 5)  # +5 buffer for weekends/holidays

                    # Round-robin across every historical-eligible Kite
                    # account so a heavy lazy-fetch burst (Pulse load
                    # during the warm window) doesn't bottleneck on a
                    # single broker's 3 req/sec budget. Each broker
                    # keeps its own Sem(2) + pace(0.35 s). Falls back to
                    # the original single-broker path cleanly when only
                    # one account is available.
                    try:
                        from backend.shared.brokers.registry import get_historical_brokers
                        _broker_pool = get_historical_brokers() or [broker]
                    except Exception:
                        _broker_pool = [broker]
                    _KITE_PACE_S = 0.35
                    _sems = [asyncio.Semaphore(2) for _ in _broker_pool]

                    def _pick(idx: int) -> tuple:
                        n = len(_broker_pool)
                        return _broker_pool[idx % n], _sems[idx % n]

                    async def _fetch_closes(sym: str, token: int, idx: int) -> tuple[str, list[float]]:
                        bk, sem_b = _pick(idx)
                        async with sem_b:
                            try:
                                raw = await asyncio.to_thread(
                                    bk.historical_data, token, from_d, to_d, "day"
                                ) or []
                                past_closes = _trim_past_closes(raw, days, today)
                                await asyncio.sleep(_KITE_PACE_S)
                                return sym, past_closes
                            except Exception as exc:
                                logger.warning(f"sparkline historical_data failed for {sym}: {exc}")
                                await asyncio.sleep(_KITE_PACE_S)
                                return sym, []

                    tasks = [
                        _fetch_closes(sym_obj.tradingsymbol, token_map[sym_obj.tradingsymbol], i)
                        for i, sym_obj in enumerate(to_fetch)
                        if sym_obj.tradingsymbol in token_map
                    ]

                    if tasks:
                        fetched = await asyncio.gather(*tasks)
                        # O(1) lookup dict — avoids O(N²) `next(…)` scan over to_fetch.
                        _to_fetch_exch = {s.tradingsymbol: s.exchange for s in to_fetch}
                        with _spark_lock:
                            for sym, past_closes in fetched:
                                exch = _to_fetch_exch.get(sym, "NSE")
                                cache_key = (sym, exch, days, today)
                                # Stamp every attempt — empty or short —
                                # so the throttle in _past_cache_is_complete
                                # waits _INCOMPLETE_RETRY_S before retrying
                                # this symbol again.
                                _past_cache_record_attempt(cache_key)
                                if past_closes:
                                    _spark_past_cache[cache_key] = past_closes
                                    past_result[sym] = past_closes

        # ── Step 2: LTP for ALL symbols — tick map first, broker.ltp() fallback
        #
        # Resolution order (per symbol):
        #   1. TickerManager._tick_map  — live WebSocket stream; zero Kite quota.
        #   2. broker.ltp() batch      — covers symbols not yet in the tick map
        #      (ticker not connected, subscription not yet active, market closed).
        #
        # After the token_map is built from the instrument lookup above, push all
        # newly-discovered tokens to the ticker so the NEXT request reads from the
        # stream instead of hitting broker.ltp().
        #
        # The ticker's subscribe() is idempotent and cheap — calling it on every
        # sparkline request is safe and ensures the subscription list grows
        # automatically as new symbols enter the pulse view.
        from backend.shared.helpers.kite_ticker import get_ticker

        ticker = get_ticker()

        # Push every resolved token to the ticker (deduped inside subscribe()).
        # token_map is the symbol→token dict built during the historical-fetch
        # step above; it may be empty if that step was skipped (all past-cached).
        # Also collect tokens for symbols that were already past-cached but whose
        # token we need for the ticker subscription and tick-map read.
        #
        # We rebuild a token_map covering ALL norm_syms (not just to_fetch) so
        # both the subscription push and the tick-map lookup are comprehensive.
        # If token_map was populated above, reuse it; otherwise fall through.
        # Note: token_map may only cover to_fetch symbols if the historical step
        # ran. For already-cached symbols we do a lightweight instrument lookup
        # here so they also get subscribed.
        if not token_map:
            # Historical step was skipped (all symbols past-cached). Build
            # token_map from the day-cache for ticker subscription + tick-map read.
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
                logger.warning(f"sparkline: token lookup for ticker subs failed: {_exc}")

        # Push all resolved tokens to the ticker (idempotent, non-blocking).
        # CRITICAL: use subscribe_with_sym so the ticker's _token_to_sym
        # map is populated. Without it, SSE tick payloads carry sym="" and
        # the frontend's quoteStream filter (`if (t && t.sym && ...)`)
        # silently drops every tick — pinned data + sparklines stop
        # refreshing live even though Kite is sending ticks.
        if token_map:
            ticker.subscribe_with_sym(
                [(tok, sym) for sym, tok in token_map.items()]
            )

        # Build reverse map: quote_key (EXCHANGE:SYMBOL) → instrument_token.
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

        # ── Step 3: Today's intraday bars (cache → fetch on miss/stale) ──
        # Adds today's 30-minute closes to each symbol so the sparkline
        # shows today's actual path, not just a single end-of-line LTP
        # dot. Cached separately from past closes with a 5-minute TTL so
        # the right edge of the sparkline keeps moving as the session
        # progresses. Symbols whose today-cache is stale or missing get
        # one historical_data call paced against the same 3 req/sec
        # budget as the past-closes step.
        today_result: dict[str, list[float]] = {}
        today_to_fetch: list[tuple[str, str, int]] = []   # (sym, exch, token)
        for sym_obj in norm_syms:
            sym  = sym_obj.tradingsymbol
            exch = sym_obj.exchange
            cached_today = _today_cache_get(sym, exch, today)
            if cached_today is not None:
                today_result[sym] = cached_today
                continue
            tok = token_map.get(sym)
            if tok is not None:
                today_to_fetch.append((sym, exch, tok))

        if today_to_fetch:
            try:
                from backend.shared.brokers.registry import get_historical_brokers
                t_pool = get_historical_brokers()
            except Exception:
                t_pool = []
            if not t_pool:
                try:
                    t_pool = [get_sparkline_broker()]
                except Exception:
                    t_pool = []
            if t_pool:
                # Same round-robin pattern as past-closes — each broker
                # holds its own Sem(2) + pace(0.35 s) so per-account
                # 3 req/sec budget compounds with broker count.
                _t_sems = [asyncio.Semaphore(2) for _ in t_pool]

                def _t_pick(idx: int) -> tuple:
                    n = len(t_pool)
                    return t_pool[idx % n], _t_sems[idx % n]

                async def _fetch_today(sym: str, exch: str, token: int, idx: int) -> tuple[str, str, list[float]]:
                    bk, sem_b = _t_pick(idx)
                    async with sem_b:
                        closes = await asyncio.to_thread(
                            _fetch_today_intraday_sync, bk, token, today,
                        )
                        await asyncio.sleep(0.35)
                        return sym, exch, closes

                t_tasks = [_fetch_today(s, e, t, i) for i, (s, e, t) in enumerate(today_to_fetch)]
                t_fetched = await asyncio.gather(*t_tasks)
                for sym, exch, closes in t_fetched:
                    if closes:
                        _today_cache_put(sym, exch, today, closes)
                        today_result[sym] = closes

        result: dict[str, list[float]] = {}
        for sym_obj in norm_syms:
            sym  = sym_obj.tradingsymbol
            past = past_result.get(sym, [])
            today_bars = today_result.get(sym, [])
            ltp_key = f"{sym_obj.exchange}:{sym}"
            ltp_val = ltp_map.get(ltp_key)
            tail_ltp = ltp_val if (ltp_val and ltp_val > 0) else None
            # Series order: past daily closes, then today's intraday
            # closes (chronological), then current LTP if available.
            # Off-hours today_bars is empty → series collapses to
            # past + [ltp] (old behaviour). During session, today_bars
            # carries the intraday path. The frontend's liveTail
            # overlay replaces just the last point — fine whether
            # that's an LTP or the last intraday bar.
            series = past + today_bars
            if tail_ltp is not None:
                series = series + [tail_ltp]
            if series:
                result[sym] = series
            # If neither past nor today nor ltp: omit silently
            # (symbol unresolvable).

        # Persist any lazy-fetch updates to disk so a redeploy
        # restores them on next startup. Throttled to one write per
        # 5 s; runs in a background thread so the request returns
        # without waiting for fsync.
        _save_caches_to_disk()

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
    Pre-populate _spark_past_cache for the given (tradingsymbol, exchange)
    pairs. Called by _task_sparkline_warm in background.py at market open
    and at app startup.

    Returns the count of symbols successfully cached.

    Uses the same Semaphore(2) + pacing logic as batch_sparkline. Symbols
    already in the cache for today are skipped. Errors per-symbol are
    swallowed silently (warn-level log) so one bad symbol never aborts the
    whole warm run.
    """
    global _spark_warm_symbols, _spark_warm_at

    if not symbols:
        return 0

    today = _ist_today()
    with _spark_lock:
        _evict_stale(today)

    # Filter to symbols whose past-cache is missing OR incomplete (so
    # we re-warm partial entries from prior fetches). Same completeness
    # rule as the endpoint — keeps the two surfaces in sync.
    to_fetch: list[SparklineSymbol] = []
    for sym, exch in symbols:
        sym_n  = sym.upper().strip()
        exch_n = exch.upper().strip()
        key = (sym_n, exch_n, days, today)
        with _spark_lock:
            cached = _spark_past_cache.get(key)
            is_hit = _past_cache_is_complete(cached, key, days)
        if not is_hit:
            to_fetch.append(
                SparklineSymbol(tradingsymbol=sym_n, exchange=exch_n)
            )
    # Today's intraday set — re-fetched even when past-cache is hot,
    # because intraday bars expire on a 5-minute TTL and the segment-
    # open warms (09:00 / 09:15 IST) need to seed the cache with the
    # first few bars so the operator's first Pulse load sees today's
    # path immediately.
    today_to_fetch = [
        SparklineSymbol(tradingsymbol=sym.upper().strip(), exchange=exch.upper().strip())
        for sym, exch in symbols
        if _today_cache_get(sym.upper().strip(), exch.upper().strip(), today) is None
    ]
    if not to_fetch and not today_to_fetch:
        logger.info(f"sparkline warm: all {len(symbols)} symbols already cached")
        return 0

    try:
        from backend.shared.brokers.registry import get_sparkline_broker
        broker = get_sparkline_broker()
    except Exception as exc:
        logger.warning(f"sparkline warm: broker unavailable, skipping: {exc}")
        return 0

    # Build instrument token map from the day-cache (shared with batch_sparkline).
    # Resolve for the union of past + today fetches so the warm pass
    # below can issue both kinds of historical_data calls in parallel.
    token_map: dict[str, int] = {}
    try:
        _full_map = await asyncio.to_thread(_get_today_token_map, broker)
        union_syms = {(s.tradingsymbol, s.exchange) for s in (to_fetch + today_to_fetch)}
        for sym_str, exch_str in union_syms:
            if sym_str in token_map:
                continue
            pref = [exch_str] + [e for e in ("MCX", "CDS", "NFO", "BFO", "NSE", "BSE") if e != exch_str]
            for ex in pref:
                tok = _full_map.get((sym_str, ex))
                if tok is not None:
                    token_map[sym_str] = tok
                    break
    except Exception as exc:
        logger.warning(f"sparkline warm: instrument lookup failed: {exc}")
        return 0

    if not token_map:
        return 0

    # Subscribe the ticker BEFORE the historical-data fetches kick off.
    # The KiteTicker WebSocket reconnect + token subscription proceeds
    # in parallel with the (rate-limited) historical fetches, so by the
    # time the warm task completes the operator's first SSE tick is
    # already in flight rather than waiting another 5-15 s after warm
    # completion. subscribe() is idempotent — the post-fetch top-up
    # block below stays for any tokens resolved late, and re-calling
    # for already-subscribed tokens is a no-op.
    _ticker_seed_early(token_map)

    to_d   = datetime.now()
    from_d = to_d - timedelta(days=days + 5)

    # Round-robin work across every historical-eligible Kite account so
    # Kite's per-account 3 req/sec budget compounds with broker count.
    # Each broker keeps its OWN Semaphore(2) + 0.35 s pace pair — within
    # the per-account limit. With two Kite accounts the effective
    # historical throughput doubles (~6 req/sec) without touching
    # per-broker pacing. Falls back to the original single-broker path
    # gracefully when only one account is available.
    try:
        from backend.shared.brokers.registry import get_historical_brokers
        broker_pool = get_historical_brokers() or [broker]
    except Exception:
        broker_pool = [broker]
    _KITE_PACE_S = 0.35
    sems = [asyncio.Semaphore(2) for _ in broker_pool]

    def _pick(idx: int) -> tuple:
        n = len(broker_pool)
        return broker_pool[idx % n], sems[idx % n]

    async def _fetch_past(sym: str, token: int, idx: int) -> tuple[str, list[float]]:
        bk, sem = _pick(idx)
        async with sem:
            try:
                raw = await asyncio.to_thread(bk.historical_data, token, from_d, to_d, "day") or []
                past_closes = _trim_past_closes(raw, days, today)
                await asyncio.sleep(_KITE_PACE_S)
                return sym, past_closes
            except Exception as exc:
                logger.warning(f"sparkline warm: historical_data failed for {sym}: {exc}")
                await asyncio.sleep(_KITE_PACE_S)
                return sym, []

    tasks = [
        _fetch_past(sym_obj.tradingsymbol, token_map[sym_obj.tradingsymbol], i)
        for i, sym_obj in enumerate(to_fetch)
        if sym_obj.tradingsymbol in token_map
    ]

    async def _fetch_today_warm(sym: str, token: int, idx: int) -> tuple[str, list[float]]:
        bk, sem = _pick(idx)
        async with sem:
            closes = await asyncio.to_thread(
                _fetch_today_intraday_sync, bk, token, today,
            )
            await asyncio.sleep(_KITE_PACE_S)
            return sym, closes

    today_tasks = [
        _fetch_today_warm(sym_obj.tradingsymbol, token_map[sym_obj.tradingsymbol], i)
        for i, sym_obj in enumerate(today_to_fetch)
        if sym_obj.tradingsymbol in token_map
    ]

    # Build O(1) sym→exch lookup once before the result loops.
    # Without this, `next(s.exchange for s in to_fetch if …)` is O(N)
    # per result → O(N²) total for 300 symbols (90,000 iterations).
    sym_to_exch      = {s.tradingsymbol: s.exchange for s in to_fetch}
    sym_to_exch_today = {s.tradingsymbol: s.exchange for s in today_to_fetch}

    cached_count = 0
    if tasks:
        fetched = await asyncio.gather(*tasks)
        with _spark_lock:
            for sym, past_closes in fetched:
                exch = sym_to_exch.get(sym, "NSE")
                key = (sym, exch, days, today)
                _past_cache_record_attempt(key)
                if past_closes:
                    _spark_past_cache[key] = past_closes
                    cached_count += 1

    today_cached = 0
    if today_tasks:
        fetched_today = await asyncio.gather(*today_tasks)
        for sym, today_closes in fetched_today:
            if today_closes:
                exch = sym_to_exch_today.get(sym, "NSE")
                _today_cache_put(sym, exch, today, today_closes)
                today_cached += 1
        if today_cached:
            logger.info(
                f"sparkline warm: today-intraday cached {today_cached}/"
                f"{len(today_tasks)} symbols"
            )

    # Ticker subscription already happened up front via _ticker_seed_early.
    # The WS reconnect + token subscribe ran in PARALLEL with the historical
    # fetches above, so by the time we land here the WebSocket is already
    # streaming live ticks rather than waiting on the operator's first
    # request to trigger the subscribe.

    _spark_warm_symbols = cached_count
    _spark_warm_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    logger.info(f"sparkline warm: cached {cached_count}/{len(to_fetch)} symbols for {today}")
    # Persist the full warm result to disk. force=True bypasses the
    # 5-s request-side throttle so a deploy seconds after a warm
    # completes still boots from the freshly-warmed snapshot — not
    # from a 5-s-stale on-disk copy. Warm cycles fire ~4× per day
    # so the unthrottled write is cheap.
    _save_caches_to_disk(force=True)
    return cached_count
