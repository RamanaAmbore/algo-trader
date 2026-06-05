"""
Dhan implementation of the `Broker` interface.

Wraps the official `dhanhq` Python SDK (PyPI: `dhanhq`). Two pieces of
auth state live in `DhanConnection`:

  * `client_id`     — the operator's 10-digit Dhan trading account number
                      (plaintext; alone it does not authenticate).
  * `access_token`  — short-lived (≤ 24 h) JWT minted from the Dhan
                      Partner API portal.

Dhan caps access-token validity at 24 hours, so the adapter cannot use
the "paste once, run forever" model Kite-without-TOTP uses. Two refresh
paths are supported:

  1. Manual refresh — operator regenerates the token on Dhan's portal
     and pastes it into /admin/brokers. Adapter picks up the new token
     on the post-save `Connections.rebuild_from_db()` call.

  2. Automated refresh via Partner API (not yet wired) — adapter can
     mint a fresh token daily using `api_key + api_secret + TOTP`.
     The credential columns already exist on `broker_accounts`
     (api_secret_enc, totp_token_enc); the OAuth login flow lands in
     a follow-up sprint once a sandbox token is available to test
     against.

Response normalisation — Dhan's REST responses use different field
names than Kite (e.g. `securityId` vs `instrument_token`,
`tradingSymbol` vs `tradingsymbol`). Every method here maps Dhan's
response shape back to the Kite shape the rest of the codebase
consumes. Where a Dhan field has no Kite analogue it's carried through
under the Dhan name so future callers can read it without another
adapter touch.

Status: scaffold. Every method that hits Dhan's API is marked with the
SDK call it delegates to. First real call against a live account will
surface any field-shape drift between Dhan's docs and the response;
fix at that point with the live trace in hand.
"""

from __future__ import annotations

from typing import Any, Callable

from backend.shared.brokers.base import Broker
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


# ── Auth-retry plumbing ──────────────────────────────────────────────
#
# Dhan's SDK doesn't raise on auth failure — it returns a dict
# `{"status": "failure", "remarks": "Invalid access token", ...}`
# instead. To match the "cache → use → re-mint on failure" lifecycle
# Kite (@retry_kite_conn) and Groww (@_retry_groww_auth) already use,
# every broker method that touches the SDK runs its call through
# DhanBroker._safe_call(...).
#
# _safe_call passes the live SDK handle into the operator's lambda
# and inspects the raw response BEFORE normalisation. If the response
# carries an auth-error shape (status=failure + auth-keyword remarks),
# it forces a re-login via get_dhan_conn(test_conn=True) — which
# re-runs _do_login() with the stored PIN + TOTP seed — and retries
# once with the new SDK handle. If the account isn't configured for
# headless re-login, _do_login raises and the original auth-failure
# response propagates to the caller unchanged.
_AUTH_ERROR_HINTS = (
    "invalid access token",
    "invalid token",
    "token expired",
    "unauthorized",
    "unauthorised",
    "auth failed",
    "401",
    "dh-901",   # Dhan: Invalid Authentication
    "dh-905",   # Dhan: Invalid Token
)


def _looks_like_auth_failure(resp: Any) -> bool:
    """True when a Dhan SDK response carries an auth-error signal."""
    if not isinstance(resp, dict):
        return False
    status = str(resp.get("status", "")).lower()
    if status != "failure":
        return False
    remarks = str(resp.get("remarks", "")).lower()
    return any(hint in remarks for hint in _AUTH_ERROR_HINTS)


# Dhan exchange-segment constants. The SDK uses opaque integer codes;
# we accept the Kite-style string ("NSE", "NFO", "MCX", ...) at the
# Broker boundary and translate here. Kite's "BSE" and "BFO" map to
# Dhan's BSE_EQ / BSE_FNO. CDS / BCD don't have direct Dhan counterparts
# (currency derivatives) — left out until needed.
_EXCHANGE_TO_DHAN: dict[str, str] = {
    "NSE": "NSE_EQ",
    "BSE": "BSE_EQ",
    "NFO": "NSE_FNO",
    "BFO": "BSE_FNO",
    "MCX": "MCX_COMM",
}

# Kite transaction_type ("BUY" / "SELL") is identical to Dhan; no map needed.
# Kite product ("CNC" / "MIS" / "NRML") → Dhan product type.
_PRODUCT_TO_DHAN: dict[str, str] = {
    "CNC":  "CNC",     # Cash and carry (delivery)
    "MIS":  "INTRADAY",
    "NRML": "MARGIN",  # F&O carry-forward
}

# Kite order_type → Dhan order_type. Same strings.
_ORDER_TYPE_TO_DHAN: dict[str, str] = {
    "MARKET": "MARKET",
    "LIMIT":  "LIMIT",
    "SL":     "STOP_LOSS",
    "SL-M":   "STOP_LOSS_MARKET",
}


def _dhan_exchange(kite_exchange: str) -> str:
    """Translate a Kite-style exchange string to Dhan's exchange-segment."""
    seg = _EXCHANGE_TO_DHAN.get(kite_exchange)
    if not seg:
        raise ValueError(f"No Dhan exchange-segment mapping for {kite_exchange!r}")
    return seg


class DhanBroker(Broker):
    """Dhan adapter. See module docstring for the auth + normalisation
    contract."""

    def __init__(self, conn: "DhanConnection") -> None:  # type: ignore[name-defined]
        self._conn = conn

    # ── Identity + escape hatch ───────────────────────────────────────

    @property
    def account(self) -> str:
        return self._conn.account

    @property
    def broker_id(self) -> str:
        return "dhan"

    @property
    def dhan(self):
        """Underlying `dhanhq` SDK handle. Re-validates the access token
        on every access (DhanConnection re-mints when expired). Escape
        hatch for SDK features not lifted into the Broker ABC."""
        return self._conn.get_dhan_conn()

    def _safe_call(self, sdk_call: Callable[[Any], Any]) -> Any:
        """Invoke an SDK call with auto re-login on auth failure.

        `sdk_call` is a one-arg lambda receiving the live SDK handle —
        e.g. `lambda d: d.get_holdings()`. If the raw response carries
        an auth-failure shape, we evict the cached token (via
        get_dhan_conn(test_conn=True)) and retry once with the freshly
        minted SDK handle. Network / 5xx / param exceptions propagate
        immediately — only auth-shaped failures trigger the retry."""
        resp = sdk_call(self.dhan)
        if _looks_like_auth_failure(resp):
            logger.warning(
                f"DhanBroker for {self.account!r} got auth failure "
                f"(remarks={resp.get('remarks')!r}). Forcing re-login "
                f"via PIN+TOTP and retrying once."
            )
            fresh = self._conn.get_dhan_conn(test_conn=True)
            resp = sdk_call(fresh)
        return resp

    # ── Account state ─────────────────────────────────────────────────

    def profile(self) -> dict:
        """Dhan exposes `get_fund_limits()` as the lightest auth-check
        call; there's no profile() equivalent that returns a user_name.
        Synthesise a Kite-shape dict so the /admin/brokers test button
        gets a recognisable success message."""
        try:
            funds = self._safe_call(lambda d: d.get_fund_limits())
            data = funds.get("data") if isinstance(funds, dict) else None
            return {
                "user_id":   self._conn.client_id,
                "user_name": f"Dhan {self._conn.client_id}",
                "broker":    "DHAN",
                "data":      data,
            }
        except Exception as e:
            raise RuntimeError(f"Dhan auth check failed: {e}") from e

    def holdings(self) -> list[dict]:
        resp = self._safe_call(lambda d: d.get_holdings())
        return _normalise_holdings(resp)

    def positions(self) -> dict:
        resp = self._safe_call(lambda d: d.get_positions())
        return _normalise_positions(resp)

    def margins(self, segment: str | None = None) -> dict:
        resp = self._safe_call(lambda d: d.get_fund_limits())
        return _normalise_margins(resp, segment)

    def orders(self) -> list[dict]:
        resp = self._safe_call(lambda d: d.get_order_list())
        return _normalise_orders(resp)

    def trades(self) -> list[dict]:
        resp = self._safe_call(lambda d: d.get_trade_book())
        return _normalise_trades(resp)

    # ── Market data ───────────────────────────────────────────────────

    def ltp(self, symbols: list[str]) -> dict:
        """Dhan's `ltp_data()` takes `{exchange_segment: [security_id, ...]}`.
        The codebase passes Kite-style `"NSE:RELIANCE"` strings; we'd
        need an instruments-cache lookup to map symbol → security_id.
        Not wired yet — raise so PriceBroker fails over to Kite cleanly."""
        raise NotImplementedError(
            "DhanBroker.ltp not yet wired. Needs instruments-cache "
            "lookup from tradingsymbol → Dhan security_id. PriceBroker "
            "will fall over to Kite in the meantime."
        )

    def quote(self, symbols: list[str]) -> dict:
        raise NotImplementedError(
            "DhanBroker.quote not yet wired. Needs instruments-cache "
            "lookup from tradingsymbol → Dhan security_id. PriceBroker "
            "will fall over to Kite in the meantime."
        )

    def instruments(self, exchange: str | None = None) -> list[dict]:
        """Dhan publishes a master CSV (api.dhan.co/v2/instruments) but
        the SDK doesn't have a one-shot loader. Defer until needed —
        most callers use Kite's `instruments()` which is fully populated."""
        raise NotImplementedError(
            "DhanBroker.instruments not yet wired. Use the Kite adapter "
            "for instruments lookup; PriceBroker falls back automatically."
        )

    def historical_data(
        self,
        instrument_token: int,
        from_date: Any,
        to_date: Any,
        interval: str = "day",
    ) -> list[dict]:
        raise NotImplementedError(
            "DhanBroker.historical_data not yet wired. Dhan SDK exposes "
            "historical_daily_data / historical_minute_charts but uses "
            "different parameter names; needs symbol/segment mapping. "
            "PriceBroker falls back to Kite."
        )

    def holidays(self, exchange: str) -> set[str]:
        """Dhan doesn't publish a holidays endpoint. Return empty set so
        PriceBroker falls over to Kite."""
        raise NotImplementedError(
            "DhanBroker.holidays not available; Dhan doesn't publish "
            "a holidays endpoint. PriceBroker falls back to Kite."
        )

    # ── Order entry ───────────────────────────────────────────────────

    def basket_order_margins(self, orders: list[dict]) -> list[dict]:
        """Dhan exposes per-order margin calculation but no batch endpoint.
        Loop over orders calling `margin_calculator()` per order; return
        a Kite-shape list with `total` populated. Slower than Kite's
        single round-trip but functionally equivalent."""
        out: list[dict] = []
        for o in orders:
            try:
                ex_seg  = _dhan_exchange(o.get("exchange", ""))
                txn     = o.get("transaction_type", "BUY")
                qty     = int(o.get("quantity", 0))
                price   = float(o.get("price") or 0)
                product = _PRODUCT_TO_DHAN.get(o.get("product", "MIS"), "INTRADAY")
                # Dhan's SDK method name has shifted between versions —
                # try `margin_calculator()` first, fall back to the
                # raw POST if missing. Either path returns a dict with
                # a `data.totalMargin` field we map to Kite's `total`.
                if hasattr(self.dhan, "margin_calculator"):
                    resp = self._safe_call(lambda d: d.margin_calculator(
                        security_id=str(o.get("security_id", "")),
                        exchange_segment=ex_seg,
                        transaction_type=txn,
                        quantity=qty,
                        product_type=product,
                        price=price,
                    ))
                else:
                    raise RuntimeError("dhanhq SDK missing margin_calculator method")
                data = resp.get("data") if isinstance(resp, dict) else {}
                out.append({
                    "total":     float(data.get("totalMargin", 0) or 0),
                    "var":       float(data.get("spanMargin", 0) or 0),
                    "exposure":  float(data.get("exposureMargin", 0) or 0),
                    "available": {"cash": float(data.get("availableBalance", 0) or 0)},
                    "raw":       resp,
                })
            except Exception as e:
                logger.warning(f"DhanBroker.basket_order_margins failed for "
                               f"{o.get('tradingsymbol')}: {e}")
                out.append({"total": 0.0, "error": str(e), "raw": None})
        return out

    def place_order(self, **kwargs: Any) -> str:
        """Translate Kite kwargs to Dhan and dispatch. Returns Dhan order_id."""
        ex_seg  = _dhan_exchange(kwargs.get("exchange", ""))
        product = _PRODUCT_TO_DHAN.get(kwargs.get("product", "MIS"), "INTRADAY")
        otype   = _ORDER_TYPE_TO_DHAN.get(kwargs.get("order_type", "MARKET"), "MARKET")
        resp = self._safe_call(lambda d: d.place_order(
            security_id=str(kwargs.get("security_id", "")),
            exchange_segment=ex_seg,
            transaction_type=kwargs.get("transaction_type", "BUY"),
            quantity=int(kwargs.get("quantity", 0)),
            order_type=otype,
            product_type=product,
            price=float(kwargs.get("price") or 0),
            trigger_price=float(kwargs.get("trigger_price") or 0),
        ))
        if not isinstance(resp, dict) or resp.get("status") != "success":
            raise RuntimeError(f"Dhan place_order rejected: {resp}")
        return str(resp.get("data", {}).get("orderId", ""))

    def modify_order(self, order_id: str, **kwargs: Any) -> str:
        resp = self._safe_call(lambda d: d.modify_order(
            order_id=order_id,
            quantity=int(kwargs.get("quantity", 0)) if kwargs.get("quantity") else None,
            price=float(kwargs.get("price") or 0) if kwargs.get("price") else None,
            trigger_price=(float(kwargs.get("trigger_price") or 0)
                           if kwargs.get("trigger_price") else None),
            order_type=_ORDER_TYPE_TO_DHAN.get(kwargs.get("order_type", ""), None),
        ))
        if not isinstance(resp, dict) or resp.get("status") != "success":
            raise RuntimeError(f"Dhan modify_order rejected: {resp}")
        return order_id

    def cancel_order(self, order_id: str, **kwargs: Any) -> str:
        resp = self._safe_call(lambda d: d.cancel_order(order_id=order_id))
        if not isinstance(resp, dict) or resp.get("status") != "success":
            raise RuntimeError(f"Dhan cancel_order rejected: {resp}")
        return order_id

    # ── Qty translation ───────────────────────────────────────────────

    def normalise_qty(self, exchange: str, raw_qty: int, lot_size: int) -> int:
        """Dhan accepts quantity in contracts for every segment including
        MCX (unlike Kite's qty=lots quirk). No translation needed —
        ABC default (identity) suffices, but we keep the override here
        for clarity since this is a frequent Kite-vs-Dhan gotcha."""
        return raw_qty


# ── Response normalisers ──────────────────────────────────────────────
#
# Each helper converts Dhan's REST response (lists of camelCase dicts)
# into the Kite shape callers expect (snake_case + Kite field names).
# Unknown/extra Dhan fields pass through so an operator inspecting the
# raw payload still sees the full picture.


def _unwrap(resp: Any) -> list[dict]:
    """Dhan responses wrap the payload in {status, data} envelopes —
    unwrap to the list inside `data` (or [] on shape mismatch)."""
    if isinstance(resp, dict):
        data = resp.get("data")
        if isinstance(data, list):
            return data
    return []


def _normalise_holdings(resp: Any) -> list[dict]:
    """Dhan holdings field map → Kite. Carries through any field we don't
    explicitly translate, so downstream summarise helpers still find
    expected keys + adapter authors see the full Dhan payload.

    Type-match Kite carefully — pandas+polars conversion downstream is
    strict about column dtypes when rows from multiple brokers are
    concatenated. instrument_token MUST be int (Kite shape), not the
    str Dhan returns; opening_quantity MUST be present (holdings model
    field) — we use totalQty as the proxy since Dhan doesn't expose a
    separate start-of-day count.
    """
    out: list[dict] = []
    for h in _unwrap(resp):
        qty = int(h.get("totalQty",  0) or 0)
        # Dhan returns securityId as a numeric string ("21131"); coerce
        # to int so concat with Kite holdings doesn't trip polars.
        try:
            inst_tok = int(h.get("securityId") or 0)
        except (TypeError, ValueError):
            inst_tok = 0

        avg_price  = float(h.get("avgCostPrice", 0) or 0)
        last_price = float(h.get("lastTradedPrice", 0) or 0)

        # Derive close_price + pnl + day_change when Dhan's response
        # omits them (the holdings endpoint frequently does — only
        # avgCostPrice + lastTradedPrice + totalQty are reliably
        # populated). Without the derivation downstream sees:
        #   close_price = 0 → day_change_pct == 100% (broken display)
        #   pnl         = 0 → P&L column shows 0 even on big movers
        # Kite responses ship these computed, so we mirror that here
        # to keep the cross-broker concat downstream comparable.
        close_price = float(
            h.get("previousClosePrice", h.get("closePrice", 0)) or 0
        )
        # If close_price is missing, fall back to last_price (gives a
        # 0% day_change rather than a -100% one — least misleading
        # display when we genuinely don't have yesterday's close).
        if close_price <= 0:
            close_price = last_price

        pnl_raw = h.get("unrealisedProfit")
        if pnl_raw in (None, 0, "0", 0.0):
            pnl = (last_price - avg_price) * qty
        else:
            pnl = float(pnl_raw)

        day_change_raw = h.get("dayChange")
        if day_change_raw in (None, 0, "0", 0.0):
            day_change = (last_price - close_price) * qty
        else:
            day_change = float(day_change_raw)

        day_change_pct_raw = h.get("dayChangePerc")
        if day_change_pct_raw in (None, 0, "0", 0.0):
            day_change_pct = (
                ((last_price - close_price) / close_price * 100.0)
                if close_price > 0 else 0.0
            )
        else:
            day_change_pct = float(day_change_pct_raw)

        out.append({
            "tradingsymbol":   h.get("tradingSymbol")  or h.get("symbol")   or "",
            "exchange":        h.get("exchange")       or "NSE",
            "instrument_token": inst_tok,
            "isin":             h.get("isin"),
            "quantity":         qty,
            # opening_quantity is required by the holdings model + drives
            # inv_val / cur_val / pnl_percentage derivations downstream.
            # Dhan doesn't expose a separate "opening" count, so default
            # to totalQty (same shape as Kite holdings T0 → T+x).
            "opening_quantity": qty,
            "t1_quantity":      int(h.get("t1Qty",     0) or 0),
            "average_price":    avg_price,
            "last_price":       last_price,
            "close_price":      close_price,
            "pnl":              pnl,
            "day_change":       day_change,
            "day_change_percentage": day_change_pct,
            "product":          "CNC",  # Holdings are always delivery on Dhan
            "_raw":             h,
        })
    return out


def _normalise_positions(resp: Any) -> dict:
    """Dhan positions → Kite-shape {net: [...], day: [...]}. Dhan
    returns one flat list; we map each row to a `net` entry. `day`
    is empty until Dhan exposes intraday-only positions separately."""
    net: list[dict] = []
    for p in _unwrap(resp):
        try:
            inst_tok = int(p.get("securityId") or 0)
        except (TypeError, ValueError):
            inst_tok = 0
        net.append({
            "tradingsymbol":   p.get("tradingSymbol") or "",
            "exchange":        p.get("exchange")     or "NFO",
            "instrument_token": inst_tok,
            "product":         {"INTRADAY": "MIS",
                                "MARGIN":   "NRML",
                                "CNC":      "CNC"}.get(p.get("productType", ""),
                                                        "NRML"),
            "quantity":        int(p.get("netQty",         0) or 0),
            "overnight_quantity": int(p.get("carryFwdQty", 0) or 0),
            "day_buy_quantity":   int(p.get("dayBuyQty",   0) or 0),
            "day_sell_quantity":  int(p.get("daySellQty",  0) or 0),
            # Day-trade cash values — used by broker_apis' split P∆
            # formula. Derive from price × qty when Dhan doesn't carry
            # the value field directly. Stored in ₹ (cash) to match
            # the Kite convention; never rescaled by multiplier.
            "day_buy_value":      float(p.get("dayBuyValue",  0) or 0)
                                  or (float(p.get("dayBuyAvg", 0) or 0)
                                      * int(p.get("dayBuyQty", 0) or 0)),
            "day_sell_value":     float(p.get("daySellValue", 0) or 0)
                                  or (float(p.get("daySellAvg", 0) or 0)
                                      * int(p.get("daySellQty", 0) or 0)),
            "multiplier":      int(p.get("multiplier", 1) or 1),
            "close_price":     float(p.get("previousClose",
                                           p.get("closePrice", 0)) or 0),
            "average_price":   float(p.get("netAvgPrice",  0) or 0),
            "last_price":      float(p.get("lastTradedPrice", 0) or 0),
            "buy_price":       float(p.get("buyAvg",       0) or 0),
            "sell_price":      float(p.get("sellAvg",      0) or 0),
            "buy_quantity":    int(p.get("buyQty",         0) or 0),
            "sell_quantity":   int(p.get("sellQty",        0) or 0),
            "pnl":             float(p.get("unrealisedProfit", 0) or 0),
            "realised":        float(p.get("realisedProfit",   0) or 0),
            "unrealised":      float(p.get("unrealisedProfit", 0) or 0),
            "_raw":            p,
        })
    return {"net": net, "day": []}


def _normalise_margins(resp: Any, segment: str | None) -> dict:
    """Dhan margins endpoint returns a single dict (not per-segment).
    Map to Kite's `equity` shape; if the caller passed segment='commodity'
    we still return the same payload (Dhan doesn't slice this way)."""
    data = resp.get("data") if isinstance(resp, dict) else {}
    if not isinstance(data, dict):
        data = {}
    payload = {
        "enabled": True,
        "net":     float(data.get("availabelBalance", data.get("availableBalance",
                                                                0)) or 0),
        "available": {
            "adhoc_margin":      float(data.get("sodLimit",        0) or 0),
            "cash":              float(data.get("availabelBalance",
                                               data.get("availableBalance", 0)) or 0),
            "opening_balance":   float(data.get("sodLimit",        0) or 0),
            "live_balance":      float(data.get("availabelBalance",
                                               data.get("availableBalance", 0)) or 0),
            "collateral":        float(data.get("collateralAmount", 0) or 0),
            "intraday_payin":    0.0,
        },
        "utilised": {
            "debits":            float(data.get("utilizedAmount",   0) or 0),
            "exposure":          0.0,
            "m2m_realised":      0.0,
            "m2m_unrealised":    0.0,
            "option_premium":    0.0,
            "payout":            float(data.get("withdrawableBalance", 0) or 0),
            "span":              0.0,
            "holding_sales":     0.0,
            "turnover":          0.0,
            "liquid_collateral": 0.0,
            "stock_collateral":  float(data.get("collateralAmount", 0) or 0),
        },
        "_raw": data,
    }
    if segment == "commodity":
        # No per-segment slice from Dhan today — return same payload.
        return payload
    return payload


def _normalise_orders(resp: Any) -> list[dict]:
    out: list[dict] = []
    for o in _unwrap(resp):
        out.append({
            "order_id":         str(o.get("orderId") or ""),
            "tradingsymbol":    o.get("tradingSymbol") or "",
            "exchange":         o.get("exchange") or "",
            "status":           (o.get("orderStatus") or "").upper(),
            "transaction_type": o.get("transactionType") or "BUY",
            "order_type":       o.get("orderType") or "MARKET",
            "product":          {"INTRADAY": "MIS",
                                 "MARGIN":   "NRML",
                                 "CNC":      "CNC"}.get(o.get("productType", ""),
                                                         "NRML"),
            "quantity":         int(o.get("quantity",         0) or 0),
            "filled_quantity":  int(o.get("filledQty",        0) or 0),
            "pending_quantity": int(o.get("remainingQty",     0) or 0),
            "price":            float(o.get("price",          0) or 0),
            "trigger_price":    float(o.get("triggerPrice",   0) or 0),
            "average_price":    float(o.get("averageTradedPrice", 0) or 0),
            "order_timestamp":  o.get("createTime")  or "",
            "exchange_timestamp": o.get("exchangeTime") or "",
            "status_message":   o.get("orderStatusMessage") or "",
            "_raw":             o,
        })
    return out


def _normalise_trades(resp: Any) -> list[dict]:
    out: list[dict] = []
    for t in _unwrap(resp):
        out.append({
            "trade_id":         str(t.get("tradeId")   or ""),
            "order_id":         str(t.get("orderId")   or ""),
            "tradingsymbol":    t.get("tradingSymbol") or "",
            "exchange":         t.get("exchange")      or "",
            "transaction_type": t.get("transactionType") or "BUY",
            "quantity":         int(t.get("tradedQuantity", 0) or 0),
            "average_price":    float(t.get("tradedPrice",  0) or 0),
            "exchange_timestamp": t.get("exchangeTime")   or "",
            "_raw":             t,
        })
    return out
