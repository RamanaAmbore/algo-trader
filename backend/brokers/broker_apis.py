import os
import threading
import pandas as pd
import polars as pl
import time as _time

from backend.api.algo.pnl_math import decomposed_intraday_pnl, naive_day_pnl
from backend.brokers.connections import Connections
from backend.shared.helpers.decorators import for_all_accounts
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Last-known-good LTP cache
# ---------------------------------------------------------------------------
# Persists the most recent valid (> 0) LTP per symbol across broker calls.
# When all live sources (PriceBroker.quote + KiteTicker) return 0 or raise,
# this cache allows the route to return a recent real value instead of
# propagating a zero to the frontend.
#
# Shape: {symbol: (unix_ts, ltp)}
# Thread-safe via _LAST_GOOD_LTP_LOCK.
# TTL: entries older than 1 hour are treated as absent so overnight stale
# values don't bleed into the next session.  Process restart clears the
# dict (in-memory only by design — next successful fetch repopulates).
_LAST_GOOD_LTP: dict[str, tuple[float, float]] = {}
_LAST_GOOD_LTP_LOCK = threading.Lock()
_LAST_GOOD_LTP_TTL_S: float = 3600.0  # 1 hour


def record_good_ltp(symbol: str, ltp: float) -> None:
    """Record `ltp` as the last-known-good price for `symbol`.

    Only writes when ltp > 0.  Thread-safe."""
    if not symbol or not (ltp > 0):
        return
    now = _time.time()
    with _LAST_GOOD_LTP_LOCK:
        _LAST_GOOD_LTP[symbol] = (now, float(ltp))


def get_last_good_ltp(symbol: str, max_age_s: float = _LAST_GOOD_LTP_TTL_S) -> float | None:
    """Return the cached last-known-good LTP for `symbol` if it was
    recorded within the last `max_age_s` seconds, else None.

    Thread-safe read; returns None when symbol unknown or entry expired."""
    if not symbol:
        return None
    now = _time.time()
    with _LAST_GOOD_LTP_LOCK:
        entry = _LAST_GOOD_LTP.get(symbol)
    if entry is None:
        return None
    ts, ltp = entry
    if now - ts > max_age_s:
        return None
    return ltp


def _ts_label(unix_ts: float) -> str:
    """Format a unix timestamp as HH:MM IST for log lines."""
    try:
        from zoneinfo import ZoneInfo
        from datetime import datetime
        return datetime.fromtimestamp(unix_ts, tz=ZoneInfo("Asia/Kolkata")).strftime("%H:%M IST")
    except Exception:
        return str(int(unix_ts))


def _col_f64(lf: pl.DataFrame, col: str) -> pl.Expr:
    """Return a Float64 expression for `col`, coercing nulls/bad values to 0.0."""
    return pl.col(col).cast(pl.Float64, strict=False).fill_null(0.0)


def _col_f64_nullable(lf: pl.DataFrame, col: str) -> pl.Expr:
    """Like _col_f64 but keeps nulls as nulls (for broker-value trust checks)."""
    return pl.col(col).cast(pl.Float64, strict=False)


# RAMBOQ_USE_CONN_SERVICE — when set on the main API process, the
# zero-arg public fetch_* entry points proxy to conn_service over UDS
# instead of running the in-process broker code. Conn_service itself
# leaves this UNSET, so its own broker_apis import keeps running the
# local @for_all_accounts path (no recursion).
#
# Cached at module load — per-call os.environ.get + .strip + .lower
# was a P2 finding (called on every fetch_holdings/positions/margins
# invocation, ~5/sec under polling). Module constant gives the same
# semantics at zero cost. The flag never changes at runtime within a
# process; service restart is the only way to flip it.
_USE_CONN_SERVICE: bool = os.environ.get(
    "RAMBOQ_USE_CONN_SERVICE", "",
).strip().lower() in ("1", "true", "yes", "on")


def _use_conn_service() -> bool:
    return _USE_CONN_SERVICE

# One-shot flag for the Kite MCX value-units diagnostic in
# fetch_positions. Logs the raw `day_buy_value` vs the two possible
# unit interpretations for the first MCX-multiplier row seen so the
# operator can confirm whether `5b995ccb` is correct or overcounts.
# Reset on process restart.
_KITE_VALUE_UNIT_LOGGED = False

# Per-account fetch-result tracker — the navbar broker-health badge
# reads from this to flag accounts whose latest fetch attempt FAILED
# (even though the connection object is still in Connections.conn).
# Operator: "when connection issue there in groww, I still 5/5 in
# navbar instead 4/5 as one account connection has issue."
#
# Shape per account:
#   { 'last_ok_at':    float (unix ts) | 0,   # most recent successful call
#     'last_fail_at':  float (unix ts) | 0,   # most recent failed call
#     'last_fail_msg': str,
#     # Circuit-breaker fields (added Jul 2026 — DH6847 rotation-loop fix):
#     'consecutive_fail_count': int,           # reset to 0 on any success
#     'circuit_open_until':     float | None,  # epoch ts; None = CLOSED
#     'circuit_last_opened_at': float | None,  # for logging
#     'open_cycle_count':       int,           # exponential back-off tracker
#   }
# An account is healthy when last_ok_at >= last_fail_at OR there's
# no recorded attempt yet (never tried = assume healthy).
_FETCH_HEALTH: dict[str, dict] = {}

# Dedicated lock for the read-modify-write on circuit-breaker fields.
# Plain dict writes are GIL-safe but the consecutive_fail_count +=1
# is a compound operation that is NOT safe without a lock.  The existing
# last_ok_at / last_fail_at writes (single assignment) continue without
# a lock — retrofitting them would risk latency regressions on the hot
# path and those fields are informational-only (stale by design).
_BREAKER_LOCK = threading.Lock()

# Circuit-breaker thresholds.
_CB_FAIL_THRESHOLD: int = 3          # consecutive fails to open the breaker
_CB_INITIAL_COOLOFF_S: float = 300.0 # 5 min
_CB_MAX_COOLOFF_S: float = 1800.0    # 30 min cap


def _default_health_entry() -> dict:
    return {
        "last_ok_at": 0.0,
        "last_fail_at": 0.0,
        "last_fail_msg": "",
        "consecutive_fail_count": 0,
        "circuit_open_until": None,
        "circuit_last_opened_at": None,
        "open_cycle_count": 0,
    }


def _circuit_state(account: str) -> str:
    """Return 'open', 'half-open', or 'closed' for `account`.

    Thread-safe read — reads `circuit_open_until` under _BREAKER_LOCK.
    """
    with _BREAKER_LOCK:
        e = _FETCH_HEALTH.get(account)
    if e is None:
        return "closed"
    until = e.get("circuit_open_until")
    if until is None:
        return "closed"
    now = _time.time()
    if now < until:
        return "open"
    return "half-open"


def _is_circuit_open(account: str) -> bool:
    """Return True only when the breaker is OPEN (not half-open)."""
    return _circuit_state(account) == "open"


def _record_fetch(account: str, ok: bool, error: str = "") -> None:
    """Record one fetch attempt's outcome and advance the circuit-breaker
    state machine.

    Called from every per-account broker API wrapper (fetch_holdings /
    fetch_positions / fetch_margins) on both success and failure paths
    so the per-account health state stays current.

    State transitions (Jul 2026 circuit-breaker):
      CLOSED  → ok=True   : reset consecutive_fail_count; stay CLOSED.
      CLOSED  → ok=False  : increment consecutive_fail_count.
                             If count >= _CB_FAIL_THRESHOLD → OPEN.
      HALF-OPEN → ok=True : reset counters; CLOSED.
      HALF-OPEN → ok=False: OPEN again with exponential cool-off.
      OPEN    → any call  : callers must check _is_circuit_open() BEFORE
                             calling the SDK; _record_fetch is not called
                             for short-circuited attempts.

    Consecutive-fail semantics: we count any consecutive failures
    regardless of wall-clock gap (no sliding-window). The cool-off
    period is the rate-limiting mechanism; the counter is just the
    threshold gate to enter it. A single success resets the counter.
    """
    if not account:
        return

    now = _time.time()
    with _BREAKER_LOCK:
        e = _FETCH_HEALTH.setdefault(account, _default_health_entry())
        # Ensure legacy entries (without breaker fields) are back-filled.
        e.setdefault("consecutive_fail_count", 0)
        e.setdefault("circuit_open_until", None)
        e.setdefault("circuit_last_opened_at", None)
        e.setdefault("open_cycle_count", 0)

        if ok:
            e["last_ok_at"] = now
            # Success from HALF-OPEN (or normal CLOSED): reset everything.
            e["consecutive_fail_count"] = 0
            e["circuit_open_until"] = None
            e["circuit_last_opened_at"] = None
            e["open_cycle_count"] = 0
        else:
            e["last_fail_at"] = now
            e["last_fail_msg"] = str(error)[:200]
            e["consecutive_fail_count"] = e.get("consecutive_fail_count", 0) + 1

            if e["consecutive_fail_count"] >= _CB_FAIL_THRESHOLD:
                # Open (or re-open) the breaker with exponential back-off.
                # open_cycle_count: 0→1st open (5m), 1→10m, 2→20m, 3+→30m.
                cycle = e.get("open_cycle_count", 0)
                cooloff = min(
                    _CB_INITIAL_COOLOFF_S * (2 ** cycle),
                    _CB_MAX_COOLOFF_S,
                )
                e["circuit_open_until"] = now + cooloff
                e["circuit_last_opened_at"] = now
                e["open_cycle_count"] = cycle + 1
                logger.warning(
                    f"[BREAKER] account={account} state=open "
                    f"reason={str(error)[:120]} "
                    f"consecutive_fails={e['consecutive_fail_count']} "
                    f"cooloff={int(cooloff)}s "
                    f"open_until={_ts_label(now + cooloff)} "
                    f"cycle={e['open_cycle_count']}"
                )


def is_account_healthy(account: str) -> bool:
    """True iff the most recent fetch for this account succeeded.

    Under RAMBOQ_USE_CONN_SERVICE=1 the local _FETCH_HEALTH dict is
    always empty (broker calls run in conn_service, not here). Falling
    back to "never tried = healthy" would silently report every account
    as 5/5 even during real Groww/Dhan auth failures. Instead we query
    fetch_health_snapshot() which already has the conn_service-aware
    code path and returns the canonical health map for the process.

    In the non-cutover path, _FETCH_HEALTH is populated directly, so
    the fast local dict read still applies — fetch_health_snapshot()
    is only called when the local dict has no entry for the account.
    """
    e = _FETCH_HEALTH.get(account)
    if e:
        return e["last_ok_at"] >= e["last_fail_at"]
    # No local entry — either the local dict was never populated (cutover
    # mode) or the account has truly never been fetched yet. Consult the
    # canonical snapshot (which may hit conn_service over UDS).
    if _use_conn_service():
        snapshot = fetch_health_snapshot()
        remote_e = snapshot.get(account)
        if not remote_e:
            # conn_service also has no entry — account has never been
            # tried; give benefit of the doubt only in this case.
            return True
        return remote_e.get("last_ok_at", 0.0) >= remote_e.get("last_fail_at", 0.0)
    # Non-cutover, no local entry: never tried — healthy.
    return True


def fetch_health_snapshot() -> dict[str, dict]:
    """Read-only copy of the per-account health map.

    When RAMBOQ_USE_CONN_SERVICE is on, the canonical health map
    lives in the conn_service process — the local _FETCH_HEALTH
    in this process is empty (no broker calls landed here). We
    fall back to a synchronous httpx call against the conn_service
    health endpoint and surface its map instead. The navbar badge
    already reads through this function, so no caller migration."""
    if _use_conn_service():
        try:
            from backend.brokers.client.sync import _get_client
            resp = _get_client().get("/internal/health/brokers")
            resp.raise_for_status()
            return (resp.json() or {}).get("health", {}) or {}
        except Exception as e:
            logger.warning(f"conn_service health snapshot failed: {e}")
            return {}
    return {k: dict(v) for k, v in _FETCH_HEALTH.items()}


# ---------------------------------------------------------------------------
# Raw broker-DataFrame TTL cache (Tier 1 — shared by routes + algo.nav)
# ---------------------------------------------------------------------------
# `fetch_holdings()` / `fetch_positions()` / `fetch_margins()` are the
# zero-arg public entry points. They run @for_all_accounts → N broker
# round-trips (or one conn_service UDS hop). Without a cache, every
# consumer that calls them does that work independently:
#
#   • PositionsController / HoldingsController / FundsController
#     (route-level `get_or_fetch("positions", _fetch, ttl=30)` already
#      memos the FORMATTED response, but not the raw DF.)
#   • compute_firm_nav (algo/nav.py) — called by NavCard, /performance,
#     investor portal, nav_daily snapshot writer. Each call fanned out
#     fresh broker fetches.
#   • intraday_equity background poller — wants live data per 5min tick
#     anyway, so it intentionally bypasses (see _fetch_*_direct below).
#
# This module-level TTL cache memos the raw `list[pd.DataFrame]` shape
# returned by `fetch_*()` so two callers within `_RAW_TTL_S` seconds
# share one broker round-trip. The route-level `get_or_fetch` cache
# continues to memo the FORMATTED response (msgspec.Struct), so the
# fast path stays unchanged; this just plugs the leak where
# `compute_firm_nav` previously bypassed both layers.
#
# Per-key asyncio-style locking would require this whole module to be
# async; instead we use a threading.Lock since broker_apis is the
# sync layer (callers offload to threadpool). The race window between
# cache-miss + broker call is bounded by `_RAW_TTL_S`.
_RAW_CACHE_LOCK = threading.Lock()
_RAW_CACHE: dict[str, tuple[float, list[pd.DataFrame]]] = {}
_RAW_TTL_S: float = 30.0  # matches the route-level cache TTL


def _raw_cache_get(key: str) -> list[pd.DataFrame] | None:
    """Return cached list[DataFrame] for `key` if still fresh, else None."""
    with _RAW_CACHE_LOCK:
        entry = _RAW_CACHE.get(key)
    if entry is None:
        return None
    expires_at, value = entry
    if _time.monotonic() >= expires_at:
        return None
    return value


def _raw_cache_put(key: str, value: list[pd.DataFrame]) -> None:
    """Store list[DataFrame] under `key` with the standard TTL.

    Stored value is the caller's reference — DataFrames are NOT deep-copied
    (would defeat the purpose). Callers that need to mutate must `.copy()`
    first; the existing route _fetch() handlers already do this via the
    Polars conversion (`pl.from_pandas(raw)` creates an independent view).
    """
    with _RAW_CACHE_LOCK:
        _RAW_CACHE[key] = (_time.monotonic() + _RAW_TTL_S, value)


def _raw_cache_invalidate(key: str | None = None) -> None:
    """Drop a key (or all keys when key=None). Used by tests + on postback
    `book_changed` events so the next fetch picks up the fresh broker state."""
    with _RAW_CACHE_LOCK:
        if key is None:
            _RAW_CACHE.clear()
        else:
            _RAW_CACHE.pop(key, None)


def fetch_holdings(*args, **kwargs):
    """Public entry — proxies to conn_service when the cutover flag
    is on, otherwise runs the local @for_all_accounts path.

    The proxy branch only kicks in for the zero-arg shape
    (`fetch_holdings()` — every caller in the codebase uses this).
    Explicit `account=`/`broker=` kwargs fall through to the local
    path so single-account internal use keeps working.

    Zero-arg results are memoised in `_RAW_CACHE` for `_RAW_TTL_S`
    seconds so concurrent consumers (routes + compute_firm_nav +
    investor slice) share one broker round-trip per cache window.

    NAV-consistency guarantee (Approach A): the cached value is a
    single-element list containing the post-backfill concatenated
    DataFrame. `backfill_market_data` runs once here — it patches
    close_price + last_price from PriceBroker.quote(), then
    recomputes day_change_val, pnl, cur_val, and pnl_percentage.
    Every consumer (route + compute_firm_nav) then reads the SAME
    patched cur_val so NavCard and /performance agree. The route's
    own `backfill_market_data` call becomes a no-op (no zero-LTP
    rows remain), and `_override_stale_ltp_from_ticker` continues
    to handle the post-cache KiteTicker tick diff.
    """
    if not args and not kwargs:
        cached = _raw_cache_get("holdings")
        if cached is not None:
            return cached
    if _use_conn_service() and not args and not kwargs:
        from backend.brokers.client import sync as conn_sync
        result = conn_sync.fetch_holdings()
        result = _apply_backfill_to_list(result, qty_col="opening_quantity")
        _raw_cache_put("holdings", result)
        return result
    result = _fetch_holdings_local(*args, **kwargs)
    if not args and not kwargs:
        result = _apply_backfill_to_list(result, qty_col="opening_quantity")
        _raw_cache_put("holdings", result)
    return result


@for_all_accounts
def _fetch_holdings_local(connections=Connections, account=None, kite=None, broker=None):
    """Multi-broker holdings fetch. Uses the Broker ABC abstraction
    (broker.holdings()) when available so Dhan / Groww accounts route
    through their own adapters; falls back to the legacy `kite=`
    handle for backwards compatibility with the original Kite-only
    path. Every adapter normalises its response to the Kite-shape
    column set used by downstream UI (tradingsymbol, average_price,
    opening_quantity, pnl, day_change, close_price, etc.)."""
    df_holdings = pd.DataFrame()
    # Circuit-breaker guard: skip SDK call when the breaker is OPEN.
    # Half-open state admits one probe attempt (breaker closes or
    # re-opens based on that attempt's outcome).
    if account and _is_circuit_open(account):
        df_holdings.attrs["circuit_open"] = True
        df_holdings.attrs["fetch_failed"] = True
        logger.warning(
            f"[BREAKER] account={account} short-circuit holdings "
            f"(open until {_ts_label(_FETCH_HEALTH.get(account, {}).get('circuit_open_until', 0) or 0)})"
        )
        return df_holdings
    try:
        rows = None
        if broker is not None:
            rows = broker.holdings()
        elif kite is not None:
            rows = kite.holdings()
        if rows is None:
            df_holdings.attrs['fetch_failed'] = True
            _record_fetch(account, ok=False, error="broker.holdings() returned None")
            return df_holdings
        df_holdings = pd.DataFrame(rows)

        if not df_holdings.empty:
            df_holdings["account"] = account
            df_holdings["type"] = "H"
        _record_fetch(account, ok=True)
    except Exception as e:
        logger.error(f"[{account}] Failed to fetch holdings: {e}")
        df_holdings.attrs['fetch_failed'] = True
        _record_fetch(account, ok=False, error=str(e))

    # Calculated columns — guard against an empty / fetch-failed frame
    # (broker 502 / 503 outages leave df_holdings empty and skipping the
    # math here is the difference between an empty response and a 500
    # KeyError on 'average_price'). Also guard each column reference
    # individually: a normaliser that omits one of the Kite-shape
    # columns (e.g. Groww doesn't always carry close_price) won't break
    # the others.
    if df_holdings.empty:
        return df_holdings

    df_holdings = _enrich_holdings(df_holdings)
    return df_holdings


def _enrich_holdings(df: pd.DataFrame) -> pd.DataFrame:
    """Polars-vectorized computed-column enrichment for holdings DataFrames.

    Converts the pandas DataFrame to a Polars DataFrame once, applies all
    derived-column expressions in a single with_columns pass (one compiled
    expression plan), then converts back to pandas. This replaces ~8 separate
    pd.to_numeric().fillna() + Series-arithmetic sequences with a single
    Polars evaluation — ~2-3× faster for typical 20-50 row per-account frames
    because Polars casts object-dtype columns to Float64 faster than pandas.

    All semantics from the original pandas path are preserved exactly — the
    same fallback priority rules, NaN-vs-0 sentinel distinctions, and the
    broker-value trust hierarchy (use broker column when not-null, fall back
    to our formula otherwise).
    """
    cols = set(df.columns)

    # ── inv_val = avg × opening_qty ──────────────────────────────────
    if {"average_price", "opening_quantity"}.issubset(cols):
        df["inv_val"] = (
            pd.to_numeric(df["average_price"], errors="coerce").fillna(0)
            * pd.to_numeric(df["opening_quantity"], errors="coerce").fillna(0)
        )

    # ── Polars block: pnl, cur_val, pnl_percentage, price_change,
    #    day_change_val ─────────────────────────────────────────────
    # Build a polars frame from the current pandas df (after inv_val).
    # All subsequent derived columns are computed here and written back
    # in one go — avoids repeated from_pandas / to_pandas round-trips.
    cols = set(df.columns)  # refresh after inv_val

    # Gather all expressions we'll compute so we can fire one
    # with_columns() call.
    computed_exprs: list[pl.Expr] = []

    has_ltp    = "last_price"     in cols
    has_avg    = "average_price"  in cols
    has_qty    = "opening_quantity" in cols
    has_close  = "close_price"    in cols
    has_pnl    = "pnl"            in cols
    has_invval = "inv_val"        in cols
    has_dcv    = "day_change_val" in cols

    lf = pl.from_pandas(df, nan_to_null=True)

    if has_ltp and has_avg and has_qty:
        _ltp = _col_f64(lf, "last_price")
        _avg = _col_f64(lf, "average_price")
        _qty = _col_f64(lf, "opening_quantity")
        _pnl_calc = ((_ltp - _avg) * _qty)
        if has_pnl:
            # Reconciled posture: trust broker pnl when not-null.
            _broker_pnl = _col_f64_nullable(lf, "pnl")
            computed_exprs.append(
                pl.when(_broker_pnl.is_not_null())
                .then(_broker_pnl)
                .otherwise(_pnl_calc)
                .alias("pnl")
            )
        else:
            computed_exprs.append(
                pl.when((_ltp > 0) & (_avg > 0))
                .then(_pnl_calc)
                .otherwise(pl.lit(0.0))
                .alias("pnl")
            )
            has_pnl = True  # will exist after this pass

    if has_pnl and has_invval:
        # These depend on pnl, which may have just been (re)written above.
        # We reference the planned alias directly.
        _pnl_expr = pl.col("pnl")
        _inv_expr = _col_f64(lf, "inv_val")
        computed_exprs.append((_inv_expr + _pnl_expr).alias("cur_val"))
        computed_exprs.append(
            pl.when(_inv_expr != 0.0)
            .then(_pnl_expr / _inv_expr * 100.0)
            .otherwise(pl.lit(0.0))
            .alias("pnl_percentage")
        )

    if has_close and has_avg:
        computed_exprs.append(
            (_col_f64(lf, "close_price") - _col_f64(lf, "average_price"))
            .alias("price_change")
        )

    if has_ltp and has_close and has_qty:
        _ltp2 = _col_f64(lf, "last_price")
        _cls  = _col_f64(lf, "close_price")
        _qty2 = _col_f64(lf, "opening_quantity")
        # Day P&L = pnl − overnight_pnl (see original comments for the
        # full derivation — handles still-held / partial-sold / full-sold).
        if has_avg and has_pnl:
            _avg2      = _col_f64(lf, "average_price")
            _pnl2      = _col_f64_nullable(lf, "pnl")
            _overnight = (_cls - _avg2) * _qty2
            _dcv_main  = _pnl2 - _overnight
            _dcv_fallback = (_ltp2 - _cls) * _qty2
            _dcv_calc = (
                pl.when(_pnl2.is_not_null())
                .then(_dcv_main)
                .otherwise(_dcv_fallback)
            )
        else:
            _dcv_calc = (_ltp2 - _cls) * _qty2

        if has_dcv:
            _broker_dcv = _col_f64_nullable(lf, "day_change_val")
            computed_exprs.append(
                pl.when(_broker_dcv.is_not_null())
                .then(_broker_dcv)
                .otherwise(_dcv_calc)
                .alias("day_change_val")
            )
        else:
            computed_exprs.append(
                pl.when((_ltp2 > 0) & (_cls > 0))
                .then(_dcv_calc)
                .otherwise(pl.lit(0.0))
                .alias("day_change_val")
            )
    elif {"day_change", "opening_quantity"}.issubset(cols):
        computed_exprs.append(
            (_col_f64(lf, "day_change") * _col_f64(lf, "opening_quantity"))
            .alias("day_change_val")
        )

    if computed_exprs:
        lf = lf.with_columns(computed_exprs)
        # Write back only the columns that now exist in the polars frame
        # (avoids KeyError if an expression alias didn't fire).
        for c in ("pnl", "inv_val", "cur_val", "pnl_percentage",
                  "price_change", "day_change_val"):
            if c in lf.columns:
                df[c] = lf[c].to_pandas()

    # authorised_date — keep in pandas (datetime parse + strftime is
    # one line; not worth a polars round-trip for a rarely-present column).
    if "authorised_date" in df.columns:
        df["authorised_date"] = pd.to_datetime(
            df["authorised_date"], errors="coerce"
        ).dt.strftime("%d%b%y")

    return df


def _apply_backfill_to_list(
    frames: list[pd.DataFrame],
    qty_col: str = "quantity",
) -> list[pd.DataFrame]:
    """Concatenate `frames`, run `backfill_market_data` on the combined
    frame, then return it as a single-element list.

    This is the Approach A helper — it runs once at the broker boundary
    so the `_RAW_CACHE` always stores post-patch data. Every consumer
    (route + compute_firm_nav + investor slice) therefore reads the same
    cur_val / pnl / day_change_val values without needing independent
    backfill calls.

    Returns the original list unchanged when:
      • the list is empty or all frames are empty (no-op for outage states)
      • backfill raises unexpectedly (safety net — caller gets raw frames)

    The single-element list shape preserves the existing iteration
    contract (`for df in result: ...`) so callers need no migration.
    """
    if not frames:
        return frames
    non_empty = [f for f in frames if not f.empty]
    if not non_empty:
        return frames
    try:
        combined = pd.concat(non_empty, ignore_index=True)
        backfill_market_data(combined)
        return [combined]
    except Exception as _e:
        logger.warning(f"_apply_backfill_to_list: backfill failed, returning raw frames: {_e}")
        return frames


def fetch_positions(*args, **kwargs):
    """Public entry — proxies to conn_service when the cutover flag
    is on, otherwise runs the local @for_all_accounts path.

    Zero-arg results are memoised in `_RAW_CACHE` for `_RAW_TTL_S`
    seconds — see `fetch_holdings` docstring for the rationale.

    Same NAV-consistency guarantee as fetch_holdings (Approach A):
    the cached value is a post-backfill concatenated DataFrame in a
    single-element list. compute_firm_nav reads the same unrealised /
    pnl values as the positions route.
    """
    if not args and not kwargs:
        cached = _raw_cache_get("positions")
        if cached is not None:
            return cached
    if _use_conn_service() and not args and not kwargs:
        from backend.brokers.client import sync as conn_sync
        result = conn_sync.fetch_positions()
        result = _apply_backfill_to_list(result, qty_col="quantity")
        _raw_cache_put("positions", result)
        return result
    result = _fetch_positions_local(*args, **kwargs)
    if not args and not kwargs:
        result = _apply_backfill_to_list(result, qty_col="quantity")
        _raw_cache_put("positions", result)
    return result


@for_all_accounts
def _fetch_positions_local(connections=Connections, account=None, kite=None, broker=None):
    """Multi-broker positions fetch. Same broker-vs-kite resolution
    pattern as fetch_holdings; non-Kite adapters return Kite-shape
    rows via their respective normalisers."""
    df_positions = pd.DataFrame()
    # Circuit-breaker guard.
    if account and _is_circuit_open(account):
        df_positions.attrs["circuit_open"] = True
        df_positions.attrs["fetch_failed"] = True
        logger.warning(
            f"[BREAKER] account={account} short-circuit positions "
            f"(open until {_ts_label(_FETCH_HEALTH.get(account, {}).get('circuit_open_until', 0) or 0)})"
        )
        return df_positions
    try:
        net_rows = None
        if broker is not None:
            resp = broker.positions()
            # broker.positions() returns a Kite-shape dict {net: [...], day: [...]}
            # for every adapter (Kite + Dhan + Groww normalise to this).
            if isinstance(resp, dict):
                net_rows = resp.get("net", [])
            elif isinstance(resp, list):
                net_rows = resp
        elif kite is not None:
            net_rows = kite.positions()["net"]
        if net_rows is None:
            df_positions.attrs['fetch_failed'] = True
            _record_fetch(account, ok=False, error="broker.positions() returned None")
            return df_positions
        df_positions = pd.DataFrame(net_rows)
        _record_fetch(account, ok=True)
        # ── One-time diagnostic — verifies Kite ships day_buy_value in
        # lot-units (lots × price) as `5b995ccb` assumes, NOT in absolute
        # ₹ (lots × multiplier × price). The Jun 26 audit could not
        # confirm Kite's convention from code alone. Fires once per
        # process restart for the first MCX-multiplier row encountered
        # so the operator can sanity-check on the next MCX session
        # without a separate probe. Confirms / falsifies the patch.
        if (not df_positions.empty
                and 'multiplier' in df_positions.columns
                and not _KITE_VALUE_UNIT_LOGGED):
            _diag = df_positions[
                (df_positions['multiplier'] > 1)
                & (df_positions.get('day_buy_quantity', 0) > 0)
            ].head(1)
            if not _diag.empty:
                _r = _diag.iloc[0].to_dict()
                _mult_v   = _r.get('multiplier', 1)
                _dbq_lots = _r.get('day_buy_quantity', 0)  # pre-multiply
                _dbv_raw  = _r.get('day_buy_value', 0)
                _avg      = _r.get('average_price', 0)
                _expected_lot_units  = float(_dbq_lots) * float(_avg)
                _expected_abs_rupees = float(_dbq_lots) * float(_mult_v) * float(_avg)
                logger.warning(
                    f"[KITE-MCX-DIAG] {_r.get('tradingsymbol', '?')} "
                    f"mult={_mult_v} dbq_lots={_dbq_lots} "
                    f"day_buy_value={_dbv_raw:.2f} avg={_avg:.4f} | "
                    f"if≈{_expected_lot_units:.2f} → LOT-UNITS (5b995ccb correct), "
                    f"if≈{_expected_abs_rupees:.2f} → ABSOLUTE ₹ (5b995ccb overcounts {_mult_v}×)"
                )
                globals()['_KITE_VALUE_UNIT_LOGGED'] = True
        if not df_positions.empty and "multiplier" in df_positions.columns:
            # MCX commodities: Kite ships `quantity` in LOTS but
            # `last_price` / `close_price` are per CONTRACT (gram for
            # GOLDM, barrel for CRUDEOIL, etc.) so we multiply qty by
            # `multiplier` (lot_size) to put it in contract units —
            # downstream consumers can do `qty × price = ₹` without
            # caring about the per-instrument lot size.
            #
            # CRITICAL: do the same for overnight_quantity +
            # day_buy_quantity + day_sell_quantity. They land in the
            # decomposed day_pnl formula alongside last_price/close_price
            # so they MUST be in the same unit. Pre-fix, MCX intraday
            # fields stayed in lots and `sq × LTP` was off by `multiplier`
            # — producing the GOLDM146000CE ₹61 537 phantom that pushed
            # the strip's P∆ to ₹1.11 L on a real ~₹50 k day.
            #
            # REVERTED Jun 26 2026 (commit 5b995ccb): the second-pass
            # multiply of `day_buy_value` + `day_sell_value` was based
            # on the assumption Kite ships them as `lots × per_unit_price`
            # (lot-units). Operator empirical data refutes that:
            # GOLDM day net showed −35.68 L in snapshot vs a real
            # ~−35 k, an exact ×100 overcount matching the GOLDM
            # multiplier. Kite actually ships day_buy_value /
            # day_sell_value as ABSOLUTE ₹ (lots × multiplier × price)
            # — the same magnitude as the final answer.
            #
            # So the formula `(_bq × LTP − _bv) + (_sv − _sq × LTP)`
            # is already balanced WITHOUT scaling the value fields:
            #   _bq (post-multiply) × LTP = contract_qty × per-contract LTP = ₹ total
            #   _bv (Kite native)          = ₹ total
            # diff = day_pnl. Correct.
            #
            # Keep multiplying only the quantity fields (the original
            # pre-5b995ccb behaviour that operator confirmed working).
            _mult = df_positions['multiplier']
            df_positions['quantity'] = df_positions['quantity'] * _mult
            for _c in ('overnight_quantity', 'day_buy_quantity', 'day_sell_quantity'):
                if _c in df_positions.columns:
                    df_positions[_c] = df_positions[_c] * _mult

        if not df_positions.empty:
            df_positions["account"] = account
            df_positions["type"] = "P"
    except Exception as e:
        logger.error(f"[{account}] Failed to fetch positions: {e}")
        df_positions.attrs['fetch_failed'] = True
        _record_fetch(account, ok=False, error=str(e))
        return df_positions

    if df_positions.empty:
        return df_positions

    df_positions = _enrich_positions(df_positions)
    return df_positions


def _enrich_positions(df: pd.DataFrame) -> pd.DataFrame:
    """Polars-vectorized computed-column enrichment for positions DataFrames.

    Converts to Polars once, evaluates all P&L and day-change expressions in a
    single with_columns pass, converts back to pandas. Replaces ~10 sequential
    pd.to_numeric().fillna() + Series-arithmetic chains. All semantics preserved:

      • day_change   = LTP − close (cosmetic per-share delta)
      • pnl          = broker value when not-null, else (LTP−avg)×qty
      • day_change_val:
          1. Decomposed intraday formula (full field set) — freezes closed positions
          2. broker.m2m when intraday fields absent
          3. Adapter-shipped day_change_val when present
          4. Naive (LTP−close)×qty otherwise
      • day_change_percentage = day_change_val / |close × qty| × 100
      • pnl_percentage        = pnl / |avg × qty| × 100
    """
    cols = set(df.columns)
    _intraday_fields = {'overnight_quantity', 'day_buy_quantity',
                        'day_sell_quantity', 'day_buy_value', 'day_sell_value'}
    has_intraday = _intraday_fields.issubset(cols)

    lf = pl.from_pandas(df, nan_to_null=True)

    _ltp = _col_f64(lf, 'last_price')
    _avg = _col_f64(lf, 'average_price')
    _cls = _col_f64(lf, 'close_price')
    _qty = _col_f64(lf, 'quantity')

    _pnl_calc = (_ltp - _avg) * _qty

    # ── day_change_val (decomposed intraday formula or fallbacks) ────
    if has_intraday:
        # Canonical formula — see `backend/api/algo/pnl_math.py` for the
        # rationale. `decomposed_intraday_pnl` takes scalars or polars
        # exprs interchangeably (each op is `+ * −` so polars Expr math
        # broadcasts the same way pandas Series math does).
        _oq = _col_f64(lf, 'overnight_quantity')
        _bq = _col_f64(lf, 'day_buy_quantity')
        _sq = _col_f64(lf, 'day_sell_quantity')
        _bv = _col_f64(lf, 'day_buy_value')
        _sv = _col_f64(lf, 'day_sell_value')
        _dcv_calc_expr = decomposed_intraday_pnl(
            _oq, _ltp, _cls, _bq, _bv, _sv, _sq,
        )
        # Validity guard: zero when LTP unhealthy (pre-open warm-up).
        _dcv_expr = pl.when(_ltp > 0).then(_dcv_calc_expr).otherwise(pl.lit(0.0))
    else:
        # Pre-intraday fallback. When close is missing/zero for fresh same-day
        # buys, use (LTP − avg) × qty so the row shows movement since entry.
        _dcv_naive = naive_day_pnl(_ltp, _cls, _qty)
        _dcv_entry = (_ltp - _avg) * _qty
        _dcv_calc_expr = pl.when((_cls <= 0) & (_avg > 0) & (_ltp > 0)).then(_dcv_entry).otherwise(_dcv_naive)
        if 'm2m' in cols:
            _broker_m2m = _col_f64_nullable(lf, 'm2m')
            _dcv_expr = (
                pl.when(_broker_m2m.is_not_null())
                .then(_broker_m2m)
                .otherwise(_dcv_calc_expr)
            )
        elif 'day_change_val' in cols:
            _broker_dcv = _col_f64_nullable(lf, 'day_change_val')
            _dcv_expr = (
                pl.when(_broker_dcv.is_not_null())
                .then(_broker_dcv)
                .otherwise(_dcv_calc_expr)
            )
        else:
            _dcv_expr = pl.when((_ltp > 0) & (_cls > 0)).then(_dcv_calc_expr).otherwise(pl.lit(0.0))

    # ── pnl ──────────────────────────────────────────────────────────
    if 'pnl' in cols:
        _broker_pnl = _col_f64_nullable(lf, 'pnl')
        _pnl_expr = (
            pl.when(_broker_pnl.is_not_null())
            .then(_broker_pnl)
            .otherwise(_pnl_calc)
        )
    else:
        _pnl_expr = (
            pl.when((_ltp > 0) & (_avg > 0))
            .then(_pnl_calc)
            .otherwise(pl.lit(0.0))
        )

    # ── day_change_percentage ─────────────────────────────────────────
    _prev_val = (_cls * _qty).abs()
    _dcp_expr = (
        pl.when(_prev_val != 0.0)
        .then(pl.col("day_change_val") / _prev_val * 100.0)
        .otherwise(pl.lit(0.0))
    )

    # ── pnl_percentage ────────────────────────────────────────────────
    _cost_basis = (_avg * _qty).abs()
    _pnl_pct_expr = (
        pl.when(_cost_basis != 0.0)
        .then(pl.col("pnl") / _cost_basis * 100.0)
        .otherwise(pl.lit(0.0))
    )

    # First pass: day_change, pnl, day_change_val (independent of each other).
    lf = lf.with_columns([
        (_ltp - _cls).alias("day_change"),
        _pnl_expr.alias("pnl"),
        _dcv_expr.alias("day_change_val"),
    ])
    # Second pass: percentages that depend on the first pass results.
    # Contract A note: a position opened TODAY has close_price=0 (no prior
    # session for this symbol), so |close × qty| collapses to 0 and the
    # percent would round to 0. Fall back to |avg × qty| (= notional at
    # entry) so opened-today rows still show a meaningful Day % — the same
    # number the operator computes mentally as `(LTP − entry)/entry × 100`.
    _close_denom = (_cls * _col_f64(lf, 'quantity')).abs()
    _avg_denom   = (_col_f64(lf, 'average_price') * _col_f64(lf, 'quantity')).abs()
    lf = lf.with_columns([
        pl.when(_close_denom != 0.0)
        .then(pl.col("day_change_val") / _close_denom * 100.0)
        .when(_avg_denom != 0.0)
        .then(pl.col("day_change_val") / _avg_denom * 100.0)
        .otherwise(pl.lit(0.0))
        .alias("day_change_percentage"),
        pl.when((_col_f64(lf, 'average_price') * _col_f64(lf, 'quantity')).abs() != 0.0)
        .then(pl.col("pnl") / (_col_f64(lf, 'average_price') * _col_f64(lf, 'quantity')).abs() * 100.0)
        .otherwise(pl.lit(0.0))
        .alias("pnl_percentage"),
    ])

    # Write computed columns back to pandas.
    for c in ("day_change", "pnl", "day_change_val",
              "day_change_percentage", "pnl_percentage"):
        if c in lf.columns:
            df[c] = lf[c].to_pandas()

    return df


def backfill_market_data(df) -> int:
    """Generalised market-data backfill. Operator: "if the fields any
    time available from dhan or groww, it can be backfilled from kite
    using symbol. only cost price is required from the broker."

    Industry pattern (IBKR / Bloomberg PRTU / Sensibull / Streak):
    each row of a multi-broker book is split into two slices —
      account-specific facts → trust the source broker
        (avg_price, quantity, opening_quantity, realised, account)
      market-data facts → one canonical source for the whole book
        (close_price, last_price, day_change_*, instrument identity)

    Kite's `quote()` is the most complete market-data feed across
    Dhan / Groww / Kite, so we route every market-data lookup
    through `PriceBroker.quote()` (which prefers Kite, then falls
    through to Dhan, then Groww via the registry's preference
    order). Source brokers that already populate close_price /
    last_price keep their values — backfill only kicks in on
    zero / missing, never overwriting a non-zero broker value.

    Called by the /api/positions and /api/holdings endpoints AFTER
    `pd.concat(broker_apis.fetch_*())` so the PriceBroker.quote
    call is ONE batched round-trip across every missing-field row
    from every broker — not N per N accounts (the prior shape
    called the lookup inside the per-account `@for_all_accounts`
    body and burned N quote() calls per poll).

    No-op when both close_price and last_price are already
    populated on every row (Kite always returns them; Dhan + Groww
    sometimes don't). Exception-safe: a PriceBroker outage leaves
    rows untouched and downstream P&L fallback behaviour matches
    the pre-patch state.

    Returns the count of patched rows (informational for callers'
    debug logs).
    """
    if df is None or df.empty:
        return 0
    if 'close_price' not in df.columns and 'last_price' not in df.columns:
        return 0
    # A row needs backfill if EITHER close_price or last_price is
    # zero / missing. Unions across both criteria so the single
    # batched quote call covers everything.
    _cls_missing = (pd.to_numeric(df['close_price'], errors='coerce').fillna(0).le(0)
                    if 'close_price' in df.columns
                    else pd.Series(False, index=df.index))
    _ltp_missing = (pd.to_numeric(df['last_price'], errors='coerce').fillna(0).le(0)
                    if 'last_price' in df.columns
                    else pd.Series(False, index=df.index))
    _missing = _cls_missing | _ltp_missing
    if not _missing.any():
        return 0

    # Build unique quote keys across every missing-field row.
    _missing_rows = df[_missing]
    _key_per_row: list[str] = []
    _seen_keys: set[str] = set()
    _unique_keys: list[str] = []
    for _, _row in _missing_rows.iterrows():
        _exch = str(_row.get('exchange', '') or 'NFO').upper()
        _sym  = str(_row.get('tradingsymbol', '') or '').upper()
        if _sym:
            _k = f"{_exch}:{_sym}"
            _key_per_row.append(_k)
            if _k not in _seen_keys:
                _seen_keys.add(_k)
                _unique_keys.append(_k)
        else:
            _key_per_row.append('')

    if not _unique_keys:
        return 0

    _q: dict = {}
    try:
        from backend.brokers.registry import get_price_broker
        _pb = get_price_broker()
        _q = _pb.quote(_unique_keys) or {}
    except Exception as _e:
        logger.warning(
            f"PriceBroker market-data backfill failed (1 batched call for "
            f"{len(_unique_keys)} symbols): {_e}"
        )
        # PriceBroker outage (all brokers rate-limited / token expired).
        # Fall back to KiteTicker for LTP on missing rows so the
        # alternating-0 symptom is suppressed even during cool-off.
        # close_price cannot come from the ticker (no OHLC there) —
        # only last_price is patched here; the day_change_val recompute
        # below handles the rest.
        _q = {}  # no REST quotes; ticker fallback below fills _ltp_lookup
        try:
            from backend.brokers.kite_ticker import get_ticker as _gt
            _ticker_fb = _gt()
            for _k in _unique_keys:
                # key shape is "EXCHANGE:SYMBOL" — strip exchange prefix.
                _sym_fb = _k.split(":", 1)[-1] if ":" in _k else _k
                _ltp_fb = _ticker_fb.get_ltp_by_sym(_sym_fb)
                if _ltp_fb and _ltp_fb > 0:
                    # Synthesise a minimal quote-like entry so the
                    # extraction loop below can populate _ltp_lookup.
                    _q[_k] = {"last_price": _ltp_fb}
                    record_good_ltp(_sym_fb, _ltp_fb)
        except Exception as _te:
            logger.debug(f"PriceBroker backfill: ticker fallback also failed: {_te}")
            # Both PriceBroker and KiteTicker are unavailable.
            # last-good-ltp fallback happens below in the patch loop.
            pass

    # Extract two fields per quote: close (from ohlc.close, fallback
    # to top-level close_price) and last_price (from top-level
    # last_price). Only positive values land in the lookup tables —
    # zeros are treated as "broker didn't have it either".
    _close_lookup: dict[str, float] = {}
    _ltp_lookup: dict[str, float] = {}
    for _k, _v in _q.items():
        if not isinstance(_v, dict):
            continue
        _ohlc = _v.get('ohlc') if isinstance(_v.get('ohlc'), dict) else {}
        _cls_val = _ohlc.get('close') if _ohlc else None
        if _cls_val is None:
            _cls_val = _v.get('close_price')
        try:
            _f_cls = float(_cls_val) if _cls_val is not None else 0.0
        except (TypeError, ValueError):
            _f_cls = 0.0
        if _f_cls > 0:
            _close_lookup[_k] = _f_cls

        _ltp_val = _v.get('last_price')
        try:
            _f_ltp = float(_ltp_val) if _ltp_val is not None else 0.0
        except (TypeError, ValueError):
            _f_ltp = 0.0
        if _f_ltp > 0:
            _ltp_lookup[_k] = _f_ltp
            # Record for last-known-good cache; strip exchange prefix for
            # the canonical symbol-only key used by get_last_good_ltp().
            _sym_only = _k.split(":", 1)[-1] if ":" in _k else _k
            record_good_ltp(_sym_only, _f_ltp)

    # Patch close_price + last_price in place, but ONLY on rows
    # where the source broker came back with 0. Never overwrite a
    # non-zero broker value — Dhan/Groww LTP may be a fresher tick
    # than the snapshot-time Kite quote.
    def _missing_val(value) -> bool:
        try:
            v = float(value)
        except (TypeError, ValueError):
            return True
        if v != v:  # NaN
            return True
        return v <= 0

    _has_close = 'close_price' in df.columns
    _has_ltp   = 'last_price'  in df.columns
    _row_indices = df.index[_missing].tolist()
    _patched_indices: set = set()
    _stale_patched_indices: set = set()  # rows rescued by last-good cache
    _unresolved: list[str] = []
    for _idx, _k in zip(_row_indices, _key_per_row):
        if not _k:
            continue
        _touched = False
        if _has_close and _missing_val(df.at[_idx, 'close_price']):
            _cls_p = _close_lookup.get(_k)
            if _cls_p:
                df.at[_idx, 'close_price'] = _cls_p
                _touched = True
        if _has_ltp and _missing_val(df.at[_idx, 'last_price']):
            _ltp_p = _ltp_lookup.get(_k)
            if _ltp_p:
                df.at[_idx, 'last_price'] = _ltp_p
                _touched = True
            else:
                # Last resort: use the last-known-good LTP from the
                # in-process cache (populated by every previous
                # successful fetch).  Mark the row as stale so
                # callers can surface a staleness indicator.
                _sym_only = _k.split(":", 1)[-1] if ":" in _k else _k
                _cached_ltp = get_last_good_ltp(_sym_only)
                if _cached_ltp is not None:
                    df.at[_idx, 'last_price'] = _cached_ltp
                    _stale_patched_indices.add(_idx)
                    _touched = True
        if _touched:
            _patched_indices.add(_idx)
        elif _k not in _close_lookup and _k not in _ltp_lookup:
            _unresolved.append(_k)

    # Mark rows whose LTP came from the last-known-good cache so routes
    # can propagate the staleness flag to the response schema.
    if _stale_patched_indices and 'last_price_stale' not in df.columns:
        df['last_price_stale'] = False
    for _idx in _stale_patched_indices:
        df.at[_idx, 'last_price_stale'] = True
    if _stale_patched_indices:
        logger.info(
            f"market-data backfill: {len(_stale_patched_indices)} rows rescued "
            f"by last-known-good LTP cache (live sources unavailable)"
        )

    # Diagnostic: log symbols where PriceBroker.quote() returned
    # neither close nor LTP. These rows stay at 0 → Day P&L = 0
    # downstream — the canonical "Dhan Day P&L shows zero while
    # Kite shows non-zero" symptom. When the operator reports it,
    # this log line names the exact symbols that failed so the
    # next step is deterministic (usually: symbol not in Kite
    # instruments cache, or broker quote returned no ohlc).
    if _unresolved:
        logger.warning(
            f"market-data backfill: {len(_unresolved)}/{len(_unique_keys)} "
            f"symbols unresolved by PriceBroker; rows stay at close=0 / "
            f"ltp=0 → Day P&L=0. Unresolved: {_unresolved[:10]}"
            + (f" (+{len(_unresolved)-10} more)" if len(_unresolved) > 10 else "")
        )

    if not _patched_indices:
        return 0

    # Re-run the (LTP - close) × qty recompute on patched rows only.
    # The per-account fetch already wrote a value (0 or broker-
    # reported) the consumer treats as authoritative — overwrite it
    # now that we have real market data.
    _qty_col = 'opening_quantity' if 'opening_quantity' in df.columns else 'quantity'
    if _qty_col not in df.columns or 'last_price' not in df.columns:
        return len(_patched_indices)

    _idx_array = pd.Index(sorted(_patched_indices))
    _ltp_p = pd.to_numeric(df.loc[_idx_array, 'last_price'], errors='coerce').fillna(0)
    _cls_p = pd.to_numeric(df.loc[_idx_array, 'close_price'], errors='coerce').fillna(0)
    _qty_p = pd.to_numeric(df.loc[_idx_array, _qty_col], errors='coerce').fillna(0)
    _dcv_p = (_ltp_p - _cls_p) * _qty_p
    _valid_p = (_ltp_p > 0) & (_cls_p > 0)
    if 'day_change_val' in df.columns:
        df.loc[_idx_array, 'day_change_val'] = _dcv_p.where(
            _valid_p, df.loc[_idx_array, 'day_change_val']
        )
    else:
        df.loc[_idx_array, 'day_change_val'] = _dcv_p.where(_valid_p, 0.0)
    if 'day_change' in df.columns:
        df.loc[_idx_array, 'day_change'] = _ltp_p - _cls_p

    # day_change_percentage rides off close × qty in the consumer's
    # per-account summary. For row-level we update the column
    # directly so the API response is consistent.
    if 'day_change_percentage' in df.columns:
        _prev_val_p = (_cls_p * _qty_p).abs()
        _pct_p = (_dcv_p / _prev_val_p.replace(0, pd.NA) * 100).fillna(0)
        df.loc[_idx_array, 'day_change_percentage'] = _pct_p.where(
            _valid_p, df.loc[_idx_array, 'day_change_percentage']
        )

    # Recompute pnl on rows where LTP was patched and we have a
    # cost basis. The source broker's pnl on those rows was
    # computed against the (broken) zero LTP, so it's typically
    # wrong (= -cost × qty for a long position). Cost basis is
    # the only field we trust the source broker for here — it's
    # the account-specific fact only that broker knows.
    if 'average_price' in df.columns and 'pnl' in df.columns:
        _avg_p = pd.to_numeric(df.loc[_idx_array, 'average_price'], errors='coerce').fillna(0)
        _pnl_calc = (_ltp_p - _avg_p) * _qty_p
        # Include realised when present (positions carry it; holdings
        # typically don't because holdings are open-only).
        if 'realised' in df.columns:
            _rea_p = pd.to_numeric(df.loc[_idx_array, 'realised'], errors='coerce').fillna(0)
            _pnl_calc = _pnl_calc + _rea_p
        _valid_pnl = (_ltp_p > 0) & (_avg_p > 0)
        df.loc[_idx_array, 'pnl'] = _pnl_calc.where(
            _valid_pnl, df.loc[_idx_array, 'pnl']
        )
        # cur_val + pnl_percentage chain off pnl — keep them
        # consistent when present.
        if 'inv_val' in df.columns and 'cur_val' in df.columns:
            _inv_p = pd.to_numeric(df.loc[_idx_array, 'inv_val'], errors='coerce').fillna(0)
            df.loc[_idx_array, 'cur_val'] = _inv_p + df.loc[_idx_array, 'pnl']
            if 'pnl_percentage' in df.columns:
                _pp = (df.loc[_idx_array, 'pnl'] / _inv_p.replace(0, pd.NA) * 100).fillna(0)
                df.loc[_idx_array, 'pnl_percentage'] = _pp

    return len(_patched_indices)


# Back-compat alias — the function used to be narrower (close only).
# Old name still resolves so external scripts / future-refactor
# callers don't break in the same deploy as the rename.
backfill_close_prices = backfill_market_data


def fetch_margins(*args, **kwargs):
    """Public entry — proxies to conn_service when the cutover flag
    is on, otherwise runs the local @for_all_accounts path.

    Zero-arg results are memoised in `_RAW_CACHE` for `_RAW_TTL_S`
    seconds — see `fetch_holdings` docstring for the rationale.
    """
    if not args and not kwargs:
        cached = _raw_cache_get("margins")
        if cached is not None:
            return cached
    if _use_conn_service() and not args and not kwargs:
        from backend.brokers.client import sync as conn_sync
        result = conn_sync.fetch_margins()
        _raw_cache_put("margins", result)
        return result
    result = _fetch_margins_local(*args, **kwargs)
    if not args and not kwargs:
        _raw_cache_put("margins", result)
    return result


@for_all_accounts
def _fetch_margins_local(connections=Connections, account=None, kite=None, broker=None):
    """Multi-broker margins fetch. broker.margins(segment) returns the
    same Kite-shape dict every adapter normalises to."""
    df_margins = pd.DataFrame()
    # Circuit-breaker guard.
    if account and _is_circuit_open(account):
        df_margins.attrs["circuit_open"] = True
        df_margins.attrs["fetch_failed"] = True
        logger.warning(
            f"[BREAKER] account={account} short-circuit margins "
            f"(open until {_ts_label(_FETCH_HEALTH.get(account, {}).get('circuit_open_until', 0) or 0)})"
        )
        return df_margins
    try:
        if broker is not None:
            margins_data = broker.margins(segment="equity")
        elif kite is not None:
            margins_data = kite.margins(segment="equity")
        else:
            return df_margins
        df_margins = pd.DataFrame([margins_data])

        # Flatten 'utilised' if it exists
        if "utilised" in df_margins.columns:
            utilised_df = pd.json_normalize(df_margins["utilised"])
            # Optional: prefix column names
            utilised_df = utilised_df.add_prefix("util ")
            # Drop original nested column and concat flattened
            df_margins = pd.concat([df_margins.drop(columns=["utilised"]), utilised_df], axis=1)

        # Flatten 'available' if needed
        if "available" in df_margins.columns:
            available_df = pd.json_normalize(df_margins["available"])
            available_df = available_df.add_prefix("avail ")
            df_margins = pd.concat([df_margins.drop(columns=["available"]), available_df], axis=1)

        if not df_margins.empty:
            df_margins["account"] = account
            df_margins["type"] = "C"
        _record_fetch(account, ok=True)
    except Exception as e:
        logger.error(f"[{account}] Failed to fetch margins: {e}")
        _record_fetch(account, ok=False, error=str(e))

    return df_margins


# Daily-TTL cache for fetch_holidays. The holiday list only changes once
# per year, but the agent engine's _build_context calls fetch_holidays on
# every run_cycle (every 5 min real-path, every 2 s in sim) — without
# this, every tick fired a blocking HTTP GET to nseindia.com.
# Format: {exchange: (cached_date, set_of_dates)}
_HOLIDAY_CACHE: dict[str, tuple] = {}

# Mapping from Kite exchange names to NSE holiday API segment keys.
# Module-level constant — avoids rebuilding this dict on every cache-miss
# call to fetch_holidays (which runs inside the agent-engine hot path).
_NSE_SEGMENT_MAP: dict[str, str] = {
    "NSE": "CM",
    "BSE": "CM",
    "NFO": "FO",
    "CDS": "CD",
    "MCX": "COM",
}


def fetch_holidays(exchange="NSE"):
    """
    Fetch trading holidays from NSE/MCX official APIs.

    NSE API returns segments: CM (equity cash), FO (F&O), CD (currency), CBM (commodity on BSE).
    MCX holidays are fetched from MCX website.
    Maps exchange param to the right segment.

    Read priority:
      1. holidays_store._MEM_CACHE (Tier 1 of the persistent store) — sync read.
         Populated on the async path by get_or_fetch_holidays().
      2. Module-level _HOLIDAY_CACHE fallback — used when the persistent store
         has not been warmed yet (first cold call from a sync context).
         Falls through to NSE API and fires a background populate as a side-effect.

    This function is sync so it can be called from non-async code (agent engine,
    background tasks). Do NOT make it async — that would require changing every
    callsite throughout the codebase.
    """
    import requests
    from datetime import datetime as dt_datetime, date as dt_date

    exch = exchange.upper().strip()

    # ── Tier 1: check holidays_store memory cache (sync read) ─────────────────
    try:
        from backend.api.persistence.holidays_store import (
            _MEM_CACHE as _hol_mem,
            _ist_year as _hol_year,
        )
        yr = _hol_year()
        hol_key = (exch, yr)
        if hol_key in _hol_mem:
            # Mirror into _HOLIDAY_CACHE so future sync callers get fast path.
            cached_set = _hol_mem[hol_key]
            today = dt_date.today()
            _HOLIDAY_CACHE[exchange] = (today, cached_set)
            return cached_set
    except Exception:
        pass  # persistent store not available — fall through

    # ── Legacy Tier 2: module-level _HOLIDAY_CACHE (daily TTL) ────────────────
    today = dt_date.today()
    cached = _HOLIDAY_CACHE.get(exchange)
    if cached and cached[0] == today:
        return cached[1]

    # ── Legacy Tier 3: NSE API fetch ──────────────────────────────────────────
    holidays: set = set()
    try:
        resp = requests.get(
            "https://www.nseindia.com/api/holiday-master?type=trading",
            headers={"User-Agent": "Mozilla/5.0"}, timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        segment = _NSE_SEGMENT_MAP.get(exchange, "CM")
        entries = data.get(segment, [])

        for h in entries:
            d = h.get("tradingDate", "")
            if d:
                try:
                    holidays.add(dt_datetime.strptime(d, "%d-%b-%Y").date())
                except ValueError:
                    pass
    except Exception:
        pass

    # Cache even on failure (empty set) — avoids retry-hammering nseindia
    # all day if the API is down. Next day's call retries naturally.
    _HOLIDAY_CACHE[exchange] = (today, holidays)

    # Fire-and-forget: populate the persistent store so future restarts
    # hit Tier 1 or Tier 2 instead of calling the API again.
    if holidays:
        _trigger_holidays_store_populate(exch, holidays)

    return holidays


def _trigger_holidays_store_populate(exchange: str, holidays: set) -> None:
    """Schedule a background write to holidays_store from a sync context.

    Schedules on the running event loop if one exists. Silently skips
    in test / import-only contexts where no loop is running.
    """
    try:
        import asyncio as _asyncio
        from backend.api.persistence.holidays_store import (
            _MEM_CACHE as _hol_mem,
            _ist_year as _hol_year,
            _enqueue_db as _hol_enqueue,
        )
        yr = _hol_year()
        key = (exchange, yr)
        # Populate Tier 1 synchronously — it's just a dict write.
        if key not in _hol_mem:
            _hol_mem[key] = holidays
        # Enqueue DB write — also sync (just puts to a queue).
        _hol_enqueue(exchange, yr, holidays)
    except Exception:
        pass  # never let background populate surface to the caller


def update_books(holdings, positions, margins):
    """Return all data combined into one DataFrame (optional)."""
    dfs = [holdings, positions, margins]
    dfs = [df for df in dfs if not df.empty]
    return pd.concat(dfs, ignore_index=True) if dfs else pd.DataFrame()


