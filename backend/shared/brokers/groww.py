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

from typing import Any

from backend.shared.brokers.base import Broker
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


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

    def holdings(self) -> list[dict]:
        resp = self.groww.get_holdings_for_user()
        return _normalise_holdings(resp)

    def positions(self) -> dict:
        resp = self.groww.get_positions_for_user()
        return _normalise_positions(resp)

    def margins(self, segment: str | None = None) -> dict:
        resp = self.groww.get_available_margin_details()
        return _normalise_margins(resp, segment)

    def orders(self) -> list[dict]:
        resp = self.groww.get_order_list()
        return _normalise_orders(resp)

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
            raise RuntimeError(f"Groww ltp failed: {e}") from e

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
            raise ValueError(
                "GrowwBroker.historical_data requires trading_symbol + "
                "exchange kwargs (Groww has no token→symbol lookup)."
            )
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
        """Groww doesn't publish a holidays endpoint. PriceBroker
        falls back to Kite."""
        raise NotImplementedError(
            "GrowwBroker.holidays not available; Groww doesn't publish "
            "a holidays endpoint. PriceBroker falls back to Kite."
        )

    # ── Order entry ───────────────────────────────────────────────────

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

    def place_order(self, **kwargs: Any) -> str:
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

    def cancel_order(self, order_id: str, **kwargs: Any) -> str:
        _, seg = _groww_exchange_and_segment(kwargs.get("exchange", "NSE"))
        self.groww.cancel_order(segment=seg, groww_order_id=order_id)
        return order_id

    # ── Qty translation ───────────────────────────────────────────────

    def normalise_qty(self, exchange: str, raw_qty: int, lot_size: int) -> int:
        """Groww accepts quantity in contracts across all segments
        (including MCX). No translation needed."""
        return raw_qty


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
        out.append({
            "tradingsymbol":   h.get("trading_symbol") or h.get("tradingsymbol") or "",
            "exchange":        h.get("exchange") or "NSE",
            "instrument_token": h.get("exchange_token") or h.get("instrument_token"),
            "isin":            h.get("isin"),
            "quantity":        int(h.get("quantity", 0) or 0),
            "t1_quantity":     int(h.get("t1_quantity", 0) or 0),
            "average_price":   float(h.get("average_price",
                                           h.get("avg_price", 0)) or 0),
            "last_price":      float(h.get("last_price",
                                           h.get("ltp", 0)) or 0),
            "close_price":     float(h.get("close_price",
                                           h.get("previous_close", 0)) or 0),
            "pnl":             float(h.get("pnl", 0) or 0),
            "day_change":      float(h.get("day_change", 0) or 0),
            "day_change_percentage": float(h.get("day_change_percentage", 0) or 0),
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


def _normalise_orders(resp: Any) -> list[dict]:
    payload = _unwrap(resp)
    rows = _iter_rows(payload, "order_list", "orders")
    out: list[dict] = []
    for o in rows:
        out.append({
            "order_id":         str(o.get("groww_order_id") or o.get("order_id") or ""),
            "tradingsymbol":    o.get("trading_symbol") or o.get("tradingsymbol") or "",
            "exchange":         o.get("exchange") or "",
            "status":           (o.get("order_status") or o.get("status") or "").upper(),
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
