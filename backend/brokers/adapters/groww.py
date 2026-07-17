"""
Groww implementation of the `Broker` interface.

Wraps the official `growwapi.GrowwAPI` Python SDK. Auth + method
shapes follow the docs at https://groww.in/trade-api/docs/python-sdk
verbatim — every SDK call below corresponds 1:1 to an example in
those docs:

  * `GrowwAPI.get_access_token(api_key, secret=…)` — API-key flow
  * `GrowwAPI.get_access_token(api_key, totp=…)` — TOTP flow
  * `GrowwAPI(access_token)` — instantiate the client
  * `g.get_user_profile()`, `g.get_holdings_for_user()`,
    `g.get_positions_for_user()`, `g.get_available_margin_details()`
  * `g.get_quote(trading_symbol=, exchange=, segment=)` — single
  * `g.get_ohlc(exchange_trading_symbols=tuple, segment=)` — batch
  * `g.get_ltp(exchange_trading_symbols=tuple, segment=)` — batch
  * `g.get_historical_candles(…)` with `CANDLE_INTERVAL_*` constants
  * `g.place_order(…)`, `g.modify_order(…)`, `g.cancel_order(…)`

Constant *values* below (e.g. `"FNO"`, `"SL_M"`, `"1day"`) are taken
from the installed `growwapi` SDK so passing them via kwargs is
equivalent to passing `GrowwAPI.SEGMENT_FNO` / `ORDER_TYPE_STOP_LOSS_MARKET`
/ `CANDLE_INTERVAL_DAY` directly. Kept as bare strings to avoid an
SDK import at module load.

Response normalisation — Groww's REST shapes differ from Kite's. Each
helper translates Groww field names (`trading_symbol`, `avg_price`,
`ltp`, `order_status`, `groww_order_id`, …) into the Kite-shape
field names the rest of the codebase consumes.
"""

from __future__ import annotations

import functools
import time as _time
import threading
from collections import defaultdict
from typing import Any, Callable

from backend.brokers.base import Broker
from backend.brokers.errors import (
    BrokerAuthError, BrokerRateLimitError, BrokerNetworkError, BrokerError,
)
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


def _groww_exc(e: Exception, status: int | None = None) -> BrokerError:
    """Wrap a Groww exception in the typed BrokerError hierarchy."""
    if status == 401:
        return BrokerAuthError(str(e), broker="groww", status=status)
    if status == 429:
        return BrokerRateLimitError(str(e), broker="groww", status=status)
    if status in (502, 503, 504):
        return BrokerNetworkError(str(e), broker="groww", status=status)
    return BrokerError(str(e), broker="groww", status=status)


# ── Auth-retry decorator ──────────────────────────────────────────────
#
# Mirrors Kite's `@retry_kite_conn` pattern but extended to the full
# Groww exception hierarchy:
#
#   Authentication (401) / Authorisation (403)
#     → evict token, re-mint via TOTP, retry ONCE.
#
#   RateLimit (429)
#     → exponential backoff: 1 / 2 / 4 / 8 s (capped at 30 s), up to
#       4 retries (5 total attempts). Do NOT re-mint — the token is valid.
#       Logs [GROWW-RATE-LIMIT] at WARN. After 4 retries, re-raises so
#       broker_apis records ok=False (amber health badge).
#
#   Timeout (504)
#     → retry ONCE with a fresh HTTP session (no re-mint). Logs
#       [GROWW-TIMEOUT] at WARN. After 1 retry, re-raises.
#
#   BadRequest (400) / NotFound (404) / other
#     → re-raise immediately — caller's bug, no retry.
#
# Resolved once at module load — moves the SDK lookup off the hot path.
# Empty tuples make isinstance() always False until growwapi is installed.

_GROWW_RATE_LIMIT_MAX_RETRIES: int = 4
_GROWW_RATE_LIMIT_BASE_SLEEP_S: float = 1.0
_GROWW_RATE_LIMIT_SLEEP_CAP_S: float = 30.0

try:
    from growwapi.groww.exceptions import (  # type: ignore[import-not-found]
        GrowwAPIAuthenticationException,
        GrowwAPIAuthorisationException,
        GrowwAPIRateLimitException,
        GrowwAPITimeoutException,
    )
    # Split authentication (401, token bad) from authorisation (403, entitlement
    # denied) so inline catch blocks can handle each separately without a
    # string-name type check.
    _GROWW_AUTHN_EXC: tuple = (GrowwAPIAuthenticationException,)
    _GROWW_AUTHZ_EXC: tuple = (GrowwAPIAuthorisationException,)
    # Combined tuple kept for the decorator's single re-mint branch (both
    # 401 and 403 warrant a token refresh attempt).
    _GROWW_AUTH_EXC: tuple = (
        GrowwAPIAuthenticationException, GrowwAPIAuthorisationException,
    )
    _GROWW_RATE_EXC: tuple = (GrowwAPIRateLimitException,)
    _GROWW_TIMEOUT_EXC: tuple = (GrowwAPITimeoutException,)
except ImportError:
    _GROWW_AUTHN_EXC = ()
    _GROWW_AUTHZ_EXC = ()
    _GROWW_AUTH_EXC = ()
    _GROWW_RATE_EXC = ()
    _GROWW_TIMEOUT_EXC = ()


def _retry_groww_auth(fn: Callable) -> Callable:
    """Wraps every GrowwBroker method with: (a) the per-account
    source-IP ContextVar bound for the duration of the SDK call so
    the patched `requests` module routes through a source-bound
    session (see `_install_groww_source_binding` in connections.py),
    and (b) structured retry handling for the full Groww exception
    hierarchy — auth re-mint, rate-limit backoff, timeout retry."""
    @functools.wraps(fn)
    def wrapper(self: "GrowwBroker", *args, **kwargs):
        from backend.brokers.connections import (
            _GROWW_SOURCE_IP_OVERRIDE,
        )
        ip = getattr(self._conn, "_source_ip", None)
        token = _GROWW_SOURCE_IP_OVERRIDE.set(ip) if ip else None
        try:
            # ── First attempt ─────────────────────────────────────
            try:
                return fn(self, *args, **kwargs)

            # Auth / Authorisation → re-mint once
            except _GROWW_AUTH_EXC as e:  # type: ignore[misc]
                logger.warning(
                    f"GrowwBroker.{fn.__name__} for {self.account!r} hit "
                    f"{type(e).__name__}: {e}. Evicting cached access token "
                    f"and re-minting via TOTP."
                )
                self._conn.refresh()
                return fn(self, *args, **kwargs)

            # RateLimit → exponential backoff up to 4 retries (5 total attempts)
            except _GROWW_RATE_EXC as e:  # type: ignore[misc]
                sleep_s = _GROWW_RATE_LIMIT_BASE_SLEEP_S
                for attempt in range(1, _GROWW_RATE_LIMIT_MAX_RETRIES + 1):
                    logger.warning(
                        f"[GROWW-RATE-LIMIT] GrowwBroker.{fn.__name__} for "
                        f"{self.account!r} — attempt {attempt}/"
                        f"{_GROWW_RATE_LIMIT_MAX_RETRIES}, "
                        f"sleeping {sleep_s:.0f}s: {e}"
                    )
                    _time.sleep(sleep_s)
                    sleep_s = min(sleep_s * 2, _GROWW_RATE_LIMIT_SLEEP_CAP_S)
                    try:
                        return fn(self, *args, **kwargs)
                    except _GROWW_RATE_EXC as e2:  # type: ignore[misc]
                        e = e2
                        if attempt == _GROWW_RATE_LIMIT_MAX_RETRIES:
                            raise
                    except Exception:
                        raise
                raise  # unreachable but satisfies type checker

            # Timeout → retry once with a fresh HTTP session
            except _GROWW_TIMEOUT_EXC as e:  # type: ignore[misc]
                logger.warning(
                    f"[GROWW-TIMEOUT] GrowwBroker.{fn.__name__} for "
                    f"{self.account!r} timed out — retrying once: {e}"
                )
                try:
                    self._conn.refresh()
                except Exception:
                    pass  # refresh_session is best-effort; proceed regardless
                return fn(self, *args, **kwargs)

        finally:
            if token is not None:
                _GROWW_SOURCE_IP_OVERRIDE.reset(token)
    return wrapper


# ── Entitlement-denied counter ────────────────────────────────────────
#
# Tracks Groww "Access forbidden" (GrowwAPIAuthorisationException) hits
# on quote/ltp paths that indicate the account lacks a market-data
# entitlement for a particular segment — distinct from a bad token.
#
# Shape: {account: {segment: count}}
# Thread-safe via _ENTITLEMENT_LOCK.
# Exposed via GET /api/admin/broker-health in the per-account `extra`
# field. Process restart clears counts (in-memory only).

_entitlement_denied: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
_ENTITLEMENT_LOCK = threading.Lock()


def record_entitlement_denied(account: str, segment: str) -> None:
    """Increment the per-account/segment entitlement-denied counter."""
    with _ENTITLEMENT_LOCK:
        _entitlement_denied[account][segment] += 1


def get_entitlement_denied_snapshot() -> dict[str, dict[str, int]]:
    """Return a deepcopy-safe snapshot of the current denied counters."""
    with _ENTITLEMENT_LOCK:
        return {
            acct: dict(segs)
            for acct, segs in _entitlement_denied.items()
        }


# Kite → Groww exchange string. Groww uses NSE / BSE / NFO directly so
# most of these are passthrough; the table makes intent explicit and
# keeps room for renames if Groww ever changes its constants.
_EXCHANGE_TO_GROWW: dict[str, str] = {
    "NSE": "NSE",
    "BSE": "BSE",
    "NFO": "NSE",  # Groww uses segment to distinguish F&O — exchange stays NSE
    "BFO": "BSE",
    "MCX": "MCX",
    "CDS": "NSE",
    "BCD": "BSE",
}

# Kite exchange → Groww segment. Groww splits CASH vs FNO vs COMMODITY
# vs CURRENCY at the segment level (the SDK's SEGMENT_* constants).
_SEGMENT_TO_GROWW: dict[str, str] = {
    "NSE": "CASH",
    "BSE": "CASH",
    "NFO": "FNO",
    "BFO": "FNO",
    "MCX": "COMMODITY",
    "CDS": "CURRENCY",
    "BCD": "CURRENCY",
}

# Kite product → Groww product. Same strings for CNC/MIS/NRML; MTF
# carried through for delivery-with-margin.
_PRODUCT_TO_GROWW: dict[str, str] = {
    "CNC":  "CNC",
    "MIS":  "MIS",
    "NRML": "NRML",
    "MTF":  "MTF",
}

# Our exchange vocabulary → Groww's segment codes for the market-status
# probe. Multiple Groww codes per our single code; ANY mapped segment
# reporting active means the exchange is open. Module-level constant
# (slice M6) — was previously a local inside `market_status()` and
# re-allocated per call.
_XCHG_TO_GROWW_MARKET_STATUS: dict[str, tuple[str, ...]] = {
    "NSE": ("NSE", "NSE_EQ"),
    "BSE": ("BSE", "BSE_EQ"),
    "NFO": ("NFO", "NSE_FO", "NSE_FNO"),
    "BFO": ("BFO", "BSE_FO", "BSE_FNO"),
    "CDS": ("CDS", "NSE_CURRENCY"),
    "MCX": ("MCX", "MCX_COMM"),
}

_GROWW_OPEN_STATUS_STRINGS = frozenset({"OPEN", "TRADING", "ACTIVE", "Y", "YES", "TRUE"})


def _first(d: dict, *keys: str, default: Any = "") -> Any:
    """Return the first non-falsy value for `keys` from dict `d`, or `default`."""
    for k in keys:
        v = d.get(k)
        if v:
            return v
    return default


def _groww_status_row_for_code(data: dict, code: str) -> dict | None:
    """Build one status row for a segment code from a flat-dict response."""
    v = data.get(code) or data.get(code.lower())
    if isinstance(v, dict):
        return {"segment": code, **v}
    if isinstance(v, (str, bool)):
        return {"segment": code, "status": v}
    return None


def _extract_groww_status_rows(resp: Any, target_codes: tuple[str, ...]) -> list[dict] | None:
    """Coerce Groww's market-status response into a flat list of rows.

    Accepts the documented list envelope AND the flat-dict-by-segment
    alternate observed across SDK versions. Returns None when the shape
    is unparseable so the caller can fall through to the next probe."""
    if not isinstance(resp, dict):
        return None
    data = resp.get("data") or resp.get("payload") or resp
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        return [row for code in target_codes
                if (row := _groww_status_row_for_code(data, code)) is not None]
    return None


def _groww_row_indicates_open(row: dict, target_codes: tuple[str, ...]) -> bool:
    """Return True when this row's segment matches AND its status
    reads as open (boolean True or one of the accepted strings)."""
    seg = str(row.get("segment") or row.get("exchange") or "").upper()
    if seg not in target_codes:
        return False
    st = row.get("status") or row.get("trading_status")
    if isinstance(st, bool):
        return st
    if isinstance(st, str):
        return st.upper() in _GROWW_OPEN_STATUS_STRINGS
    return False

# Kite order_type → Groww order_type. Values match GrowwAPI constants:
#   GrowwAPI.ORDER_TYPE_MARKET             = "MARKET"
#   GrowwAPI.ORDER_TYPE_LIMIT              = "LIMIT"
#   GrowwAPI.ORDER_TYPE_STOP_LOSS          = "SL"        (NOT "STOP_LOSS")
#   GrowwAPI.ORDER_TYPE_STOP_LOSS_MARKET   = "SL_M"      (NOT "STOP_LOSS_MARKET")
# Earlier mapping used the constant *names* as values — Groww would
# reject any SL / SL-M order placed through this path.
_ORDER_TYPE_TO_GROWW: dict[str, str] = {
    "MARKET": "MARKET",
    "LIMIT":  "LIMIT",
    "SL":     "SL",
    "SL-M":   "SL_M",
}

# Kite-style candle interval → Groww `CANDLE_INTERVAL_*` value.
# Values match the SDK constants:
#   CANDLE_INTERVAL_DAY     = "1day"
#   CANDLE_INTERVAL_HOUR_1  = "1hour"
#   CANDLE_INTERVAL_MIN_5   = "5minute"   (and 1/2/3/10/15/30)
#   CANDLE_INTERVAL_WEEK    = "1week"
#   CANDLE_INTERVAL_MONTH   = "1month"
_INTERVAL_TO_GROWW: dict[str, str] = {
    "minute":         "1minute",
    "1minute":        "1minute",
    "2minute":        "2minute",
    "3minute":        "3minute",
    "5minute":        "5minute",
    "10minute":       "10minute",
    "15minute":       "15minute",
    "30minute":       "30minute",
    "hour":           "1hour",
    "60minute":       "1hour",
    "1hour":          "1hour",
    "4hour":          "4hour",
    "240minute":      "4hour",
    "day":            "1day",
    "1day":           "1day",
    "week":           "1week",
    "1week":          "1week",
    "month":          "1month",
    "1month":         "1month",
}


def _groww_exchange_and_segment(kite_exchange: str) -> tuple[str, str]:
    """Translate a Kite-style exchange to Groww's (exchange, segment) pair."""
    ex = _EXCHANGE_TO_GROWW.get(kite_exchange)
    seg = _SEGMENT_TO_GROWW.get(kite_exchange)
    if not ex or not seg:
        raise ValueError(f"No Groww exchange/segment mapping for {kite_exchange!r}")
    return ex, seg


def _groww_coerce_date(d: Any) -> str:
    """Coerce a date/datetime/string to Groww's expected 'YYYY-MM-DD HH:MM:SS' format."""
    if hasattr(d, "strftime"):
        return d.strftime("%Y-%m-%d %H:%M:%S")
    return str(d)


def _groww_hist_candle_row(row: Any) -> dict | None:
    """Convert a Groww OHLCV candle list/tuple to a Kite-shape dict.
    Returns None when the row is too short to be valid."""
    if not isinstance(row, (list, tuple)) or len(row) < 6:
        return None
    return {
        "date":   row[0],
        "open":   float(row[1] or 0),
        "high":   float(row[2] or 0),
        "low":    float(row[3] or 0),
        "close":  float(row[4] or 0),
        "volume": int(row[5] or 0),
    }


def _groww_instrument_row(r: dict) -> dict:
    """Map a single Groww instrument record to Kite-shape instrument dict."""
    return {
        "instrument_token": _first(r, "exchange_token", "instrument_token"),
        "tradingsymbol":    _first(r, "trading_symbol", "tradingsymbol"),
        "name":             r.get("name") or r.get("groww_symbol", ""),
        "exchange":         r.get("exchange") or "",
        "segment":          r.get("segment") or "",
        "instrument_type":  r.get("instrument_type") or "",
        "expiry":           r.get("expiry") or "",
        "strike":           float(r.get("strike", 0) or 0),
        "lot_size":         int(r.get("lot_size", 0) or 0),
        "tick_size":        float(r.get("tick_size", 0) or 0),
        "_raw":             r,
    }


def _groww_build_gtt_order_leg(r: dict, order_inner: dict) -> dict:
    """Build the single-leg orders list entry for a Groww GTT row."""
    return {
        "transaction_type": order_inner.get("transaction_type") or "SELL",
        "quantity":         int(r.get("quantity") or 0),
        "price":            float(order_inner.get("price") or 0),
        "order_type":       order_inner.get("order_type") or "LIMIT",
        "product":          r.get("product_type") or "NRML",
    }


def _groww_margin_available(data: dict) -> dict:
    """Build the `available` sub-dict from Groww margin data."""
    return {
        "adhoc_margin":    float(data.get("adhoc_margin", 0) or 0),
        "cash":            float(_first(data, "available_balance", "cash", default=0)),
        "opening_balance": float(data.get("opening_balance", 0) or 0),
        "live_balance":    float(data.get("available_balance", 0) or 0),
        "collateral":      float(data.get("collateral", 0) or 0),
        "intraday_payin":  float(data.get("intraday_payin", 0) or 0),
    }


def _groww_margin_utilised(data: dict) -> dict:
    """Build the `utilised` sub-dict from Groww margin data."""
    return {
        "debits":            float(data.get("utilised", 0) or 0),
        "exposure":          float(data.get("exposure_margin", 0) or 0),
        "m2m_realised":      float(data.get("realised_pnl", 0) or 0),
        "m2m_unrealised":    float(data.get("unrealised_pnl", 0) or 0),
        "option_premium":    float(data.get("option_premium", 0) or 0),
        "payout":            float(data.get("payout", 0) or 0),
        "span":              float(data.get("span_margin", 0) or 0),
        "holding_sales":     0.0,
        "turnover":          0.0,
        "liquid_collateral": 0.0,
        "stock_collateral":  float(data.get("stock_collateral", 0) or 0),
    }


def _groww_order_row(o: dict) -> dict:
    """Normalise one Groww order record to Kite-shape."""
    return {
        "order_id":           str(_first(o, "groww_order_id", "order_id")),
        "tradingsymbol":      _first(o, "trading_symbol", "tradingsymbol"),
        "exchange":           o.get("exchange") or "",
        "status":             _order_status(o),
        "transaction_type":   o.get("transaction_type") or "BUY",
        "order_type":         o.get("order_type") or "MARKET",
        "product":            o.get("product") or "NRML",
        "quantity":           _gi(o, "quantity"),
        "filled_quantity":    _gi(o, "filled_quantity"),
        "pending_quantity":   _gi(o, "remaining_quantity", "pending_quantity"),
        "price":              _gf(o, "price"),
        "trigger_price":      _gf(o, "trigger_price"),
        "average_price":      _gf(o, "average_price", "filled_avg_price"),
        "order_timestamp":    _first(o, "created_at", "order_timestamp"),
        "exchange_timestamp": o.get("exchange_time") or "",
        "status_message":     _first(o, "remark", "status_message"),
        "_raw":               o,
    }


def _groww_gtt_unpack_order(
    orders: list[dict], trigger_values: list[float]
) -> tuple:
    """Unpack the first order dict + first trigger value into primitives."""
    order0  = orders[0] if orders else {}
    qty0    = int(order0.get("quantity", 0))
    price0  = float(order0.get("price") or 0)
    otype0  = _ORDER_TYPE_TO_GROWW.get(order0.get("order_type", "LIMIT"), "LIMIT")
    txn0    = order0.get("transaction_type", "SELL")
    product = _PRODUCT_TO_GROWW.get(order0.get("product", "NRML"), "NRML")
    trig    = float(trigger_values[0]) if trigger_values else 0.0
    return order0, qty0, price0, otype0, txn0, product, trig


def _groww_gtt_order_body(txn: str, otype: str, price: float) -> dict:
    """Build the Groww Smart Order `order` sub-dict (LIMIT includes price)."""
    body: dict[str, Any] = {"transaction_type": txn, "order_type": otype}
    if otype == "LIMIT" and price > 0:
        body["price"] = price
    return body


class GrowwBroker(Broker):
    """Groww adapter. See module docstring for the auth + normalisation
    contract."""

    def __init__(self, conn: "GrowwConnection") -> None:  # type: ignore[name-defined]
        super().__init__()
        self._conn = conn

    # ── Identity + escape hatch ───────────────────────────────────────

    @property
    def account(self) -> str:
        return self._conn.account

    @property
    def broker_id(self) -> str:
        return "groww"

    @property
    def groww(self):
        """Underlying `GrowwAPI` SDK handle. Escape hatch for SDK
        features not lifted into the Broker ABC."""
        return self._conn.get_groww_conn()

    # ── Account state ─────────────────────────────────────────────────

    @_retry_groww_auth
    def profile(self) -> dict:
        try:
            prof = self.groww.get_user_profile()
            data = prof.get("data") if isinstance(prof, dict) else None
            if not isinstance(data, dict):
                data = prof if isinstance(prof, dict) else {}
            return {
                "user_id":   data.get("user_id")   or data.get("userId")   or "",
                "user_name": data.get("user_name") or data.get("name")     or "Groww user",
                "email":     data.get("email")     or "",
                "broker":    "GROWW",
                "data":      data,
            }
        except Exception as e:
            raise RuntimeError(f"Groww auth check failed: {e}") from e

    @_retry_groww_auth
    def holdings(self) -> list[dict]:
        resp = self.groww.get_holdings_for_user()
        return _normalise_holdings(resp)

    @_retry_groww_auth
    def positions(self) -> dict:
        resp = self.groww.get_positions_for_user()
        return _normalise_positions(resp)

    @_retry_groww_auth
    def margins(self, segment: str | None = None) -> dict:
        resp = self.groww.get_available_margin_details()
        # Audit cycle 8 — one-time INFO log of the raw Groww margin
        # response keys per account, mirroring the Dhan adapter pattern.
        # Confirms which of the optimistically-mapped fields
        # (realised_pnl, option_premium, etc.) actually arrive.
        global _GROWW_MARGINS_LOGGED
        try:
            if self.account not in _GROWW_MARGINS_LOGGED:
                _GROWW_MARGINS_LOGGED.add(self.account)
                _raw = resp if isinstance(resp, dict) else None
                logger.info(
                    f"Groww margins[{self.account}] raw response keys: "
                    f"{sorted((_raw or {}).keys()) if isinstance(_raw, dict) else type(resp).__name__}"
                )
        except Exception:
            pass
        return _normalise_margins(resp, segment)

    @_retry_groww_auth
    def orders(self) -> list[dict]:
        resp = self.groww.get_order_list()
        return _normalise_orders(resp)

    @_retry_groww_auth
    def order_status(self, order_id: str) -> dict:
        """Audit fix (M-1) — per-id status endpoint. Pre-fix this fell
        back to the ABC default (filter `orders()`) which fetched the
        entire day book on every chase tick.

        Uses Groww SDK's `get_order_detail` / `get_order_status_by_id`
        (whichever the installed SDK version exposes). Falls back to
        the ABC default when neither method exists. Returns Kite-shape
        via `_normalise_orders` so the chase loop downstream parses
        the result the same way regardless of SDK version."""
        sdk = self.groww
        single_fn = (getattr(sdk, "get_order_detail", None)
                     or getattr(sdk, "get_order_status_by_id", None)
                     or getattr(sdk, "get_order_by_id", None))
        if single_fn is None:
            return super().order_status(order_id)
        try:
            resp = single_fn(str(order_id))
        except Exception as e:
            logger.debug(f"GrowwBroker.order_status({order_id}) failed: {e}")
            return {}
        rows = _normalise_orders(resp)
        return rows[0] if rows else {}

    def trades(self) -> list[dict]:
        """Groww exposes per-order trade lookup (`get_trade_list_for_order`)
        but no day-wide trade book endpoint. Stub returns [] so callers
        that aggregate trades across accounts (daily snapshot) skip
        Groww cleanly until per-order rollup ships."""
        logger.debug(f"GrowwBroker.trades(): day-wide trade book unavailable; "
                     f"returning []. Use get_trade_list_for_order(order_id) "
                     f"for per-order detail.")
        return []

    # ── Market data ───────────────────────────────────────────────────

    @_retry_groww_auth
    def _ltp_fetch_segment(self, seg: str, keys: list[str], out: dict) -> None:
        """Fetch LTP for one segment and populate `out` with Kite-shape entries."""
        try:
            resp = self.groww.get_ltp(tuple(keys), segment=seg)
            data = resp.get("data") if isinstance(resp, dict) else {}
            if isinstance(data, dict):
                for k, v in data.items():
                    out[k.replace("_", ":", 1)] = {"last_price": float(v or 0)}
        except _GROWW_AUTHN_EXC:  # type: ignore[misc]
            raise
        except _GROWW_AUTHZ_EXC as e:  # type: ignore[misc]
            logger.info(
                f"[GROWW-ENTITLEMENT] GrowwBroker.ltp for {self.account!r}: "
                f"Access forbidden on segment={seg!r}: {e}"
            )
            record_entitlement_denied(self.account, seg)
        except Exception as e:
            logger.debug(f"Groww ltp segment={seg}: {e}")

    def ltp(self, symbols: list[str]) -> dict:
        """Groww's `get_ltp` wants a Tuple of `"EXCHANGE_TRADINGSYMBOL"` keys
        plus a segment. The codebase passes Kite-style `"NSE:RELIANCE"`
        strings. Translate inline so PriceBroker can fail over without
        an instruments-cache lookup."""
        if not symbols:
            return {}
        by_seg: dict[str, list[str]] = {}
        for sym in symbols:
            if ":" not in sym:
                continue
            exch, ts = sym.split(":", 1)
            try:
                _, seg = _groww_exchange_and_segment(exch)
            except ValueError:
                continue
            by_seg.setdefault(seg, []).append(f"{exch}_{ts}")
        out: dict[str, dict] = {}
        for seg, keys in by_seg.items():
            self._ltp_fetch_segment(seg, keys, out)
        return out

    @_retry_groww_auth
    def quote(self, symbols: list[str]) -> dict:
        """Two-tier quote fetch:
          * **Single symbol** — call `get_quote(trading_symbol, exchange,
            segment)` for the full Kite-shape row (depth + OI + Greeks).
          * **Batch** (>1 symbol) — call `get_ohlc(exchange_trading_symbols,
            segment)` once per segment (Groww supports up to 50 keys
            per call, much faster than looping `get_quote`).
        Both paths return the same `{"NSE:RELIANCE": {...}}` shape so
        callers don't branch on batch size."""
        if not symbols:
            return {}
        if len(symbols) == 1:
            return self._quote_single(symbols[0])
        return self._quote_batch_ohlc(symbols)

    def _quote_single(self, sym: str) -> dict:
        """Single-symbol path — richer payload via `get_quote`."""
        if ":" not in sym:
            return {}
        out: dict[str, dict] = {}
        try:
            exch, ts = sym.split(":", 1)
            _, seg = _groww_exchange_and_segment(exch)
            resp = self.groww.get_quote(trading_symbol=ts, exchange=exch,
                                        segment=seg)
            data = resp.get("data") if isinstance(resp, dict) else {}
            if isinstance(data, dict):
                out[sym] = _normalise_quote_row(data)
        except _GROWW_AUTHN_EXC:  # type: ignore[misc]
            # Authentication (401) after decorator re-mint — still failing.
            # Re-raise so broker_apis records ok=False.
            raise
        except _GROWW_AUTHZ_EXC as e:  # type: ignore[misc]
            # Authorisation (403) = partial-entitlement; log at INFO and
            # count only the specific segment that was denied.
            exch_part = sym.split(":", 1)[0] if ":" in sym else sym
            try:
                _, seg = _groww_exchange_and_segment(exch_part)
            except ValueError:
                seg = exch_part
            logger.info(
                f"[GROWW-ENTITLEMENT] GrowwBroker._quote_single {sym!r} "
                f"for {self.account!r}: Access forbidden (seg={seg}): {e}"
            )
            record_entitlement_denied(self.account, seg)
        except Exception as e:
            logger.debug(f"GrowwBroker._quote_single skipping {sym}: {e}")
        return out

    def _ohlc_fetch_segment(
        self, seg: str, kite_keys: list[str], groww_keys: tuple, out: dict
    ) -> None:
        """Fetch OHLC for one segment and populate `out` with Kite-shape entries."""
        try:
            resp = self.groww.get_ohlc(exchange_trading_symbols=groww_keys, segment=seg)
            data = resp.get("data") if isinstance(resp, dict) else resp
            if not isinstance(data, dict):
                return
            for kite_key, gk in zip(kite_keys, groww_keys):
                row = data.get(gk) or {}
                if isinstance(row, dict):
                    out[kite_key] = _normalise_quote_row(row)
        except _GROWW_AUTHN_EXC:  # type: ignore[misc]
            raise
        except _GROWW_AUTHZ_EXC as e:  # type: ignore[misc]
            logger.info(
                f"[GROWW-ENTITLEMENT] GrowwBroker._quote_batch_ohlc "
                f"for {self.account!r} segment={seg}: Access forbidden: {e}"
            )
            record_entitlement_denied(self.account, seg)
        except Exception as e:
            logger.debug(f"GrowwBroker._quote_batch_ohlc segment={seg}: {e}")

    def _quote_batch_ohlc(self, symbols: list[str]) -> dict:
        """Batch path — one `get_ohlc(exchange_trading_symbols=tuple,
        segment=…)` call per segment. Output preserves Kite's quote-row
        shape (`last_price`, `ohlc.*`, `volume`, …) but omits depth and
        OI — callers needing those use `_quote_single` instead."""
        by_seg: dict[str, list[tuple[str, str]]] = {}
        for sym in symbols:
            if ":" not in sym:
                continue
            exch, ts = sym.split(":", 1)
            try:
                _, seg = _groww_exchange_and_segment(exch)
            except ValueError:
                continue
            by_seg.setdefault(seg, []).append((sym, f"{exch}_{ts}"))
        out: dict[str, dict] = {}
        for seg, pairs in by_seg.items():
            self._ohlc_fetch_segment(
                seg, [p[0] for p in pairs], tuple(p[1] for p in pairs), out
            )
        return out

    @_retry_groww_auth
    def instruments(self, exchange: str | None = None) -> list[dict]:
        """Groww's `get_all_instruments()` returns the master CSV as a
        list. Field names are close to Kite's but not identical — map
        the columns the codebase reads off this list."""
        resp = self.groww.get_all_instruments()
        rows = resp if isinstance(resp, list) else resp.get("data", []) \
            if isinstance(resp, dict) else []
        return [
            _groww_instrument_row(r)
            for r in rows
            if not (exchange and r.get("exchange") != exchange)
        ]

    @_retry_groww_auth
    def historical_data(
        self,
        instrument_token: int,
        from_date: Any,
        to_date: Any,
        interval: str = "day",
        *,
        trading_symbol: str | None = None,
        exchange: str | None = None,
        segment: str | None = None,
    ) -> list[dict]:
        """Groww's `get_historical_candles` is keyed by trading_symbol /
        exchange / segment, not by instrument_token. Callers wiring this
        path must pass `trading_symbol`, `exchange` (and optionally
        `segment`); otherwise the call short-circuits since Groww has
        no token→symbol lookup parallel to Kite's. Date params accept
        ISO strings (`"YYYY-MM-DD HH:MM:SS"`) or `datetime` objects;
        we coerce to Groww's expected format below.

        Returns a list of Kite-shape candle dicts:
            [{"date": …, "open": …, "high": …, "low": …,
              "close": …, "volume": …}, …]
        """
        if not trading_symbol or not exchange:
            # PriceBroker / get_historical_brokers call this signature
            # by position with just an instrument_token — Groww has no
            # token→symbol lookup parallel to Kite's. Return empty
            # bars silently so the fallback chain walks to the next
            # adapter without raising. Earlier this raised ValueError,
            # generating WARNING-level log spam every time a quote /
            # historical lookup went through the failover chain.
            logger.debug(
                f"GrowwBroker.historical_data: no trading_symbol/exchange "
                f"provided (token={instrument_token}) — returning [] for PriceBroker fallback"
            )
            return []
        ex, seg = _groww_exchange_and_segment(exchange)
        seg = segment or seg
        groww_interval = _INTERVAL_TO_GROWW.get(interval.lower())
        if not groww_interval:
            raise ValueError(
                f"Unknown candle interval {interval!r}. Supported: "
                f"{sorted(set(_INTERVAL_TO_GROWW.values()))}"
            )
        resp = self.groww.get_historical_candles(
            trading_symbol=trading_symbol,
            exchange=ex,
            segment=seg,
            start_time=_groww_coerce_date(from_date),
            end_time=_groww_coerce_date(to_date),
            interval=groww_interval,
        )
        data = resp.get("data") if isinstance(resp, dict) else resp
        # Two shapes documented: `{"candles": [[ts,o,h,l,c,v], …]}` or
        # `[[ts,o,h,l,c,v], …]`. Tolerate both.
        candles: list = []
        if isinstance(data, dict):
            candles = data.get("candles", [])
        elif isinstance(data, list):
            candles = data
        return [row_d for row in candles
                if (row_d := _groww_hist_candle_row(row)) is not None]

    def holidays(self, exchange: str) -> set[str]:
        """Groww doesn't publish a holidays endpoint. Empty set so
        PriceBroker falls over to Kite without an exception trace."""
        return set()

    @_retry_groww_auth
    def market_status(self, exchange: str) -> bool | None:
        """Probe Groww's market-status endpoint for `exchange`.
        Returns True / False / None per the Broker ABC contract.
        SDK support is uncertain across versions — probe known
        method names and gracefully None on miss.

        Maps our exchange codes (NSE / BSE / NFO / BFO / MCX / CDS)
        to Groww's segment vocabulary. Same shape semantics as Dhan:
        ANY mapped segment reporting active means the exchange is
        open."""
        resp = self._call_market_status_sdk(exchange)
        if resp is None:
            return None
        target_codes = _XCHG_TO_GROWW_MARKET_STATUS.get((exchange or "").upper())
        if not target_codes:
            return None
        rows = _extract_groww_status_rows(resp, target_codes)
        if rows is None:
            # SDK returned a shape we can't parse — probe falls through.
            return None
        for row in rows:
            if _groww_row_indicates_open(row, target_codes):
                return True
        return False

    def _call_market_status_sdk(self, exchange: str) -> Any | None:
        """Discover the SDK method + invoke; returns raw response or None on miss/failure."""
        sdk = self.groww
        status_fn = (getattr(sdk, "get_market_status", None)
                     or getattr(sdk, "market_status", None)
                     or getattr(sdk, "get_exchange_status", None))
        if status_fn is None:
            return None
        try:
            return status_fn()
        except Exception as e:
            logger.debug(f"GrowwBroker.market_status({exchange}) SDK call failed: {e}")
            return None

    # ── Order entry ───────────────────────────────────────────────────

    @_retry_groww_auth
    def basket_order_margins(self, orders: list[dict]) -> list[dict]:
        """Groww exposes per-order margin via `get_order_margin_details`;
        no batch endpoint. Loop and return Kite-shape list."""
        out: list[dict] = []
        for o in orders:
            try:
                exch = o.get("exchange", "")
                ex, seg = _groww_exchange_and_segment(exch)
                resp = self.groww.get_order_margin_details(
                    trading_symbol=o.get("tradingsymbol", ""),
                    exchange=ex,
                    segment=seg,
                    transaction_type=o.get("transaction_type", "BUY"),
                    quantity=int(o.get("quantity", 0)),
                    order_type=_ORDER_TYPE_TO_GROWW.get(o.get("order_type", "MARKET"),
                                                        "MARKET"),
                    product=_PRODUCT_TO_GROWW.get(o.get("product", "MIS"), "MIS"),
                    price=float(o.get("price") or 0),
                )
                data = resp.get("data") if isinstance(resp, dict) else {}
                if not isinstance(data, dict):
                    data = {}
                out.append({
                    "total":    float(data.get("total_margin",     0) or 0),
                    "var":      float(data.get("span_margin",      0) or 0),
                    "exposure": float(data.get("exposure_margin",  0) or 0),
                    "available": {"cash": float(data.get("available_balance",
                                                          0) or 0)},
                    "raw":      resp,
                })
            except Exception as e:
                logger.warning(f"GrowwBroker.basket_order_margins failed for "
                               f"{o.get('tradingsymbol')}: {e}")
                out.append({"total": 0.0, "error": str(e), "raw": None})
        return out

    @_retry_groww_auth
    def place_order(self, *, intent: str | None = None, **kwargs: Any) -> str:
        # Audit fix (M-3) — `variety` is Kite-semantic. AMO needs
        # explicit Groww-side handling that isn't wired today; raise
        # so the operator knows the request isn't honored instead of
        # silently landing AMO orders as regular-hours.
        # `intent` is a RamboQuant-level hint; not forwarded to Groww SDK.
        del intent
        _variety = str(kwargs.pop("variety", "regular") or "regular").lower()
        if _variety in ("amo", "after_market", "after-market"):
            raise NotImplementedError(
                "Groww adapter does not yet route AMO orders. Submit during "
                "market hours or route via the Kite-mirrored account."
            )
        ex, seg = _groww_exchange_and_segment(kwargs.get("exchange", ""))
        resp = self.groww.place_order(
            validity=kwargs.get("validity", "DAY"),
            exchange=ex,
            order_type=_ORDER_TYPE_TO_GROWW.get(kwargs.get("order_type", "MARKET"),
                                                "MARKET"),
            product=_PRODUCT_TO_GROWW.get(kwargs.get("product", "MIS"), "MIS"),
            quantity=int(kwargs.get("quantity", 0)),
            segment=seg,
            trading_symbol=kwargs.get("tradingsymbol", ""),
            transaction_type=kwargs.get("transaction_type", "BUY"),
            price=float(kwargs.get("price") or 0),
            trigger_price=(float(kwargs.get("trigger_price"))
                           if kwargs.get("trigger_price") else None),
        )
        data = resp.get("data") if isinstance(resp, dict) else {}
        order_id = (data.get("groww_order_id") or data.get("order_id")
                    if isinstance(data, dict) else None)
        if not order_id:
            raise RuntimeError(f"Groww place_order rejected: {resp}")
        return str(order_id)

    def _resolve_exchange_from_order(self, order_id: str) -> str:
        """Look up an open order's exchange via self.orders() when the
        caller did not supply it.  Returns an empty string when not found
        so callers can raise a clear error rather than routing to a wrong
        segment silently."""
        try:
            for o in self.orders():
                if str(o.get("order_id", "")) == str(order_id):
                    return str(o.get("exchange", ""))
        except Exception as _e:
            logger.debug(f"GrowwBroker._resolve_exchange_from_order({order_id}): {_e}")
        return ""

    @_retry_groww_auth
    def modify_order(self, order_id: str, **kwargs: Any) -> str:
        # Slice Q — resolve exchange when caller omits it (e.g. chase.py
        # doesn't pass exchange to broker.modify_order). Pre-fix: empty
        # string raised ValueError from _groww_exchange_and_segment.
        exchange = str(kwargs.get("exchange") or "")
        if not exchange:
            exchange = self._resolve_exchange_from_order(order_id)
        if not exchange:
            raise ValueError(
                f"modify_order: exchange required and could not be resolved "
                f"from broker.orders() for order_id={order_id!r}"
            )
        _, seg = _groww_exchange_and_segment(exchange)
        self.groww.modify_order(
            order_type=_ORDER_TYPE_TO_GROWW.get(kwargs.get("order_type", "LIMIT"),
                                                 "LIMIT"),
            segment=seg,
            groww_order_id=order_id,
            quantity=int(kwargs.get("quantity", 0)),
            price=(float(kwargs.get("price")) if kwargs.get("price") else None),
            trigger_price=(float(kwargs.get("trigger_price"))
                           if kwargs.get("trigger_price") else None),
        )
        return order_id

    @_retry_groww_auth
    def cancel_order(self, order_id: str, **kwargs: Any) -> str:
        # Slice Q — resolve exchange when caller omits it. Pre-fix: default
        # "NSE" silently sent MCX/NFO cancels to the CASH segment.
        exchange = str(kwargs.get("exchange") or "")
        if not exchange:
            exchange = self._resolve_exchange_from_order(order_id)
        if not exchange:
            raise ValueError(
                f"cancel_order: exchange required and could not be resolved "
                f"from broker.orders() for order_id={order_id!r}"
            )
        _, seg = _groww_exchange_and_segment(exchange)
        self.groww.cancel_order(segment=seg, groww_order_id=order_id)
        return order_id

    # ── GTT (Groww Smart Orders — single-trigger only) ────────────────
    #
    # Groww supports single-trigger GTT via `create_smart_order` with
    # smart_order_type="GTT". OCO is declared gtt_oco=False in the
    # capability matrix — the orchestrator emulates it via two singles +
    # a pair-watcher; those don't reach this method.
    #
    # Kite shape → Groww `create_smart_order` (GTT variant):
    #   trigger_values[0]  → trigger_price (string)
    #   orders[0]["transaction_type"]  → inferred from trigger direction
    #   trigger direction  → "UP" when trigger > last_price (stop-buy or
    #                         target on short); "DOWN" otherwise (stop-loss
    #                         or target on long).
    #   orders[0]["price"] → order.price (limit price, or absent for MARKET)
    #   orders[0]["order_type"] → order.order_type ("LIMIT" / "MARKET")
    #   orders[0]["transaction_type"] → order.transaction_type
    #
    # Groww Smart Order docs: https://groww.in/trade-api/docs — see
    # "Smart Orders" → "Create Smart Order".
    # The SDK method is `create_smart_order(smart_order_type, segment,
    #   trading_symbol, quantity, product_type, exchange, duration,
    #   trigger_price, trigger_direction, order, ...)`.

    @_retry_groww_auth
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
        """Place a Groww GTT (Smart Order). Returns the Groww smart
        order ID (e.g. 'gtt_91a7f4') for a single trigger, or a
        compound ID `"oco:{a}+{b}"` for an emulated OCO pair.

        Sprint C — Groww has no native OCO. When `trigger_type='two-leg'`
        we split into two separate single-trigger Smart Orders, return
        a compound id that `cancel_gtt` / `modify_gtt` know how to parse,
        and the background `_task_oco_pair_watcher` polls broker state
        to cancel the sibling when one side fires. If the second leg
        placement fails after the first one succeeded, we cancel the
        first leg immediately so the operator doesn't end up with a
        naked single-leg exit on the book.
        """
        if trigger_type == "two-leg":
            return self._place_oco_emulated(
                tradingsymbol=tradingsymbol,
                exchange=exchange,
                last_price=last_price,
                orders=orders,
                trigger_values=trigger_values,
                tag=tag,
            )
        ex, seg = _groww_exchange_and_segment(exchange)
        order0, qty0, price0, otype0, txn0, product, trig = \
            _groww_gtt_unpack_order(orders, trigger_values)
        direction  = "UP" if trig > last_price else "DOWN"
        order_body = _groww_gtt_order_body(txn0, otype0, price0)
        resp = self.groww.create_smart_order(
            smart_order_type="GTT",
            segment=seg,
            trading_symbol=tradingsymbol,
            quantity=qty0,
            product_type=product,
            exchange=ex,
            duration="GTC",
            trigger_price=str(trig),
            trigger_direction=direction,
            order=order_body,
        )
        data = resp.get("data") if isinstance(resp, dict) else resp
        if not isinstance(data, dict):
            raise RuntimeError(f"Groww place_gtt rejected: {resp}")
        gtt_id = _first(data, "smart_order_id", "reference_id", "id")
        if not gtt_id:
            raise RuntimeError(f"Groww place_gtt: no ID in response: {resp}")
        return str(gtt_id)

    def _place_oco_emulated(
        self,
        *,
        tradingsymbol: str,
        exchange: str,
        last_price: float,
        orders: list[dict],
        trigger_values: list[float],
        tag: str | None,
    ) -> str:
        """Place two single-trigger Smart Orders to emulate a Kite OCO.

        Returns a compound id `"oco:{a}+{b}"` that `cancel_gtt` /
        `modify_gtt` parse to dispatch to both legs. Atomic: if leg 1
        placement fails after leg 0 succeeded, we cancel leg 0 so the
        operator doesn't get left with a naked single-leg bracket.
        """
        if len(orders) < 2 or len(trigger_values) < 2:
            raise RuntimeError(
                "GrowwBroker.place_gtt OCO needs 2 orders + 2 trigger_values"
            )
        # Leg 0 first (typically TP — caller's convention).
        leg0_id = self.place_gtt(
            trigger_type="single",
            tradingsymbol=tradingsymbol,
            exchange=exchange,
            last_price=last_price,
            orders=[orders[0]],
            trigger_values=[trigger_values[0]],
            tag=tag,
        )
        try:
            leg1_id = self.place_gtt(
                trigger_type="single",
                tradingsymbol=tradingsymbol,
                exchange=exchange,
                last_price=last_price,
                orders=[orders[1]],
                trigger_values=[trigger_values[1]],
                tag=tag,
            )
        except Exception as e:
            # Roll back leg 0 — operator should NEVER end up with one
            # half of an OCO sitting alone on the book.
            try:
                self.cancel_gtt(leg0_id, exchange=exchange)
            except Exception as ce:
                logger.error(
                    f"GrowwBroker emulated OCO rollback failed for leg0={leg0_id}: "
                    f"{ce} (after leg1 place failed: {e})"
                )
            raise RuntimeError(
                f"Groww emulated OCO leg1 failed (leg0={leg0_id} rolled back): {e}"
            )
        return f"oco:{leg0_id}+{leg1_id}"

    @staticmethod
    def _parse_oco_id(gtt_id: str) -> tuple[str, str] | None:
        """Parse `"oco:{a}+{b}"` → (a, b). Returns None for plain ids."""
        if not isinstance(gtt_id, str) or not gtt_id.startswith("oco:"):
            return None
        body = gtt_id[4:]
        if "+" not in body:
            return None
        a, b = body.split("+", 1)
        if not (a and b):
            return None
        return a, b

    @_retry_groww_auth
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
        """Modify a Groww GTT (Smart Order). Returns the (same) gtt_id.

        Sprint C — for an emulated OCO (`gtt_id` starts with `"oco:"`),
        dispatch to both underlying singles. Caller hands us the full
        `[tp, sl]` shape; we map orders[0]/trigger_values[0] → leg0,
        orders[1]/trigger_values[1] → leg1.
        """
        oco = self._parse_oco_id(gtt_id)
        if oco is not None:
            leg0_id, leg1_id = oco
            if len(orders) < 2 or len(trigger_values) < 2:
                raise RuntimeError(
                    "Groww OCO modify needs 2 orders + 2 trigger_values"
                )
            self.modify_gtt(
                leg0_id,
                trigger_type="single",
                tradingsymbol=tradingsymbol,
                exchange=exchange,
                last_price=last_price,
                orders=[orders[0]],
                trigger_values=[trigger_values[0]],
            )
            self.modify_gtt(
                leg1_id,
                trigger_type="single",
                tradingsymbol=tradingsymbol,
                exchange=exchange,
                last_price=last_price,
                orders=[orders[1]],
                trigger_values=[trigger_values[1]],
            )
            return gtt_id
        if trigger_type == "two-leg":
            # Reached only when caller passed two-leg with a non-compound
            # id — happens if state was corrupted. Surface clearly.
            raise RuntimeError(
                f"GrowwBroker.modify_gtt: trigger_type='two-leg' requires "
                f"a compound 'oco:{{a}}+{{b}}' id; got {gtt_id!r}"
            )
        _, seg = _groww_exchange_and_segment(exchange)
        _order0, qty0, price0, otype0, txn0, _product, trig = \
            _groww_gtt_unpack_order(orders, trigger_values)
        direction  = "UP" if trig > last_price else "DOWN"
        order_body = _groww_gtt_order_body(txn0, otype0, price0)
        resp = self.groww.modify_smart_order(
            smart_order_id=gtt_id,
            smart_order_type="GTT",
            segment=seg,
            quantity=qty0 if qty0 else None,
            trigger_price=str(trig),
            trigger_direction=direction,
            order=order_body,
        )
        if isinstance(resp, dict) and resp.get("status", "").upper() == "ERROR":
            raise RuntimeError(f"Groww modify_gtt rejected: {resp}")
        return gtt_id

    @_retry_groww_auth
    def cancel_gtt(self, gtt_id: str, *, exchange: str | None = None) -> str:
        """Cancel a Groww GTT (Smart Order). Returns the cancelled gtt_id.

        Sprint C — compound OCO ids dispatch to both singles, each
        cancelled independently. A single-leg failure logs but does not
        block the other side from being attempted (operator hits an
        all-or-nothing situation otherwise — better one cancelled than
        zero).

        `exchange` kwarg, when present, lets us resolve the Groww
        segment without the four-way blind retry (CASH → FNO →
        COMMODITY → CURRENCY). Existing callers without exchange in
        hand still work via the legacy fall-through.
        """
        oco = self._parse_oco_id(gtt_id)
        if oco is not None:
            leg0_id, leg1_id = oco
            err0 = None
            try:
                self.cancel_gtt(leg0_id, exchange=exchange)
            except Exception as e:
                err0 = e
                logger.warning(
                    f"GrowwBroker.cancel_gtt OCO leg0={leg0_id} failed: {e}"
                )
            try:
                self.cancel_gtt(leg1_id, exchange=exchange)
            except Exception as e:
                logger.warning(
                    f"GrowwBroker.cancel_gtt OCO leg1={leg1_id} failed: {e}"
                )
                if err0 is not None:
                    raise RuntimeError(
                        f"Groww cancel_gtt: both legs failed (leg0={err0}, leg1={e})"
                    )
            return gtt_id
        # cancel_smart_order needs segment + smart_order_type.
        # Audit fix (M-4) — REQUIRE the `exchange` kwarg. Pre-fix
        # legacy callers that didn't carry the segment triggered a
        # blind CASH → FNO → COMMODITY → CURRENCY iteration. If two
        # Groww GTTs from different segments shared a numeric id
        # (possible across order types in Groww's SDK), the fallback
        # would cancel the WRONG one silently. Every internal caller
        # in the codebase (template_attach, _task_oco_pair_watcher)
        # already passes `exchange`; raising here surfaces any future
        # caller that forgets at code-review time rather than at
        # operator-debug time.
        if not exchange:
            raise ValueError(
                f"Groww cancel_gtt requires `exchange` kwarg to resolve "
                f"the Groww segment (CASH / FNO / COMMODITY / CURRENCY). "
                f"Pre-fix this fell through to a blind 4-segment retry "
                f"which could cancel the wrong GTT on numeric-id "
                f"collisions across segments. Pass the originating "
                f"exchange (e.g. 'NFO', 'MCX', 'NSE') so the segment "
                f"resolves deterministically. gtt_id={gtt_id!r}"
            )
        try:
            _, seg = _groww_exchange_and_segment(exchange)
            resp = self.groww.cancel_smart_order(
                segment=seg,
                smart_order_type="GTT",
                smart_order_id=gtt_id,
            )
            if isinstance(resp, dict) and resp.get("status", "").upper() == "ERROR":
                raise RuntimeError(f"Groww cancel_gtt rejected: {resp}")
            return gtt_id
        except Exception as e:
            logger.warning(
                f"GrowwBroker.cancel_gtt {gtt_id!r} on {exchange} "
                f"(seg={seg}) failed: {e}"
            )
            raise

    @_retry_groww_auth
    def get_gtts(self) -> list[dict]:
        """List all active Groww GTT Smart Orders, normalised to Kite GTT shape.
        Paginates automatically (page_size=50 max per Groww docs)."""
        out: list[dict] = []
        page = 0
        while True:
            resp = self.groww.get_smart_order_list(
                smart_order_type="GTT",
                status="ACTIVE",
                page=page,
                page_size=50,
            )
            rows = _extract_groww_gtt_rows(resp)
            if not rows:
                break
            for r in rows:
                out.append(_normalise_groww_gtt_row(r))
            # Groww paginates; if fewer than 50 rows came back we're on the last page
            if len(rows) < 50:
                break
            page += 1
        return out

    # ── Qty translation ───────────────────────────────────────────────

    def translate_qty(self, exchange: str, raw_qty: int, lot_size: int) -> int:
        """Groww accepts quantity in contracts across all segments
        (including MCX). No lot-to-contract translation needed.

        ASSUMPTION: verified against Groww Trade API docs at
        https://groww.in/trade-api/docs — Groww's `quantity` field is
        in contracts across CASH, FNO, and COMMODITY segments. If a
        Groww account rejects an MCX order with a lot-size error, add
        MCX-specific `// lot_size` logic here matching Kite's path."""
        return raw_qty

    def normalise_qty(self, exchange: str, raw_qty: int, lot_size: int) -> int:
        """Back-compat alias — prefer translate_qty in new code."""
        return self.translate_qty(exchange, raw_qty, lot_size)


# ── Response normalisers ──────────────────────────────────────────────


def _gi(row: dict, *keys: str, default: int = 0) -> int:
    """Tolerant int coercion — tries each key in order, falls back to default.
    Equivalent to ``int(row.get(k1, row.get(k2, default)) or default)`` for
    any number of fallback keys. Eliminates repeated ``or 0`` guard patterns
    in per-field normalisation branches.
    """
    for k in keys:
        v = row.get(k)
        if v is not None:
            try:
                return int(v) or default
            except (TypeError, ValueError):
                pass
    return default


def _gf(row: dict, *keys: str, default: float = 0.0) -> float:
    """Tolerant float coercion — same as _gi but returns float."""
    for k in keys:
        v = row.get(k)
        if v is not None:
            try:
                result = float(v)
                return result if result else default
            except (TypeError, ValueError):
                pass
    return default


def _unwrap(resp: Any, key: str = "data") -> Any:
    """Most Groww responses look like {"status": "SUCCESS", "data": ...}.
    Unwrap the inner payload; return [] if it's not a list/dict we can
    iterate."""
    if isinstance(resp, dict):
        return resp.get(key, resp)
    return resp


def _iter_rows(payload: Any, *candidate_keys: str) -> list[dict]:
    """Tolerant row-iterator. Tries multiple field names since Groww
    nests the row list under different keys per endpoint
    (`holdings`, `positions`, `order_list`, etc.)."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for k in candidate_keys:
            v = payload.get(k)
            if isinstance(v, list):
                return v
    return []


def _holding_derive_pnl(h: dict, ltp: float, avg: float, qty: int) -> float:
    """Broker pnl when present, otherwise (ltp − avg) × qty."""
    pnl = _gf(h, "pnl")
    if not pnl and ltp > 0 and avg > 0 and qty:
        return (ltp - avg) * qty
    return pnl


def _holding_derive_day_change(h: dict, ltp: float, close: float) -> float:
    """Broker day_change when present, otherwise ltp − close."""
    day_change = _gf(h, "day_change")
    if not day_change and ltp > 0 and close > 0:
        return ltp - close
    return day_change


def _holding_derive_day_change_pct(h: dict, day_change: float, close: float) -> float:
    """Broker day_change_percentage when present, else derive from day_change/close."""
    pct = _gf(h, "day_change_percentage")
    if not pct and close > 0:
        return (day_change / close) * 100
    return pct


def _normalise_holdings(resp: Any) -> list[dict]:
    payload = _unwrap(resp)
    rows = _iter_rows(payload, "holdings")
    out: list[dict] = []
    for h in rows:
        qty    = _gi(h, "quantity")
        t1_qty = _gi(h, "t1_quantity")
        avg    = _gf(h, "average_price", "avg_price")
        ltp    = _gf(h, "last_price", "ltp")
        close  = _gf(h, "close_price", "previous_close")
        # opening_quantity is REQUIRED by the holdings API model — rows
        # missing it get dropped at serialisation, which is why a Groww
        # holding (HFCL / NATIONALUM …) would silently disappear from
        # /api/holdings even though the broker layer returned it.
        # Mirror Kite's convention: opening_quantity is the deliverable
        # count (qty minus any T1 not-yet-settled). Groww carries the
        # field under `quantity` for delivered, `t1_quantity` for in-flight.
        opening_qty = max(qty - t1_qty, 0)
        # Derive missing pnl / day_change from the values Groww does
        # send. Some Groww account shapes omit these and rely on the
        # consumer (e.g. our /performance UI) to compute them.
        #
        # Honest close_price (slice P4): pre-fix `close = ltp` when
        # Groww omitted previous_close. That made day_change = 0 →
        # silently masked these rows from broker_apis.backfill_market_data
        # (which patches a real prior close via PriceBroker.quote()),
        # so the operator-facing Day P&L column read 0 (looks flat)
        # instead of waiting for the backfill (looks unknown). Now
        # we leave close=0 like Dhan does — backfill picks them up.
        pnl = _holding_derive_pnl(h, ltp, avg, qty)
        day_change = _holding_derive_day_change(h, ltp, close)
        day_change_pct = _holding_derive_day_change_pct(h, day_change, close)
        out.append({
            "tradingsymbol":   h.get("trading_symbol") or h.get("tradingsymbol") or "",
            "exchange":        h.get("exchange") or "NSE",
            "instrument_token": h.get("exchange_token") or h.get("instrument_token"),
            "isin":            h.get("isin"),
            "quantity":        qty,
            "opening_quantity": opening_qty,
            "t1_quantity":     t1_qty,
            "average_price":   avg,
            "last_price":      ltp,
            "close_price":     close,
            "pnl":             pnl,
            "day_change":      day_change,
            "day_change_percentage": day_change_pct,
            "product":         h.get("product", "CNC"),
            "_raw":            h,
        })
    return out


def _position_day_buy_value(p: dict) -> float:
    """day_buy_value: prefer the direct field, fall back to price × qty."""
    direct = _gf(p, "day_buy_value")
    if direct:
        return direct
    return _gf(p, "day_buy_price") * _gi(p, "day_buy_quantity")


def _position_day_sell_value(p: dict) -> float:
    """day_sell_value: prefer the direct field, fall back to price × qty."""
    direct = _gf(p, "day_sell_value")
    if direct:
        return direct
    return _gf(p, "day_sell_price") * _gi(p, "day_sell_quantity")


def _normalise_positions(resp: Any) -> dict:
    payload = _unwrap(resp)
    rows = _iter_rows(payload, "positions")
    net: list[dict] = []
    day: list[dict] = []
    for p in rows:
        row = {
            "tradingsymbol":   p.get("trading_symbol") or p.get("tradingsymbol") or "",
            "exchange":        p.get("exchange") or "",
            "instrument_token": p.get("exchange_token") or p.get("instrument_token"),
            "product":         p.get("product", "NRML"),
            "quantity":        _gi(p, "quantity"),
            "overnight_quantity": _gi(p, "net_carry_forward_quantity", "overnight_quantity"),
            "day_buy_quantity":   _gi(p, "day_buy_quantity"),
            "day_sell_quantity":  _gi(p, "day_sell_quantity"),
            # Day-trade cash values — forwarded to the /admin/derivatives
            # Candidates panel where `splitClosedReopened` splits a
            # closed-and-reopened leg into two display rows. Derive from
            # price × qty when Groww doesn't return value directly.
            # ₹ cash to match Kite convention.
            "day_buy_value":      _position_day_buy_value(p),
            "day_sell_value":     _position_day_sell_value(p),
            # Hard-coded to 1 (mirrors Dhan adapter at brokers/dhan.py:1654).
            # Groww ships quantity + value fields in CONTRACTS across
            # all segments including MCX — no lot→contract conversion
            # needed. If Groww's positions row carried `lot_size` /
            # `multiplier` from the instruments cache and that field
            # leaked into the output (e.g. lot_size=100 for GOLDM),
            # the post-fetch normaliser in broker_apis.py:165-185
            # would multiply quantity, day_buy/sell qty AND (after the
            # Jun 26 2026 fix) day_buy/sell value all by 100 — a 100×
            # double-count on every field. Pinning multiplier=1 here
            # makes the broker_apis multiply a safe no-op for Groww
            # regardless of what's in Groww's positions payload.
            "multiplier":      1,
            "close_price":     _gf(p, "close_price", "previous_close"),
            "average_price":   _gf(p, "average_price", "net_price"),
            "last_price":      _gf(p, "last_price", "ltp"),
            "buy_price":       _gf(p, "buy_price", "buy_avg_price"),
            "sell_price":      _gf(p, "sell_price", "sell_avg_price"),
            "buy_quantity":    _gi(p, "buy_quantity"),
            "sell_quantity":   _gi(p, "sell_quantity"),
            "pnl":             _gf(p, "pnl", "unrealised_pnl"),
            "realised":        _gf(p, "realised_pnl"),
            "unrealised":      _gf(p, "unrealised_pnl"),
            "_raw":            p,
        }
        # Groww splits intraday vs CF via product/quantity context — for
        # now route everything to `net`. day-only positions surface as
        # net rows with overnight_quantity=0 which downstream summarise
        # already handles.
        net.append(row)
    return {"net": net, "day": day}


# Set of Groww accounts whose raw margin response keys have been logged.
# Paired with the one-time INFO log in margins() to confirm field names
# against Groww's incomplete SDK documentation. Resets on process restart.
_GROWW_MARGINS_LOGGED: set[str] = set()


def _normalise_margins(resp: Any, segment: str | None) -> dict:
    payload = _unwrap(resp)
    if not isinstance(payload, dict):
        payload = {}
    if "equity" in payload and isinstance(payload["equity"], dict):
        seg = "commodity" if segment == "commodity" else "equity"
        data = payload.get(seg, payload.get("equity", {}))
    else:
        data = payload
    net = float(_first(data, "net", "available_balance", default=0))
    return {
        "enabled":   True,
        "net":       net,
        "available": _groww_margin_available(data),
        "utilised":  _groww_margin_utilised(data),
        "_raw":      data,
    }


# Audit fix (B-1) — translate Groww terminal status strings to Kite
# canonical values. Pre-fix _normalise_orders passed verbatim strings
# like "EXECUTED" / "TRADED" / "PARTIALLY_FILLED" through, and the
# chase loop (which checks `status == "COMPLETE"`) never detected
# Groww fills. Every Groww-placed order silently orphaned by the
# platform's lifecycle tracking — no template attach, no exit GTTs.
# Mirrors the _DHAN_STATUS_TO_KITE pattern. Covers documented Groww
# status strings + common variants observed across SDK versions.
_GROWW_STATUS_TO_KITE = {
    "EXECUTED":          "COMPLETE",
    "COMPLETED":         "COMPLETE",
    "TRADED":            "COMPLETE",
    "FILLED":            "COMPLETE",
    "PARTIALLY_FILLED":  "OPEN",
    "PARTIAL_FILL":      "OPEN",
    "PARTIAL":           "OPEN",
    "OPEN":              "OPEN",
    "NEW":               "OPEN",
    "PENDING":           "OPEN",
    "ACKNOWLEDGED":      "OPEN",
    "MODIFIED":          "OPEN",
    "CANCELLED":         "CANCELLED",
    "CANCELED":          "CANCELLED",
    "REJECTED":          "REJECTED",
    "FAILED":            "REJECTED",
    "EXPIRED":           "EXPIRED",
}


def _order_status(o: dict) -> str:
    """Map raw Groww order status string to Kite canonical value."""
    raw = (o.get("order_status") or o.get("status") or "").upper()
    return _GROWW_STATUS_TO_KITE.get(raw, raw)


def _normalise_orders(resp: Any) -> list[dict]:
    payload = _unwrap(resp)
    rows = _iter_rows(payload, "order_list", "orders")
    return [_groww_order_row(o) for o in rows]


def _extract_groww_gtt_rows(resp: Any) -> list:
    """Unwrap a Groww get_smart_order_list response into a flat list of rows.
    Handles both {data: {smart_orders: [...]}} and {data: [...]} shapes."""
    data = resp.get("data") if isinstance(resp, dict) else {}
    if isinstance(data, dict):
        return data.get("smart_orders") or data.get("orders") or []
    if isinstance(data, list):
        return data
    return []


def _normalise_groww_gtt_row(r: dict) -> dict:
    """Normalise a single Groww Smart Order row to Kite GTT shape."""
    trig_price  = float(r.get("trigger_price") or 0)
    order_inner = r.get("order") or {}
    return {
        "gtt_id":         str(_first(r, "smart_order_id", "reference_id")),
        "status":         (r.get("status") or "active").lower(),
        "trigger_type":   "single",
        "tradingsymbol":  r.get("trading_symbol") or "",
        "exchange":       r.get("exchange") or "",
        "trigger_values": [trig_price],
        "last_price":     float(r.get("last_price") or 0),
        "orders":         [_groww_build_gtt_order_leg(r, order_inner)],
        "created_at":     r.get("created_at") or "",
        "_raw":           r,
    }


def _normalise_quote_row(data: dict) -> dict:
    """Map a Groww quote row to Kite's quote-row shape."""
    depth = data.get("depth") or {}
    return {
        "instrument_token": _first(data, "exchange_token", "instrument_token"),
        "last_price":       float(_first(data, "last_price", "ltp", default=0)),
        "volume":           int(data.get("volume", 0) or 0),
        "average_price":    float(_first(data, "average_price", "vwap", default=0)),
        "oi":               int(data.get("open_interest", 0) or 0),
        "ohlc": {
            "open":  float(data.get("open",  0) or 0),
            "high":  float(data.get("high",  0) or 0),
            "low":   float(data.get("low",   0) or 0),
            "close": float(_first(data, "close", "previous_close", default=0)),
        },
        "depth": {
            "buy":  depth.get("buy")  or [],
            "sell": depth.get("sell") or [],
        },
        "_raw": data,
    }
