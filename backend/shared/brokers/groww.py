"""
Groww implementation of the `Broker` interface.

Wraps the official `growwapi.GrowwAPI` Python SDK. Groww's auth model
mirrors Dhan's: a single access token. Token validity depends on how
it was generated:

  * Dashboard-generated token — typically 24 h, manual refresh.
  * Programmatically generated via API key + secret + TOTP — same flow
    Kite uses; auto-refresh wired in a follow-up sprint.

v0 reads the access token from `broker_accounts.access_token_enc`
(Fernet-encrypted), operator pastes a fresh one daily via /admin/brokers.

Response normalisation — Groww's REST shapes are different from both
Kite and Dhan. Field names are usually `snake_case` already (closer to
Kite than to Dhan), but the column names differ — e.g. `quantity` vs
Kite's `quantity` is fine, but `average_price` may come back as
`avg_price`. Each helper translates Groww field names to Kite field
names so the rest of the codebase consumes a uniform shape.

Status: scaffold. Every method that hits Groww's API is wired but
response-shape normalisers are best-effort based on Groww's docs.
First real call against a live account will surface any field-shape
drift; fix in place with the live trace in hand.
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

# Kite order_type → Groww order_type. Same strings, mapping kept for
# clarity + future drift.
_ORDER_TYPE_TO_GROWW: dict[str, str] = {
    "MARKET": "MARKET",
    "LIMIT":  "LIMIT",
    "SL":     "STOP_LOSS",
    "SL-M":   "STOP_LOSS_MARKET",
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
        """Groww's `get_quote` takes one symbol at a time. Loop over
        the batch and aggregate; matches Kite's batch-quote response
        shape (`{"NSE:RELIANCE": {...}}`)."""
        if not symbols:
            return {}
        out: dict[str, dict] = {}
        for sym in symbols:
            if ":" not in sym:
                continue
            try:
                exch, ts = sym.split(":", 1)
                _, seg = _groww_exchange_and_segment(exch)
                resp = self.groww.get_quote(trading_symbol=ts, exchange=exch,
                                            segment=seg)
                data = resp.get("data") if isinstance(resp, dict) else {}
                if isinstance(data, dict):
                    out[sym] = _normalise_quote_row(data)
            except Exception as e:
                logger.debug(f"GrowwBroker.quote skipping {sym}: {e}")
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
    ) -> list[dict]:
        """Groww's `get_historical_candles` wants Groww-specific candle
        interval constants (`CANDLE_INTERVAL_DAY` etc.) and a different
        date format. Stubbed — PriceBroker falls over to Kite for
        historical data until a full mapping ships."""
        raise NotImplementedError(
            "GrowwBroker.historical_data not yet wired. Groww uses its "
            "own interval constants + date format; needs translator. "
            "PriceBroker falls back to Kite."
        )

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
