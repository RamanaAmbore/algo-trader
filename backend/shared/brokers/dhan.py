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

import threading
from typing import Any, Callable
from urllib.request import urlopen

from backend.shared.brokers.base import Broker
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


# ── Instruments cache ──────────────────────────────────────────────────
#
# Dhan publishes a master CSV at the URL below. We fetch it once per IST
# day (cache buster = today's date string) and build two lookup tables:
#   _DHAN_BY_EXCHANGE   — {kite_exchange: list[dict]}  (per-exchange list)
#   _DHAN_BY_SYMBOL     — {(kite_exchange, tradingsymbol): security_id}
# Both are wiped and rebuilt on the first call after midnight IST.
#
# The CSV download is done with stdlib `urllib.request` — no extra deps.
# On any network or parse failure the tables stay empty and callers
# see the "unknown tradingsymbol" error rather than a 500 trace.

_DHAN_INSTRUMENTS_URL = "https://api.dhan.co/v2/instruments-detailed"

# Map Dhan's exchangeSegment column → Kite-style exchange string.
# Used when building the cache so the rest of the codebase never sees
# Dhan's opaque strings.
_DHAN_SEGMENT_TO_EXCHANGE: dict[str, str] = {
    "NSE_EQ":      "NSE",
    "BSE_EQ":      "BSE",
    "NSE_FNO":     "NFO",
    "BSE_FNO":     "BFO",
    "MCX_COMM":    "MCX",
    "NSE_CURRENCY":"CDS",
    "BSE_CURRENCY":"BCD",
    "IDX_I":       "NSE",   # Index instruments — treat as NSE for lookup
}

_dhan_instruments_lock = threading.Lock()
_DHAN_INSTRUMENTS_DATE: str = ""            # IST date string when cache was built
_DHAN_BY_EXCHANGE: dict[str, list[dict]] = {}   # kite_exchange → [instrument rows]
_DHAN_BY_SYMBOL: dict[tuple, str] = {}          # (kite_exchange, tradingsymbol) → security_id


def _ist_today() -> str:
    """Return today's IST date as 'YYYY-MM-DD' (used as cache buster)."""
    from datetime import datetime, timezone, timedelta
    ist = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(ist).strftime("%Y-%m-%d")


import re as _re

# Dhan F&O tradingsymbol format:
#   Options:  ROOT-DDmonYYYY-STRIKE-CE|PE   e.g. "CRUDEOIL-16JUL2026-8500-CE"
#   Futures:  ROOT-DDmonYYYY-FUT            e.g. "CRUDEOIL-19JUN2026-FUT"
#
# Kite F&O tradingsymbol format (what every downstream parser expects):
#   Options:  ROOTYYmmmSTRIKECE|PE          e.g. "CRUDEOIL26JUL8500CE"
#   Futures:  ROOTYYmmmFUT                  e.g. "CRUDEOIL26JULFUT"
#
# Without translation, decomposeSymbol / parse_tradingsymbol / the
# instruments-cache lookup all reject Dhan-format symbols and the
# /admin/derivatives page shows "isn't a recognised option or
# futures contract" above the Legs grid, killing the payoff chart.
_DHAN_OPT_RE = _re.compile(r"^([A-Z]+)-(\d{1,2})([A-Z]{3})(\d{4})-(\d+(?:\.\d+)?)-(CE|PE)$")
_DHAN_FUT_RE = _re.compile(r"^([A-Z]+)-(\d{1,2})([A-Z]{3})(\d{4})-FUT$")


def _dhan_to_kite_symbol(raw: str) -> str:
    """Convert a Dhan F&O tradingsymbol to the Kite-style canonical form.
    Equity / index / unknown shapes fall through unchanged with dashes
    + spaces stripped — same conservative fallback the rest of the
    codebase already uses for non-derivative symbols.
    """
    s = (raw or "").upper().strip()
    if not s:
        return ""
    m = _DHAN_OPT_RE.match(s)
    if m:
        root, dd, mon, yyyy, strike, opt_type = m.groups()
        # Drop trailing .0 on whole-number strikes; preserve halves.
        try:
            strike_f = float(strike)
            strike_disp = (str(int(strike_f)) if strike_f.is_integer()
                           else str(strike_f))
        except ValueError:
            strike_disp = strike
        return f"{root}{yyyy[2:]}{mon}{strike_disp}{opt_type}"
    m = _DHAN_FUT_RE.match(s)
    if m:
        root, _dd, mon, yyyy = m.groups()
        return f"{root}{yyyy[2:]}{mon}FUT"
    # Fallback: just strip dashes + spaces. Equity / index symbols
    # ("RELIANCE", "NIFTY 50") and any Dhan format the regex doesn't
    # cover yet pass through cleanly.
    return s.replace("-", "").replace(" ", "").strip()


def _load_dhan_instruments() -> None:
    """Fetch Dhan's master CSV and populate the module-level caches.
    Called under _dhan_instruments_lock. Silently no-ops on any failure
    so a network blip doesn't crash the broker registry."""
    global _DHAN_INSTRUMENTS_DATE, _DHAN_BY_EXCHANGE, _DHAN_BY_SYMBOL
    by_exchange: dict[str, list[dict]] = {}
    by_symbol: dict[tuple, str] = {}
    try:
        with urlopen(_DHAN_INSTRUMENTS_URL, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        lines = raw.splitlines()
        if not lines:
            logger.warning("DhanBroker: instruments CSV empty")
            return
        # Parse header from first line
        header = [h.strip() for h in lines[0].split(",")]
        # Build column-index lookup for robustness against column reorder
        col = {name: idx for idx, name in enumerate(header)}
        required = {"SEM_SMST_SECURITY_ID", "SEM_TRADING_SYMBOL", "SEM_EXM_EXCH_ID",
                    "SM_SYMBOL_NAME"}
        if not required.issubset(col):
            logger.warning(f"DhanBroker: instruments CSV missing columns "
                           f"{required - set(col)}; cache aborted")
            return
        for line in lines[1:]:
            parts = line.split(",")
            if len(parts) <= max(col.get("SEM_SMST_SECURITY_ID", 0),
                                 col.get("SEM_TRADING_SYMBOL", 0),
                                 col.get("SEM_EXM_EXCH_ID", 0)):
                continue
            seg_raw = parts[col["SEM_EXM_EXCH_ID"]].strip()
            kite_exch = _DHAN_SEGMENT_TO_EXCHANGE.get(seg_raw)
            if not kite_exch:
                continue
            # Translate Dhan's F&O tradingsymbol to the Kite-style canonical
            # form. Dhan ships symbols as "CRUDEOIL-16JUL2026-8500-CE"
            # (ROOT-DDmonYYYY-STRIKE-CE|PE); the Kite parser expects
            # "CRUDEOIL26JUL8500CE" (ROOTYYmmmSTRIKECE). Without this
            # the security_id lookup misses, and the strategy-analytics
            # endpoint rejects the leg with "isn't a recognised option
            # or futures contract" — payoff chart never renders. Equity
            # / index symbols pass through (just strip dashes + spaces).
            ts_raw = parts[col["SEM_TRADING_SYMBOL"]].strip()
            ts = _dhan_to_kite_symbol(ts_raw)
            sid = parts[col["SEM_SMST_SECURITY_ID"]].strip()
            if not ts or not sid:
                continue
            lot_size = 0
            tick_size = 0.0
            if "SM_LOT_SIZE" in col and len(parts) > col["SM_LOT_SIZE"]:
                try:
                    lot_size = int(float(parts[col["SM_LOT_SIZE"]].strip() or 0))
                except (ValueError, TypeError):
                    pass
            if "SEM_TICK_SIZE" in col and len(parts) > col["SEM_TICK_SIZE"]:
                try:
                    tick_size = float(parts[col["SEM_TICK_SIZE"]].strip() or 0)
                except (ValueError, TypeError):
                    pass
            row = {
                "tradingsymbol":    ts,
                "security_id":      sid,
                "exchange":         kite_exch,
                "exchange_segment": seg_raw,
                "lot_size":         lot_size,
                "tick_size":        tick_size,
            }
            by_exchange.setdefault(kite_exch, []).append(row)
            by_symbol[(kite_exch, ts)] = sid
        _DHAN_BY_EXCHANGE = by_exchange
        _DHAN_BY_SYMBOL = by_symbol
        _DHAN_INSTRUMENTS_DATE = _ist_today()
        total = sum(len(v) for v in by_exchange.values())
        logger.info(f"DhanBroker: instruments cache loaded — {total} rows "
                    f"across {len(by_exchange)} exchanges")
    except Exception as e:
        logger.warning(f"DhanBroker: instruments cache load failed: {e}")


def _ensure_dhan_instruments() -> None:
    """Ensure the instruments cache is warm for today's IST date."""
    with _dhan_instruments_lock:
        if _DHAN_INSTRUMENTS_DATE != _ist_today():
            _load_dhan_instruments()


def _resolve_security_id(tradingsymbol: str, kite_exchange: str) -> str:
    """Return the Dhan security_id for a tradingsymbol + Kite exchange.

    Loads the instruments cache lazily (once per IST day). Returns an
    empty string when not found — callers should raise a meaningful
    error rather than passing an empty string to Dhan (which would
    return an opaque 'Invalid security_id' rejection)."""
    _ensure_dhan_instruments()
    return _DHAN_BY_SYMBOL.get((kite_exchange, tradingsymbol), "")


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
        """Not wired yet — returns empty dict so PriceBroker walks to
        the next adapter. Earlier this raised NotImplementedError on
        every iteration, generating WARNING-level log spam. Wiring
        needs tradingsymbol → Dhan security_id mapping via the
        instruments cache."""
        return {}

    def quote(self, symbols: list[str]) -> dict:
        """Empty dict for the same reason as ltp() above."""
        return {}

    def instruments(self, exchange: str | None = None) -> list[dict]:
        """Load Dhan instruments from the master CSV (api.dhan.co/v2/instruments-detailed).
        Cached per IST day — first call fetches, subsequent calls read from memory.
        Returns a Kite-shape list (tradingsymbol, security_id, exchange, lot_size,
        tick_size, exchange_segment). Returns [] on network failure so PriceBroker /
        get_historical_brokers fall through to the next adapter cleanly."""
        _ensure_dhan_instruments()
        if exchange:
            return list(_DHAN_BY_EXCHANGE.get(exchange, []))
        # No exchange filter — merge all
        out: list[dict] = []
        for rows in _DHAN_BY_EXCHANGE.values():
            out.extend(rows)
        return out

    def historical_data(
        self,
        instrument_token: int,
        from_date: Any,
        to_date: Any,
        interval: str = "day",
    ) -> list[dict]:
        """Not wired yet — returns empty bars. PriceBroker fallback
        chain moves on to the next adapter (typically Kite). Same
        rationale as instruments() above: silent empty beats noisy
        NotImplementedError when the adapter is intentionally
        partial."""
        return []

    def holidays(self, exchange: str) -> set[str]:
        """Dhan doesn't publish a holidays endpoint. Empty set so
        PriceBroker falls over to Kite without an exception trace."""
        return set()

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
        """Translate Kite kwargs to Dhan and dispatch. Returns Dhan order_id.

        Accepts the same kwargs as KiteBroker.place_order: tradingsymbol,
        exchange, transaction_type, quantity, product, order_type, price,
        trigger_price, validity, tag, variety.

        security_id is resolved from tradingsymbol + exchange via the
        instruments cache (loaded from Dhan's master CSV once per IST
        day). If the symbol is unknown, raises RuntimeError with a clear
        message pointing at the cache — operator should check whether the
        Dhan instruments CSV has loaded successfully."""
        exchange      = kwargs.get("exchange", "")
        tradingsymbol = kwargs.get("tradingsymbol", "")

        # Resolve security_id — prefer explicit kwarg over instruments lookup
        # so callers that already have security_id (e.g. basket_order_margins)
        # don't pay the cache lookup cost unnecessarily.
        security_id = str(kwargs.get("security_id") or "")
        if not security_id:
            security_id = _resolve_security_id(tradingsymbol, exchange)
        if not security_id:
            raise RuntimeError(
                f"Dhan: unknown tradingsymbol {tradingsymbol!r} on {exchange!r} — "
                f"symbol not found in instruments cache. Ensure Dhan instruments "
                f"CSV loaded successfully (check DhanBroker.instruments())."
            )

        ex_seg  = _dhan_exchange(exchange)
        product = _PRODUCT_TO_DHAN.get(kwargs.get("product", "MIS"), "INTRADAY")
        otype   = _ORDER_TYPE_TO_DHAN.get(kwargs.get("order_type", "MARKET"), "MARKET")

        # Truncate correlation_id (tag) to 20 chars — Dhan enforces
        # a similar cap on correlationId as Kite does on tag.
        _DHAN_CORR_MAX = 20
        tag = kwargs.get("tag")
        if tag is not None:
            tag = str(tag)[:_DHAN_CORR_MAX]

        resp = self._safe_call(lambda d: d.place_order(
            security_id=security_id,
            exchange_segment=ex_seg,
            transaction_type=kwargs.get("transaction_type", "BUY"),
            quantity=int(kwargs.get("quantity", 0)),
            order_type=otype,
            product_type=product,
            price=float(kwargs.get("price") or 0),
            trigger_price=float(kwargs.get("trigger_price") or 0),
            validity=kwargs.get("validity", "DAY"),
            **({"tag": tag} if tag else {}),
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

    # ── GTT (Forever Orders) ──────────────────────────────────────────
    #
    # Dhan calls these "Forever Orders". The dhanhq SDK inherits from
    # ForeverOrder which provides: place_forever / modify_forever /
    # cancel_forever / get_forever. The capability matrix declares
    # gtt_single=True and gtt_oco=True.
    #
    # Kite's "single" → Dhan's order_flag="SINGLE"
    # Kite's "two-leg" (OCO) → Dhan's order_flag="OCO"
    #
    # Shape mapping (Kite → Dhan):
    #   trigger_type="single"  → order_flag="SINGLE"
    #                            trigger_Price  = trigger_values[0]
    #                            price          = orders[0]["price"]
    #   trigger_type="two-leg" → order_flag="OCO"
    #                            leg 0: trigger_Price, price, quantity
    #                            leg 1: trigger_Price1, price1, quantity1
    #
    # Dhan docs: https://dhanhq.co/docs/api-reference/v2/forever-orders/

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
        """Place a Dhan Forever Order (GTT). Returns the Dhan order_id."""
        security_id = _resolve_security_id(tradingsymbol, exchange)
        if not security_id:
            raise RuntimeError(
                f"Dhan place_gtt: unknown symbol {tradingsymbol!r} on {exchange!r}"
            )
        ex_seg  = _dhan_exchange(exchange)
        order0  = orders[0] if orders else {}
        product = _PRODUCT_TO_DHAN.get(order0.get("product", "NRML"), "MARGIN")
        otype   = _ORDER_TYPE_TO_DHAN.get(order0.get("order_type", "LIMIT"), "LIMIT")
        qty0    = int(order0.get("quantity", 0))
        price0  = float(order0.get("price") or 0)
        trig0   = float(trigger_values[0]) if trigger_values else 0.0
        txn0    = order0.get("transaction_type", "SELL")

        _DHAN_CORR_MAX = 20
        corr = str(tag)[:_DHAN_CORR_MAX] if tag else None

        if trigger_type == "single":
            resp = self._safe_call(lambda d: d.place_forever(
                security_id=security_id,
                exchange_segment=ex_seg,
                transaction_type=txn0,
                product_type=product,
                order_type=otype,
                quantity=qty0,
                price=price0,
                trigger_Price=trig0,
                order_flag="SINGLE",
                tag=corr,
                symbol=tradingsymbol,
            ))
        else:
            # OCO — two-leg. Leg 0: entry/stop, Leg 1: target.
            order1  = orders[1] if len(orders) > 1 else {}
            otype1  = _ORDER_TYPE_TO_DHAN.get(order1.get("order_type", "LIMIT"), "LIMIT")
            qty1    = int(order1.get("quantity", qty0))
            price1  = float(order1.get("price") or 0)
            trig1   = float(trigger_values[1]) if len(trigger_values) > 1 else 0.0
            resp = self._safe_call(lambda d: d.place_forever(
                security_id=security_id,
                exchange_segment=ex_seg,
                transaction_type=txn0,
                product_type=product,
                order_type=otype,
                quantity=qty0,
                price=price0,
                trigger_Price=trig0,
                order_flag="OCO",
                price1=price1,
                trigger_Price1=trig1,
                quantity1=qty1,
                tag=corr,
                symbol=tradingsymbol,
            ))

        if not isinstance(resp, dict) or resp.get("status") != "success":
            raise RuntimeError(f"Dhan place_gtt rejected: {resp}")
        data = resp.get("data") or {}
        if isinstance(data, dict):
            return str(data.get("orderId") or data.get("order_id") or "")
        return str(data)

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
        """Modify an existing Dhan Forever Order. Returns the (same) order_id."""
        order0  = orders[0] if orders else {}
        otype   = _ORDER_TYPE_TO_DHAN.get(order0.get("order_type", "LIMIT"), "LIMIT")
        qty0    = int(order0.get("quantity", 0))
        price0  = float(order0.get("price") or 0)
        trig0   = float(trigger_values[0]) if trigger_values else 0.0
        order_flag = "SINGLE" if trigger_type == "single" else "OCO"
        # Dhan's modify_forever `leg_name` differentiates which OCO leg
        # to update: "ENTRY_LEG" (leg 0) or "TARGET_LEG" (leg 1).
        # For single GTT, leg_name is also "ENTRY_LEG".
        resp = self._safe_call(lambda d: d.modify_forever(
            order_id=gtt_id,
            order_flag=order_flag,
            order_type=otype,
            leg_name="ENTRY_LEG",
            quantity=qty0,
            price=price0,
            trigger_price=trig0,
            disclosed_quantity=0,
            validity="DAY",
        ))
        if not isinstance(resp, dict) or resp.get("status") != "success":
            raise RuntimeError(f"Dhan modify_gtt rejected: {resp}")
        return gtt_id

    def cancel_gtt(self, gtt_id: str) -> str:
        """Cancel a Dhan Forever Order. Returns the cancelled order_id."""
        resp = self._safe_call(lambda d: d.cancel_forever(order_id=gtt_id))
        if not isinstance(resp, dict) or resp.get("status") != "success":
            raise RuntimeError(f"Dhan cancel_gtt rejected: {resp}")
        return gtt_id

    def get_gtts(self) -> list[dict]:
        """List all active Dhan Forever Orders, normalised to Kite GTT shape."""
        resp = self._safe_call(lambda d: d.get_forever())
        rows = _unwrap(resp)
        if not isinstance(rows, list):
            rows = []
        out: list[dict] = []
        for r in rows:
            flag = (r.get("orderFlag") or "SINGLE").upper()
            ttype = "single" if flag == "SINGLE" else "two-leg"
            seg = r.get("exchangeSegment") or ""
            kite_exch = _DHAN_SEGMENT_TO_EXCHANGE.get(seg, seg)
            # Build trigger_values list from the response fields
            t0 = float(r.get("triggerPrice") or r.get("trigger_Price") or 0)
            t1 = float(r.get("triggerPrice1") or r.get("trigger_Price1") or 0)
            trigger_values = [t0, t1] if ttype == "two-leg" else [t0]
            out.append({
                "gtt_id":       str(r.get("orderId") or r.get("order_id") or ""),
                "status":       (r.get("orderStatus") or r.get("status") or "").lower(),
                "trigger_type": ttype,
                "tradingsymbol": r.get("tradingSymbol") or r.get("symbol") or "",
                "exchange":     kite_exch,
                "trigger_values": trigger_values,
                "last_price":   float(r.get("lastTradedPrice") or 0),
                "orders": [{
                    "transaction_type": r.get("transactionType") or "SELL",
                    "quantity":         int(r.get("quantity") or 0),
                    "price":            float(r.get("price") or 0),
                    "order_type":       r.get("orderType") or "LIMIT",
                    "product":          r.get("productType") or "NRML",
                }],
                "created_at":   r.get("createTime") or "",
                "_raw":         r,
            })
        return out

    # ── Qty translation ───────────────────────────────────────────────

    def translate_qty(self, exchange: str, raw_qty: int, lot_size: int) -> int:
        """Convert canonical-contract qty (the unit our routes + position
        normalisers use internally) to Dhan's wire format.

        Dhan's API takes quantity IN LOTS for MCX/NCO and IN CONTRACTS
        for NSE/BSE F&O — same convention Kite uses. The position-data
        normaliser (`_normalise_positions`) multiplies netQty × multiplier
        to convert Dhan's lot-based read response back to contracts so
        every downstream surface (Legs grid, day_change_val formula,
        analytics, paper engine) treats Dhan + Kite rows uniformly. This
        method undoes that for the OUTBOUND order: contract qty → lots
        on MCX/NCO, identity on NSE/BSE F&O.

        operator on /admin/derivatives: Dhan CRUDEOIL position was
        showing qty=1 while Kite showed qty=300 for the same 3 lots —
        because the read path stayed in lots while Kite read in contracts.
        The fix normalises BOTH read + write to contracts internally."""
        if exchange in ("MCX", "NCO") and lot_size > 0 and raw_qty >= lot_size:
            translated = max(1, raw_qty // lot_size)
            if translated != raw_qty:
                logger.info(
                    f"[DHAN-QTY] {exchange}: contracts={raw_qty} → lots={translated} "
                    f"(lot_size={lot_size})"
                )
            return translated
        return raw_qty

    def normalise_qty(self, exchange: str, raw_qty: int, lot_size: int) -> int:
        """Back-compat alias — prefer translate_qty in new code."""
        return self.translate_qty(exchange, raw_qty, lot_size)


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

        # Translate Dhan F&O symbol → Kite-style (see _dhan_to_kite_symbol)
        # so every downstream parser + chart works without per-vendor branches.
        _raw_ts_h = str(h.get("tradingSymbol") or h.get("symbol") or "")
        out.append({
            "tradingsymbol":   _dhan_to_kite_symbol(_raw_ts_h),
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
        # Translate Dhan's F&O tradingsymbol to Kite-style canonical
        # form via `_dhan_to_kite_symbol` (e.g. "CRUDEOIL-16JUL2026-8500-CE"
        # → "CRUDEOIL26JUL8500CE"). Without this every downstream parser
        # (decomposeSymbol on the frontend, parse_tradingsymbol in the
        # strategy endpoint, the instruments-cache lookup, etc.) rejects
        # Dhan-format symbols and the Legs grid shows "isn't a recognised
        # option or futures contract" above the payoff chart.
        raw_ts = str(p.get("tradingSymbol") or "")
        ts = _dhan_to_kite_symbol(raw_ts)
        # Dhan returns netQty / dayBuy/SellQty in LOTS, not contracts.
        # Kite returns positions already in CONTRACTS — and every
        # downstream surface (Legs grid display qty, day_change_val
        # formula in broker_apis.py, options strategy analytics, sim
        # paper-trade engine, agent rules referring to qty) expects
        # the CONTRACTS convention. Multiply by Dhan's `multiplier`
        # (lot size for the contract) to align both adapters before
        # the row hits broker_apis. Order placement re-divides via
        # `DhanBroker.translate_qty` (contracts → lots) so the SDK call
        # still sees Dhan's expected unit. `multiplier=1` in the output
        # dict because the qty is now already in contracts — the
        # broker_apis day-PnL formula doesn't need to re-multiply.
        _mult = int(p.get("multiplier", 1) or 1) or 1
        # Convert Dhan's lot-based qty fields to contracts.
        qty_contracts = int(p.get("netQty",      0) or 0) * _mult
        ovn_contracts = int(p.get("carryFwdQty", 0) or 0) * _mult
        dbq_contracts = int(p.get("dayBuyQty",   0) or 0) * _mult
        dsq_contracts = int(p.get("daySellQty",  0) or 0) * _mult

        # Compute P&L ourselves — don't trust Dhan's pre-computed
        # unrealisedProfit / realisedProfit fields, which have shown a
        # ~100× off-by-lot-size discrepancy on F&O contracts (Dhan
        # appears to compute these in LOTS while we display CONTRACTS,
        # and there's no way to flip the convention from the API).
        # Operator: "the entry price and current price difference is
        # P&L. from yesterday's closing price and today's price is day
        # P&L."
        # Formulas (signed; long qty>0, short qty<0):
        #   pnl            = (LTP - avg_price)   × qty   (lifetime / unrealised)
        #   day_change_val = (LTP - close_price) × qty   (today's change)
        avg = float(p.get("netAvgPrice",     0) or 0)
        ltp = float(p.get("lastTradedPrice", 0) or 0)
        close = float(p.get("previousClose", p.get("closePrice", 0)) or 0)
        pnl_calc = (ltp - avg)   * qty_contracts if (ltp > 0 and avg > 0) else 0.0
        dcv_calc = (ltp - close) * qty_contracts if (ltp > 0 and close > 0) else 0.0
        # Keep Dhan's realisedProfit verbatim — that's a closed-book
        # figure they're authoritative on.
        realised = float(p.get("realisedProfit", 0) or 0)

        net.append({
            "tradingsymbol":   ts,
            "exchange":        p.get("exchange")     or "NFO",
            "instrument_token": inst_tok,
            "product":         {"INTRADAY": "MIS",
                                "MARGIN":   "NRML",
                                "CNC":      "CNC"}.get(p.get("productType", ""),
                                                        "NRML"),
            "quantity":           qty_contracts,
            "overnight_quantity": ovn_contracts,
            "day_buy_quantity":   dbq_contracts,
            "day_sell_quantity":  dsq_contracts,
            # Day-trade cash values — used by broker_apis' split P∆
            # formula. Derive from price × qty (in contracts) when Dhan
            # doesn't carry the value field directly.
            "day_buy_value":      float(p.get("dayBuyValue",  0) or 0)
                                  or (float(p.get("dayBuyAvg", 0) or 0) * dbq_contracts),
            "day_sell_value":     float(p.get("daySellValue", 0) or 0)
                                  or (float(p.get("daySellAvg", 0) or 0) * dsq_contracts),
            # Multiplier=1 on the normalised row — qty is now in contracts
            # so the broker_apis day_change_val formula treats it the same
            # as Kite's contract-qty (no extra multiplication needed).
            "multiplier":      1,
            "close_price":     close,
            "average_price":   avg,
            "last_price":      ltp,
            "buy_price":       float(p.get("buyAvg",       0) or 0),
            "sell_price":      float(p.get("sellAvg",      0) or 0),
            "buy_quantity":    int(p.get("buyQty",         0) or 0) * _mult,
            "sell_quantity":   int(p.get("sellQty",        0) or 0) * _mult,
            # Pre-computed pnl + day_change_val from our own formulas;
            # broker_apis still recomputes day_change_val using the split-
            # formula for accuracy on intraday reversals, but having
            # day_change_val populated here means /api/positions returns
            # a sensible value even on routes that don't run the full
            # split (e.g. raw-broker views, demo serialisation).
            "pnl":               pnl_calc,
            "realised":          realised,
            "unrealised":        pnl_calc,
            "day_change_val":    dcv_calc,
            "_raw":              p,
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
        # Translate Dhan F&O symbol → Kite-style (see _dhan_to_kite_symbol)
        # so orders + positions display under one canonical tradingsymbol.
        _raw_ts_o = str(o.get("tradingSymbol") or "")
        out.append({
            "order_id":         str(o.get("orderId") or ""),
            "tradingsymbol":    _dhan_to_kite_symbol(_raw_ts_o),
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
        _raw_ts_t = str(t.get("tradingSymbol") or "")
        out.append({
            "trade_id":         str(t.get("tradeId")   or ""),
            "order_id":         str(t.get("orderId")   or ""),
            "tradingsymbol":    _dhan_to_kite_symbol(_raw_ts_t),
            "exchange":         t.get("exchange")      or "",
            "transaction_type": t.get("transactionType") or "BUY",
            "quantity":         int(t.get("tradedQuantity", 0) or 0),
            "average_price":    float(t.get("tradedPrice",  0) or 0),
            "exchange_timestamp": t.get("exchangeTime")   or "",
            "_raw":             t,
        })
    return out
