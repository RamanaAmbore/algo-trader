"""RemoteBroker — Broker ABC implementation that proxies every call
to conn_service over UDS.

Lives in `backend.conn_client` so it can be constructed by the main
API's `registry.get_broker(account)` when the cutover flag is on.
Downstream callers see a normal `Broker` instance; they don't know
the broker_id-to-adapter dispatch happens inside `conn_service`.

Design notes:

  • Sync. Broker ABC is sync; we use httpx.Client (not AsyncClient)
    so the existing call sites — many of which sit in
    `loop.run_in_executor(...)` thread pools — keep their model.
  • Capability lookup is local. `BrokerCapabilities` doesn't need
    a round-trip: the broker_id is enough, and `capabilities_for_
    broker_id` is a pure function. Saves a UDS hop on every
    capability-gated decision (lots of those in actions.py).
  • Errors map to RuntimeError. Conn_service returns `{ok:false,
    error}` for caught exceptions inside the broker call; we
    re-raise so existing try/except in callers keeps catching.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from backend.conn_client.transport import CONN_SOCK
from backend.shared.brokers.base import Broker
from backend.shared.brokers.capabilities import (
    BrokerCapabilities,
    capabilities_for_broker_id,
)

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(30.0, connect=5.0)
_client: httpx.Client | None = None


def _get_client() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(
            transport=httpx.HTTPTransport(uds=CONN_SOCK),
            base_url="http://conn",
            timeout=_TIMEOUT,
        )
    return _client


class RemoteBroker(Broker):
    """Proxy every Broker ABC method through conn_service UDS."""

    def __init__(self, account: str, broker_id: str = "zerodha_kite"):
        self._account = account
        self._broker_id = broker_id

    @property
    def account(self) -> str:
        return self._account

    @property
    def broker_id(self) -> str:
        return self._broker_id

    @property
    def capabilities(self) -> BrokerCapabilities:
        return capabilities_for_broker_id(self._broker_id)

    # ── Dispatch primitive ────────────────────────────────────────────

    def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        path = f"/internal/broker/{self._account}/call/{method}"
        try:
            resp = _get_client().post(
                path,
                json={"args": list(args), "kwargs": kwargs},
            )
            resp.raise_for_status()
        except httpx.HTTPError as e:
            # Transport / 5xx — surface as RuntimeError so existing
            # try/except in callers (broker_apis, routes/orders, etc.)
            # catches it the same way a local SDK failure would.
            raise RuntimeError(
                f"conn_service unreachable for {self._account}.{method}: {e}"
            ) from e

        payload = resp.json() or {}
        if not payload.get("ok", False):
            raise RuntimeError(
                f"{self._account}.{method} failed: {payload.get('error', '?')}"
            )
        return payload.get("result")

    # ── Account state ─────────────────────────────────────────────────

    def profile(self) -> dict:
        return self._call("profile")

    def holdings(self) -> list[dict]:
        return self._call("holdings")

    def positions(self) -> dict:
        return self._call("positions")

    def margins(self, segment: str | None = None) -> dict:
        return self._call("margins", segment=segment)

    def orders(self) -> list[dict]:
        return self._call("orders")

    def order_status(self, order_id: str) -> dict:
        return self._call("order_status", order_id)

    def trades(self) -> list[dict]:
        return self._call("trades")

    # ── Market data ───────────────────────────────────────────────────

    def ltp(self, symbols: list[str]) -> dict:
        return self._call("ltp", list(symbols))

    def quote(self, symbols: list[str]) -> dict:
        return self._call("quote", list(symbols))

    def instruments(self, exchange: str | None = None) -> list[dict]:
        return self._call("instruments", exchange)

    def historical_data(
        self,
        instrument_token: int,
        from_date: Any,
        to_date: Any,
        interval: str = "day",
    ) -> list[dict]:
        # Coerce date args to ISO strings — datetime objects survive
        # the JSON hop poorly. Adapters at the other end accept
        # str or datetime so this is safe.
        return self._call(
            "historical_data",
            int(instrument_token),
            from_date.isoformat() if hasattr(from_date, "isoformat") else from_date,
            to_date.isoformat() if hasattr(to_date, "isoformat") else to_date,
            interval=interval,
        )

    def holidays(self, exchange: str) -> set[str]:
        result = self._call("holidays", exchange)
        # conn_service coerces set→list for the wire; restore the
        # set shape callers expect.
        return set(result or [])

    def market_status(self, exchange: str) -> bool | None:
        return self._call("market_status", exchange)

    # ── Order entry ───────────────────────────────────────────────────

    def basket_order_margins(self, orders: list[dict]) -> list[dict]:
        return self._call("basket_order_margins", list(orders))

    def place_order(self, **kwargs: Any) -> str:
        return self._call("place_order", **kwargs)

    def modify_order(self, order_id: str, **kwargs: Any) -> str:
        return self._call("modify_order", order_id, **kwargs)

    def cancel_order(self, order_id: str, **kwargs: Any) -> str:
        return self._call("cancel_order", order_id, **kwargs)

    # ── GTT ───────────────────────────────────────────────────────────

    def place_gtt(self, **kwargs: Any) -> str:
        return self._call("place_gtt", **kwargs)

    def modify_gtt(self, gtt_id: str, **kwargs: Any) -> str:
        return self._call("modify_gtt", gtt_id, **kwargs)

    def cancel_gtt(self, gtt_id: str, *, exchange: str | None = None) -> str:
        return self._call("cancel_gtt", gtt_id, exchange=exchange)

    def get_gtts(self) -> list[dict]:
        return self._call("get_gtts")


# Module-level helpers — used by registry.get_broker shim and by the
# Kite postback handler.

def verify_postback(
    account: str,
    *,
    order_id: str,
    order_timestamp: str,
    checksum: str,
) -> bool:
    """Verify a Kite postback signature without disclosing api_secret
    to the main API process. Returns True/False; falls back to False
    on transport error so a stuck conn_service doesn't accept bogus
    postbacks."""
    try:
        resp = _get_client().post(
            f"/internal/broker/{account}/verify_postback",
            json={
                "order_id": str(order_id),
                "order_timestamp": str(order_timestamp),
                "checksum": str(checksum),
            },
        )
        resp.raise_for_status()
        return bool((resp.json() or {}).get("ok", False))
    except Exception as e:
        logger.warning("verify_postback (%s) failed: %s", account, e)
        return False


def fetch_access_token(account: str) -> tuple[str | None, str | None]:
    """Fetch (api_key, access_token) for a Kite account. Used by the
    main API's KiteTicker initialiser until ticker ownership moves
    into conn_service in slice 4."""
    try:
        resp = _get_client().get(f"/internal/broker/{account}/access_token")
        resp.raise_for_status()
        body = resp.json() or {}
        return body.get("api_key"), body.get("access_token")
    except Exception as e:
        logger.warning("access_token (%s) fetch failed: %s", account, e)
        return None, None


def list_remote_accounts() -> list[dict[str, str]]:
    """Snapshot of currently-loaded accounts (account, conn_cls,
    broker_id) — used by Connections.rebuild_from_db's flag-on path."""
    try:
        resp = _get_client().get("/internal/accounts")
        resp.raise_for_status()
        return (resp.json() or {}).get("accounts", []) or []
    except Exception as e:
        logger.warning("list_remote_accounts failed: %s", e)
        return []


def trigger_rebuild() -> dict[str, Any]:
    """Call POST /internal/rebuild on conn_service. Used by main API
    after /admin/brokers CRUD mutations."""
    try:
        resp = _get_client().post("/internal/rebuild")
        resp.raise_for_status()
        return resp.json() or {}
    except Exception as e:
        logger.warning("trigger_rebuild failed: %s", e)
        return {"ok": False, "error": str(e)[:200]}
