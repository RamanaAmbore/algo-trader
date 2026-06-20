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
from typing import Any, Callable

from backend.shared.brokers.base import Broker
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


# ── Auth-retry decorator ──────────────────────────────────────────────
#
# Mirrors Kite's `@retry_kite_conn` pattern but scoped to Groww auth
# errors only: if a method raises GrowwAPIAuthenticationException (or
# the loose GrowwAPIException carrying a 401 status), evict the cached
# access token, mint a fresh one via TOTP, and retry the call ONCE
# with the new SDK handle. Non-auth errors propagate immediately so
# real bugs (bad params, 5xx, network) aren't masked by silent retries.

# Resolved once at module load — moves the SDK lookup off the hot path.
# Tuple is empty when the SDK isn't installed (lets the broker module
# stay importable while the registry surfaces a cleaner error). Empty
# tuple makes `isinstance(e, ())` always False, so the decorator
# becomes a transparent passthrough until growwapi lands.
try:
    from growwapi.groww.exceptions import (  # type: ignore[import-not-found]
        GrowwAPIAuthenticationException,
        GrowwAPIAuthorisationException,
    )
    _GROWW_AUTH_EXC: tuple = (
        GrowwAPIAuthenticationException, GrowwAPIAuthorisationException,
    )
except ImportError:
    _GROWW_AUTH_EXC = ()


def _retry_groww_auth(fn: Callable) -> Callable:
    """Wraps every GrowwBroker method with: (a) the per-account
    source-IP ContextVar bound for the duration of the SDK call so
    the patched `requests` module routes through a source-bound
    session (see `_install_groww_source_binding` in connections.py),
    and (b) an auth-retry that re-mints the token once on
    GrowwAPIAuthenticationException."""
    @functools.wraps(fn)
    def wrapper(self: "GrowwBroker", *args, **kwargs):
        from backend.shared.helpers.connections import (
            _GROWW_SOURCE_IP_OVERRIDE,
        )
        ip = getattr(self._conn, "_source_ip", None)
        token = _GROWW_SOURCE_IP_OVERRIDE.set(ip) if ip else None
        try:
            try:
                return fn(self, *args, **kwargs)
            except _GROWW_AUTH_EXC as e:  # type: ignore[misc]
                logger.warning(
                    f"GrowwBroker.{fn.__name__} for {self.account!r} hit "
                    f"{type(e).__name__}: {e}. Evicting cached access token "
                    f"and re-minting via TOTP."
                )
                self._conn.refresh()
                return fn(self, *args, **kwargs)
        finally:
            if token is not None:
                _GROWW_SOURCE_IP_OVERRIDE.reset(token)
    return wrapper


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


class GrowwBroker(Broker):
    """Groww adapter. See module docstring for the auth + normalisation
    contract."""

    def __init__(self, conn: "GrowwConnection") -> None:  # type: ignore[name-defined]
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
    def ltp(self, symbols: list[str]) -> dict:
        """Groww's `get_ltp` wants a Tuple of `"EXCHANGE_TRADINGSYMBOL"` keys
        plus a segment. The codebase passes Kite-style `"NSE:RELIANCE"`
        strings. Translate inline so PriceBroker can fail over without
        an instruments-cache lookup."""
        if not symbols:
            return {}
        try:
            # Split each `"NSE:RELIANCE"` into exchange + symbol; Groww
            # wants them joined back as `"NSE_RELIANCE"`. Mixed-exchange
            # calls need a per-segment fan-out — group by segment first.
            by_seg: dict[str, list[str]] = {}
            for sym in symbols:
                if ":" not in sym:
                    continue
                exch, ts = sym.split(":", 1)
                _, seg = _groww_exchange_and_segment(exch)
                by_seg.setdefault(seg, []).append(f"{exch}_{ts}")
            out: dict[str, dict] = {}
            for seg, keys in by_seg.items():
                resp = self.groww.get_ltp(tuple(keys), segment=seg)
                data = resp.get("data") if isinstance(resp, dict) else {}
                if isinstance(data, dict):
                    # Groww returns {"NSE_RELIANCE": 2435.50, ...}. Rekey
                    # to Kite-shape `"NSE:RELIANCE": {"last_price": …}`.
                    for k, v in data.items():
                        kite_key = k.replace("_", ":", 1)
                        out[kite_key] = {"last_price": float(v or 0)}
            return out
        except Exception as e:
            # Common failure modes on dev/prod: "Access forbidden" when
            # the Groww token is stale or the account lacks the segment
            # entitlement. Earlier this raised RuntimeError which
            # bubbled up through PriceBroker._try as a WARNING per
            # failover hop, generating log spam on every quote loop.
            # Empty dict lets the chain fall through to the next
            # adapter (Kite) silently — the failure is real but it's
            # already been logged once at adapter scope.
            logger.debug(f"Groww ltp returned empty: {e}")
            return {}

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
        except Exception as e:
            logger.debug(f"GrowwBroker._quote_single skipping {sym}: {e}")
        return out

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
            kite_keys = [p[0] for p in pairs]
            groww_keys = tuple(p[1] for p in pairs)
            try:
                resp = self.groww.get_ohlc(
                    exchange_trading_symbols=groww_keys, segment=seg,
                )
                data = resp.get("data") if isinstance(resp, dict) else resp
                if not isinstance(data, dict):
                    continue
                # Groww returns {"NSE_RELIANCE": {"open":…, "high":…,
                # "low":…, "close":…, "ltp":…, "volume":…}, …}.
                # Translate back to Kite-shape quote rows.
                for kite_key, gk in zip(kite_keys, groww_keys):
                    row = data.get(gk) or {}
                    if isinstance(row, dict):
                        out[kite_key] = _normalise_quote_row(row)
            except Exception as e:
                logger.debug(f"GrowwBroker._quote_batch_ohlc segment={seg}: {e}")
        return out

    @_retry_groww_auth
    def instruments(self, exchange: str | None = None) -> list[dict]:
        """Groww's `get_all_instruments()` returns the master CSV as a
        list. Field names are close to Kite's but not identical — map
        the columns the codebase reads off this list."""
        resp = self.groww.get_all_instruments()
        rows = resp if isinstance(resp, list) else resp.get("data", []) \
            if isinstance(resp, dict) else []
        out: list[dict] = []
        for r in rows:
            if exchange and r.get("exchange") != exchange:
                continue
            out.append({
                "instrument_token": r.get("exchange_token") or r.get("instrument_token"),
                "tradingsymbol":    r.get("trading_symbol")  or r.get("tradingsymbol"),
                "name":             r.get("name") or r.get("groww_symbol", ""),
                "exchange":         r.get("exchange") or "",
                "segment":          r.get("segment") or "",
                "instrument_type":  r.get("instrument_type") or "",
                "expiry":           r.get("expiry") or "",
                "strike":           float(r.get("strike", 0) or 0),
                "lot_size":         int(r.get("lot_size", 0) or 0),
                "tick_size":        float(r.get("tick_size", 0) or 0),
                "_raw":             r,
            })
        return out

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
            return []
        ex, seg = _groww_exchange_and_segment(exchange)
        seg = segment or seg
        groww_interval = _INTERVAL_TO_GROWW.get(interval.lower())
        if not groww_interval:
            raise ValueError(
                f"Unknown candle interval {interval!r}. Supported: "
                f"{sorted(set(_INTERVAL_TO_GROWW.values()))}"
            )
        # Groww accepts either `datetime` or ISO `"YYYY-MM-DD HH:MM:SS"`.
        def _coerce(d: Any) -> str:
            if hasattr(d, "strftime"):
                return d.strftime("%Y-%m-%d %H:%M:%S")
            return str(d)
        resp = self.groww.get_historical_candles(
            trading_symbol=trading_symbol,
            exchange=ex,
            segment=seg,
            start_time=_coerce(from_date),
            end_time=_coerce(to_date),
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
        out: list[dict] = []
        for row in candles:
            if not isinstance(row, (list, tuple)) or len(row) < 6:
                continue
            out.append({
                "date":   row[0],
                "open":   float(row[1] or 0),
                "high":   float(row[2] or 0),
                "low":    float(row[3] or 0),
                "close":  float(row[4] or 0),
                "volume": int(row[5] or 0),
            })
        return out

    def holidays(self, exchange: str) -> set[str]:
        """Groww doesn't publish a holidays endpoint. Empty set so
        PriceBroker falls over to Kite without an exception trace."""
        return set()

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
    def place_order(self, **kwargs: Any) -> str:
        # Audit fix (M-3) — `variety` is Kite-semantic. AMO needs
        # explicit Groww-side handling that isn't wired today; raise
        # so the operator knows the request isn't honored instead of
        # silently landing AMO orders as regular-hours.
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

    @_retry_groww_auth
    def modify_order(self, order_id: str, **kwargs: Any) -> str:
        _, seg = _groww_exchange_and_segment(kwargs.get("exchange", ""))
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
        _, seg = _groww_exchange_and_segment(kwargs.get("exchange", "NSE"))
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
        order0   = orders[0] if orders else {}
        qty0     = int(order0.get("quantity", 0))
        price0   = float(order0.get("price") or 0)
        otype0   = _ORDER_TYPE_TO_GROWW.get(order0.get("order_type", "LIMIT"), "LIMIT")
        txn0     = order0.get("transaction_type", "SELL")
        product  = _PRODUCT_TO_GROWW.get(order0.get("product", "NRML"), "NRML")
        trig     = float(trigger_values[0]) if trigger_values else 0.0

        # Trigger direction: UP when trigger is above last_price (e.g.
        # stop-buy / short target); DOWN otherwise (stop-loss / long target).
        direction = "UP" if trig > last_price else "DOWN"

        # Groww order sub-dict: transaction_type, order_type, price (optional)
        order_body: dict[str, Any] = {
            "transaction_type": txn0,
            "order_type":       otype0,
        }
        if otype0 == "LIMIT" and price0 > 0:
            order_body["price"] = price0

        resp = self.groww.create_smart_order(
            smart_order_type="GTT",
            segment=seg,
            trading_symbol=tradingsymbol,
            quantity=qty0,
            product_type=product,
            exchange=ex,
            duration="GTC",           # GTT is always Good-Till-Cancelled
            trigger_price=str(trig),
            trigger_direction=direction,
            order=order_body,
        )
        data = resp.get("data") if isinstance(resp, dict) else resp
        if not isinstance(data, dict):
            raise RuntimeError(f"Groww place_gtt rejected: {resp}")
        gtt_id = (data.get("smart_order_id") or data.get("reference_id")
                  or data.get("id") or "")
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
        order0    = orders[0] if orders else {}
        qty0      = int(order0.get("quantity", 0))
        price0    = float(order0.get("price") or 0)
        otype0    = _ORDER_TYPE_TO_GROWW.get(order0.get("order_type", "LIMIT"), "LIMIT")
        txn0      = order0.get("transaction_type", "SELL")
        trig      = float(trigger_values[0]) if trigger_values else 0.0
        direction = "UP" if trig > last_price else "DOWN"
        order_body: dict[str, Any] = {"transaction_type": txn0, "order_type": otype0}
        if otype0 == "LIMIT" and price0 > 0:
            order_body["price"] = price0
        resp = self.groww.modify_smart_order(
            smart_order_id=gtt_id,
            smart_order_type="GTT",
            segment=seg,
            quantity=qty0 if qty0 else None,
            trigger_price=str(trig),
            trigger_direction=direction,
            order=order_body,
        )
        # modify_smart_order raises GrowwAPIException on failure; if it
        # returns a dict check for an error shape.
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
            data = resp.get("data") if isinstance(resp, dict) else {}
            rows = []
            if isinstance(data, dict):
                rows = data.get("smart_orders") or data.get("orders") or []
            elif isinstance(data, list):
                rows = data
            if not rows:
                break
            for r in rows:
                trig_price = float(r.get("trigger_price") or 0)
                order_inner = r.get("order") or {}
                out.append({
                    "gtt_id":       str(r.get("smart_order_id") or r.get("reference_id") or ""),
                    "status":       (r.get("status") or "active").lower(),
                    "trigger_type": "single",
                    "tradingsymbol": r.get("trading_symbol") or "",
                    "exchange":     r.get("exchange") or "",
                    "trigger_values": [trig_price],
                    "last_price":   float(r.get("last_price") or 0),
                    "orders": [{
                        "transaction_type": order_inner.get("transaction_type") or "SELL",
                        "quantity":         int(r.get("quantity") or 0),
                        "price":            float(order_inner.get("price") or 0),
                        "order_type":       order_inner.get("order_type") or "LIMIT",
                        "product":          r.get("product_type") or "NRML",
                    }],
                    "created_at":   r.get("created_at") or "",
                    "_raw":         r,
                })
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


def _normalise_holdings(resp: Any) -> list[dict]:
    payload = _unwrap(resp)
    rows = _iter_rows(payload, "holdings")
    out: list[dict] = []
    for h in rows:
        qty    = int(h.get("quantity", 0) or 0)
        t1_qty = int(h.get("t1_quantity", 0) or 0)
        avg    = float(h.get("average_price",  h.get("avg_price", 0)) or 0)
        ltp    = float(h.get("last_price",     h.get("ltp", 0)) or 0)
        close  = float(h.get("close_price",    h.get("previous_close", 0)) or 0)
        # opening_quantity is REQUIRED by the holdings API model — rows
        # missing it get dropped at serialisation, which is why a Groww
        # holding (HFCL / NATIONALUM …) would silently disappear from
        # /api/holdings even though the broker layer returned it.
        # Mirror Kite's convention: opening_quantity is the deliverable
        # count (qty minus any T1 not-yet-settled). Groww carries the
        # field under `quantity` for delivered, `t1_quantity` for in-flight.
        opening_qty = max(qty - t1_qty, 0)
        # Derive missing close_price / pnl / day_change from the values
        # Groww does send. Some Groww account shapes omit these and rely
        # on the consumer (e.g. our /performance UI) to compute them.
        if close <= 0 and ltp > 0:
            close = ltp
        pnl = float(h.get("pnl", 0) or 0)
        if not pnl and ltp > 0 and avg > 0 and qty:
            pnl = (ltp - avg) * qty
        day_change = float(h.get("day_change", 0) or 0)
        if not day_change and ltp > 0 and close > 0:
            day_change = ltp - close
        day_change_pct = float(h.get("day_change_percentage", 0) or 0)
        if not day_change_pct and close > 0:
            day_change_pct = (day_change / close) * 100
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
            "quantity":        int(p.get("quantity",       0) or 0),
            "overnight_quantity": int(p.get("net_carry_forward_quantity",
                                            p.get("overnight_quantity", 0)) or 0),
            "day_buy_quantity":   int(p.get("day_buy_quantity",  0) or 0),
            "day_sell_quantity":  int(p.get("day_sell_quantity", 0) or 0),
            # Day-trade cash values — forwarded to the /admin/derivatives
            # Candidates panel where `splitClosedReopened` splits a
            # closed-and-reopened leg into two display rows. Not used by
            # the day_change_val recompute in broker_apis any more
            # (that was the retired "split P∆" formula; current shape
            # is the universal (LTP-close)*qty applied at the
            # chokepoint). Derive from price × qty when Groww doesn't
            # return value directly. ₹ cash to match Kite convention.
            "day_buy_value":      float(p.get("day_buy_value",  0) or 0)
                                  or (float(p.get("day_buy_price",  0) or 0)
                                      * int(p.get("day_buy_quantity",  0) or 0)),
            "day_sell_value":     float(p.get("day_sell_value", 0) or 0)
                                  or (float(p.get("day_sell_price", 0) or 0)
                                      * int(p.get("day_sell_quantity", 0) or 0)),
            "multiplier":      int(p.get("multiplier",
                                         p.get("lot_size", 1)) or 1),
            "close_price":     float(p.get("close_price",
                                           p.get("previous_close", 0)) or 0),
            "average_price":   float(p.get("average_price",
                                           p.get("net_price", 0)) or 0),
            "last_price":      float(p.get("last_price",
                                           p.get("ltp", 0)) or 0),
            "buy_price":       float(p.get("buy_price",  p.get("buy_avg_price", 0)) or 0),
            "sell_price":      float(p.get("sell_price", p.get("sell_avg_price", 0)) or 0),
            "buy_quantity":    int(p.get("buy_quantity",  0) or 0),
            "sell_quantity":   int(p.get("sell_quantity", 0) or 0),
            "pnl":             float(p.get("pnl",        p.get("unrealised_pnl", 0)) or 0),
            "realised":        float(p.get("realised_pnl", 0) or 0),
            "unrealised":      float(p.get("unrealised_pnl", 0) or 0),
            "_raw":            p,
        }
        # Groww splits intraday vs CF via product/quantity context — for
        # now route everything to `net`. day-only positions surface as
        # net rows with overnight_quantity=0 which downstream summarise
        # already handles.
        net.append(row)
    return {"net": net, "day": day}


def _normalise_margins(resp: Any, segment: str | None) -> dict:
    payload = _unwrap(resp)
    if not isinstance(payload, dict):
        payload = {}
    # Groww nests by segment when segment query param is omitted —
    # tolerate both shapes.
    if "equity" in payload and isinstance(payload["equity"], dict):
        seg = "commodity" if segment == "commodity" else "equity"
        data = payload.get(seg, payload.get("equity", {}))
    else:
        data = payload
    return {
        "enabled":   True,
        "net":       float(data.get("net",
                                    data.get("available_balance", 0)) or 0),
        "available": {
            "adhoc_margin":      float(data.get("adhoc_margin", 0) or 0),
            "cash":              float(data.get("available_balance",
                                                data.get("cash", 0)) or 0),
            "opening_balance":   float(data.get("opening_balance", 0) or 0),
            "live_balance":      float(data.get("available_balance", 0) or 0),
            "collateral":        float(data.get("collateral", 0) or 0),
            "intraday_payin":    float(data.get("intraday_payin", 0) or 0),
        },
        "utilised": {
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
        },
        "_raw": data,
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


def _normalise_orders(resp: Any) -> list[dict]:
    payload = _unwrap(resp)
    rows = _iter_rows(payload, "order_list", "orders")
    out: list[dict] = []
    for o in rows:
        _raw_status = (o.get("order_status") or o.get("status") or "").upper()
        _status = _GROWW_STATUS_TO_KITE.get(_raw_status, _raw_status)
        out.append({
            "order_id":         str(o.get("groww_order_id") or o.get("order_id") or ""),
            "tradingsymbol":    o.get("trading_symbol") or o.get("tradingsymbol") or "",
            "exchange":         o.get("exchange") or "",
            "status":           _status,
            "transaction_type": o.get("transaction_type") or "BUY",
            "order_type":       o.get("order_type") or "MARKET",
            "product":          o.get("product") or "NRML",
            "quantity":         int(o.get("quantity",         0) or 0),
            "filled_quantity":  int(o.get("filled_quantity",  0) or 0),
            "pending_quantity": int(o.get("remaining_quantity",
                                          o.get("pending_quantity", 0)) or 0),
            "price":            float(o.get("price",         0) or 0),
            "trigger_price":    float(o.get("trigger_price", 0) or 0),
            "average_price":    float(o.get("average_price",
                                            o.get("filled_avg_price", 0)) or 0),
            "order_timestamp":  o.get("created_at") or o.get("order_timestamp") or "",
            "exchange_timestamp": o.get("exchange_time") or "",
            "status_message":   o.get("remark") or o.get("status_message") or "",
            "_raw":             o,
        })
    return out


def _normalise_quote_row(data: dict) -> dict:
    """Map a Groww quote row to Kite's quote-row shape."""
    depth = data.get("depth") or {}
    return {
        "instrument_token":  data.get("exchange_token") or data.get("instrument_token"),
        "last_price":        float(data.get("last_price",
                                            data.get("ltp", 0)) or 0),
        "volume":            int(data.get("volume", 0) or 0),
        "average_price":     float(data.get("average_price",
                                            data.get("vwap", 0)) or 0),
        "oi":                int(data.get("open_interest", 0) or 0),
        "ohlc": {
            "open":   float(data.get("open",  0) or 0),
            "high":   float(data.get("high",  0) or 0),
            "low":    float(data.get("low",   0) or 0),
            "close":  float(data.get("close",
                                     data.get("previous_close", 0)) or 0),
        },
        "depth": {
            "buy":  depth.get("buy")  or [],
            "sell": depth.get("sell") or [],
        },
        "_raw": data,
    }
