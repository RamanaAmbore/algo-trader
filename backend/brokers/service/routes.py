"""Internal endpoints exposed by conn_service — the broker wrapper.

This file exposes two endpoint families:

  /internal/holdings | /positions | /margins | /health/brokers
      — high-level aggregations across every loaded broker account.

  /internal/broker/{account}/{method}  (POST)
      — generic per-account dispatch to any whitelisted Broker ABC
        method. Lets the main API treat conn_service as a remote
        broker registry: it constructs a RemoteBroker that proxies
        every call through here, and downstream code (registry.get_
        broker(account).quote(...)) works without knowing it's
        remote.

  /internal/broker/{account}/api_secret  (GET, sensitive)
      — single-purpose endpoint for Kite postback HMAC verification.
        The postback signature gate in routes/orders.py needs the
        api_secret for an HMAC-SHA256, but we don't want to expose
        the full credential to the main API process at-rest. This
        endpoint scopes the disclosure to the postback path only.

  /internal/broker/{account}/verify_postback  (POST)
      — preferred alternative: compute the HMAC inside conn_service
        so api_secret never leaves this process. Body: {order_id,
        order_timestamp, checksum}. Returns {ok: bool}.

  /internal/rebuild  (POST)
      — re-run Connections.rebuild_from_db(). Called by main API
        after /admin/brokers CRUD mutations so credential changes
        propagate without restarting either service.


The main API speaks to this service via Unix domain socket. Every
endpoint here delegates to the existing `broker_apis` module, which
in turn reads from the in-process Connections singleton owned by
this process. All three brokers (Kite + Dhan + Groww) flow through
the same path because the @for_all_accounts decorator iterates
Connections.conn.keys() uniformly — no broker-specific branches.

These endpoints are NOT meant to be public — they're consumed by
the main API service over the UDS. No auth required at the HTTP
layer because the socket itself is the auth boundary (file mode
0660 owned by www-data:www-data restricts access to the same OS
user that runs ramboq_api.service / ramboq_dev_api.service).

Wire format for fetch endpoints — `{accounts: [...]}` instead of a
flat row list. Each entry preserves the per-account boundary plus
the `fetch_failed` flag so callers can keep their outage-detection
logic (`if all(df.attrs['fetch_failed'])`) drop-in compatible.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
from typing import Any

from litestar import Controller, get, post
from litestar.exceptions import HTTPException, NotFoundException

from backend.brokers.service.schemas import (
    InternalHealthResp,
    InternalAccountRow,
    InternalAccountsResp,
    InternalAccountEnvelope,
    InternalPerAccountResp,
    VerifyPostbackResp,
    TickerSubscribeResp,
    PollResetRequest,
    PollResetResp,
)

logger = logging.getLogger(__name__)


# Broker ABC methods exposed via the generic dispatch endpoint. Anything
# not in this set returns 403 — keeps a hostile main API (or operator
# mistake) from invoking unintended adapter internals over the UDS.
#
# Categories:
#   read    — quote / position / account state, safe to invoke freely.
#   write   — order placement / modification / cancellation. Sensitive
#             but the UDS file mode (0660 www-data) is the auth
#             boundary; no extra layer needed.
#   meta    — capability lookups, pure-math helpers.
_ALLOWED_BROKER_METHODS = frozenset({
    # read
    "profile", "holdings", "positions", "margins", "orders",
    "order_status", "trades",
    "ltp", "quote",
    "instruments", "historical_data", "holidays", "market_status",
    "basket_order_margins", "get_gtts",
    # write
    "place_order", "modify_order", "cancel_order",
    "place_gtt", "modify_gtt", "cancel_gtt",
    # meta
    "translate_qty", "normalise_qty",
})


def _to_jsonable(value: Any) -> Any:
    """Coerce broker return values into something msgspec/json can
    serialize. The common odd type is `set[str]` from Broker.holidays;
    nested dicts and lists pass through unchanged."""
    if isinstance(value, set):
        return sorted(value)
    if isinstance(value, dict):
        return {k: _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    return value


def _df_list_to_per_account(dfs: list) -> list[InternalAccountEnvelope]:
    """Convert a list of per-account DataFrames into msgspec Struct envelopes.

    Output: one InternalAccountEnvelope per DataFrame returned by
    @for_all_accounts. Account is taken from the first row; falls back
    to None on empty frames. Litestar auto-encodes Struct lists as JSON,
    ~3× faster than dict-to-json for the same payload shape.
    """
    out: list[InternalAccountEnvelope] = []
    for df in dfs or []:
        if df is None:
            continue
        fetch_failed = bool(getattr(df, "attrs", {}).get("fetch_failed", False))
        acct = None
        if not df.empty and "account" in df.columns:
            acct = df["account"].iloc[0]
        rows = [] if df.empty else df.to_dict(orient="records")
        out.append(InternalAccountEnvelope(
            account=str(acct) if acct is not None else None,
            ok=not fetch_failed,
            rows=rows,
        ))
    return out


class HealthController(Controller):
    """Cheap probe — used by systemd ExecStartPost + deploy.sh
    health-check loop to verify the service is up. Does NOT exercise
    broker calls; only confirms the Litestar app is responsive."""

    path = "/"

    @get("/health")
    async def health(self) -> InternalHealthResp:
        from backend.brokers.connections import Connections

        accts = sorted(Connections().conn.keys())

        # Ticker snapshot including auto-failover state. The watchdog is
        # the writer; this is a read-only view. Failure is non-fatal — a
        # newly-booted process before the ticker has ever started should
        # still return ok=True from /health so systemd probes pass.
        ticker_snap: dict | None = None
        try:
            from backend.brokers.kite_ticker import get_ticker
            from backend.brokers.service.app import _kite_failover_list

            ts = get_ticker().status()
            # Recompute the failover-list snapshot here so the health probe
            # reflects the CURRENT priority ordering, not whatever the
            # watchdog last saw (operator may have edited priority via
            # /admin/brokers between watchdog cycles).
            fo_list = _kite_failover_list()
            ticker_snap = {
                "active_account":        ts.get("active_account", ""),
                "failover_list":         fo_list,
                "consecutive_unhealthy": int(ts.get("consecutive_unhealthy", 0)),
                "swaps_last_hour":       int(ts.get("swaps_last_hour", 0)),
                "last_swap_at":          float(ts.get("last_swap_at", 0.0)),
                "started":               bool(ts.get("started", False)),
                "connected":             bool(ts.get("connected", False)),
                "subscribed_count":      int(ts.get("subscribed_count", 0)),
                "stale_count":           int(ts.get("stale_count", 0)),
                "max_age_seconds":       float(ts.get("max_age_seconds", 0.0)),
            }
        except Exception:
            logger.exception("conn_service: /health ticker snapshot failed")

        return InternalHealthResp(
            ok=True,
            service="ramboq_conn",
            accounts_loaded=len(accts),
            accounts=accts,
            ticker=ticker_snap,
        )


class InternalBrokerController(Controller):
    """Broker-data endpoints — consumed by the main API service over
    UDS. Each endpoint wraps the existing broker_apis.fetch_* helpers
    so the same path that already runs in production today is now
    just hosted in a different process."""

    path = "/internal"

    @get("/accounts")
    async def accounts(self) -> InternalAccountsResp:
        """Return the currently-loaded broker accounts, including
        which broker each one is bound to AND the canonical broker_id
        (zerodha_kite / dhan / groww) so the main API can populate
        its registry without holding a real Connections singleton.

        Shape:
          {accounts: [
              {account, conn_cls, broker_id},
              ...
          ]}
        """
        from backend.brokers.connections import Connections

        c = Connections()
        id_map = getattr(c, "_broker_id_map", {}) or {}
        rows: list[InternalAccountRow] = []
        for acct, conn in c.conn.items():
            conn_cls = type(conn).__name__
            broker_id = id_map.get(acct, "zerodha_kite")
            rows.append(InternalAccountRow(
                account=acct,
                conn_cls=conn_cls,
                broker_id=broker_id,
            ))
        return InternalAccountsResp(accounts=rows)

    @post("/rebuild")
    async def rebuild(self) -> dict[str, Any]:
        """Re-run Connections.rebuild_from_db(). Called by main API
        after /admin/brokers CRUD mutations so credential changes
        land in conn_service without restarting either service.

        Idempotent (every call re-reads the broker_accounts table)
        but expensive (touches every account's auth flow). Don't
        call on a hot path."""
        from backend.brokers.connections import Connections

        try:
            await Connections().rebuild_from_db()
            accts = sorted(Connections().conn.keys())
            return {"ok": True, "accounts": accts}
        except Exception as e:
            logger.exception("conn_service: rebuild failed")
            return {"ok": False, "error": str(e)[:300]}

    @get("/holdings")
    async def holdings(self) -> InternalPerAccountResp:
        """Multi-broker holdings fetch (Kite + Dhan + Groww). Wraps
        broker_apis.fetch_holdings() which is decorated with
        @for_all_accounts — iterates EVERY account in the singleton
        and returns the union of rows.

        Wire shape: { accounts: [...envelopes], errors: [] }
        """
        from backend.brokers.broker_apis import fetch_holdings

        try:
            dfs = await asyncio.to_thread(fetch_holdings)
            return InternalPerAccountResp(
                accounts=_df_list_to_per_account(dfs), errors=[]
            )
        except Exception as e:
            logger.exception("conn_service: fetch_holdings failed")
            return InternalPerAccountResp(accounts=[], errors=[str(e)[:300]])

    @get("/positions")
    async def positions(self) -> InternalPerAccountResp:
        """Multi-broker positions fetch (Kite + Dhan + Groww)."""
        from backend.brokers.broker_apis import fetch_positions

        try:
            dfs = await asyncio.to_thread(fetch_positions)
            return InternalPerAccountResp(
                accounts=_df_list_to_per_account(dfs), errors=[]
            )
        except Exception as e:
            logger.exception("conn_service: fetch_positions failed")
            return InternalPerAccountResp(accounts=[], errors=[str(e)[:300]])

    @get("/margins")
    async def margins(self) -> InternalPerAccountResp:
        """Multi-broker margins / funds fetch (Kite + Dhan + Groww).
        Returns the flattened payload with broker-native column names
        (e.g. 'avail cash', 'util debits' with spaces) — the main API
        applies its _COL_MAP rename downstream."""
        from backend.brokers.broker_apis import fetch_margins

        try:
            dfs = await asyncio.to_thread(fetch_margins)
            return InternalPerAccountResp(
                accounts=_df_list_to_per_account(dfs), errors=[]
            )
        except Exception as e:
            logger.exception("conn_service: fetch_margins failed")
            return InternalPerAccountResp(accounts=[], errors=[str(e)[:300]])

    @get("/health/brokers")
    async def broker_health(self) -> dict[str, Any]:
        """Per-account fetch-health snapshot. Used by the navbar
        'N of M brokers connected' badge in main API."""
        from backend.brokers.broker_apis import fetch_health_snapshot

        return {"health": fetch_health_snapshot()}

    @post("/dhan/poll_reset")
    async def dhan_poll_reset(
        self, data: PollResetRequest | None = None,
    ) -> PollResetResp:
        """Clear the Dhan next-poll interval gate for one or all accounts.

        Called by the main API when ``?fresh=1`` is requested under
        ``RAMBOQ_USE_CONN_SERVICE=1``.  The _dhan_next_poll dict lives in
        this process (conn_service owns all broker calls), so the main API
        cannot clear it locally — it proxies the reset through this endpoint.

        Auth boundary: UDS file mode 0660 www-data (same as every other
        /internal/* endpoint — no extra HMAC needed).

        Body: {"accounts": ["DH6847"]} or {} / omitted (clear all).
        """
        from backend.brokers.broker_apis import dhan_next_poll_clear

        try:
            accounts = (data.accounts if data is not None else None)
            dhan_next_poll_clear(accounts)
            label = ",".join(accounts) if accounts else "all"
            logger.debug("conn_service: dhan_poll_reset cleared=%s", label)
            return PollResetResp(ok=True, cleared=label)
        except Exception as e:
            logger.warning("conn_service: dhan_poll_reset failed: %s", e)
            return PollResetResp(ok=False, cleared="", error=str(e)[:300])

    @post("/ticker/subscribe")
    async def ticker_subscribe(self, data: dict[str, Any]) -> TickerSubscribeResp:
        """Push a batch of token-symbol pairs onto the KiteTicker
        subscription set.

        Body shape: {"pairs": [[token, "SYM1"], [token2, "SYM2"], ...]}
        Returns: {"ok": true, "subscribed": N, "total": M}

        Idempotent — subscribe_with_sym on the underlying TickerManager
        already dedupes against the existing token set. Called by main
        API on sparkline-warm / watchlist add / quote routes."""
        from backend.brokers.kite_ticker import get_ticker

        ticker = get_ticker()
        pairs_raw = data.get("pairs") or []
        # Accept either [[tok, sym], ...] or [{"token": ..., "sym": ...}].
        pairs: list[tuple[int, str]] = []
        for entry in pairs_raw:
            if isinstance(entry, dict):
                tok = entry.get("token") or entry.get("tok")
                sym = entry.get("sym") or entry.get("symbol")
            else:
                tok = entry[0] if len(entry) > 0 else None
                sym = entry[1] if len(entry) > 1 else ""
            if tok is None:
                continue
            try:
                pairs.append((int(tok), str(sym or "")))
            except (TypeError, ValueError):
                continue
        if not pairs:
            return TickerSubscribeResp(ok=True, subscribed=0, total=0)
        try:
            ticker.subscribe_with_sym(pairs)
            status = ticker.status()
            return TickerSubscribeResp(
                ok=True,
                subscribed=len(pairs),
                total=status.get("subscribed_count", 0),
            )
        except Exception as e:
            logger.exception("conn_service: ticker subscribe failed")
            return TickerSubscribeResp(ok=False, error=str(e)[:300])

    @post("/ticker/force-unhealthy")
    async def ticker_force_unhealthy(
        self, data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Operator escape hatch — flag the ticker unhealthy for a
        bounded window so the watchdog fires its failover path within
        one cycle.

        Used for prod verification of the auto-failover state machine.
        Does NOT actually kill the WebSocket (that would take a Kite-
        side action or a network drop). Instead it sets a deadline
        during which `is_active_ticker_healthy()` returns False; the
        watchdog then bumps the unhealthy counter across the threshold
        and calls `restart_with_account()` on the next cycle.

        The deadline auto-clears past `duration_s` seconds (default
        120 s — long enough for one 30 s watchdog cycle + swap),
        so a forgotten force-unhealthy never leaves the ticker broken.

        Body: {"duration_s": 120} (optional). Returns
        {ok, deadline_unix, forced_from}.
        """
        from backend.brokers.kite_ticker import get_ticker

        try:
            ticker = get_ticker()
            duration_s = 120.0
            if data and "duration_s" in data:
                try:
                    duration_s = float(data["duration_s"])
                except (TypeError, ValueError):
                    duration_s = 120.0
            deadline = ticker.force_unhealthy(duration_s)
            return {
                "ok": True,
                "deadline_unix": deadline,
                "forced_from": ticker.current_account() or "",
            }
        except Exception as e:
            logger.exception("conn_service: force-unhealthy failed")
            return {"ok": False, "error": str(e)[:300]}

    @get("/ticker/status")
    async def ticker_status(self) -> dict[str, Any]:
        """KiteTicker health snapshot. Used by /admin/health badge and
        the watchdog in main API.

        Includes the auto-failover state machine fields
        (`active_account`, `consecutive_unhealthy`, `swaps_last_hour`,
        `last_swap_at`, `failover_list`) alongside the legacy `started`
        / `connected` / `subscribed_count` fields.
        """
        from backend.brokers.kite_ticker import get_ticker
        try:
            snap = get_ticker().status()
            # Enrich with the priority-ordered failover list — computed
            # here (not stored on the ticker) so an operator's live
            # priority edit surfaces without waiting for a swap.
            try:
                from backend.brokers.service.app import _kite_failover_list
                snap["failover_list"] = _kite_failover_list()
            except Exception:
                snap["failover_list"] = []
            return {"ok": True, "status": snap}
        except Exception as e:
            return {"ok": False, "error": str(e)[:300]}


class BrokerDispatchController(Controller):
    """Per-account broker dispatch — RemoteBroker in main API speaks
    to this controller to invoke any whitelisted Broker ABC method
    over UDS. Keeps the per-call surface generic so adding a new
    Broker method doesn't require a new endpoint here.

    Path layout:
      POST  /internal/broker/{account}/call/{method}
            — invoke broker.method(*args, **kwargs)
      POST  /internal/broker/{account}/verify_postback
            — compute HMAC(order_id + order_timestamp + api_secret)
              inside this process and compare to checksum, so the
              api_secret never crosses the UDS.
      GET   /internal/broker/{account}/access_token
            — fetch the live Kite access_token. Needed by the main
              API's KiteTicker until ticker ownership moves into
              conn_service (slice 4).
    """

    path = "/internal/broker"

    @post("/{account:str}/call/{method:str}")
    async def call_broker(
        self,
        account: str,
        method: str,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Generic dispatch to a Broker ABC method.

        Body: {"args": [...], "kwargs": {...}}
        Returns: {"ok": true, "result": <json-coerced return value>}
        Errors: {"ok": false, "error": "<msg>"}
        """
        if method not in _ALLOWED_BROKER_METHODS:
            raise HTTPException(
                status_code=403,
                detail=f"method '{method}' not in dispatch whitelist",
            )

        from backend.brokers.registry import get_broker

        try:
            broker = get_broker(account)
        except (KeyError, ValueError) as e:
            raise NotFoundException(detail=str(e))

        fn = getattr(broker, method, None)
        if not callable(fn):
            raise NotFoundException(
                detail=f"broker for {account!r} has no method '{method}'",
            )

        payload = data or {}
        args = payload.get("args", []) or []
        kwargs = payload.get("kwargs", {}) or {}

        try:
            result = await asyncio.to_thread(fn, *args, **kwargs)
            return {"ok": True, "result": _to_jsonable(result)}
        except Exception as e:
            logger.exception(
                "conn_service: broker dispatch failed: %s.%s",
                account, method,
            )
            return {"ok": False, "error": str(e)[:500]}

    @post("/{account:str}/verify_postback")
    async def verify_postback(
        self,
        account: str,
        data: dict[str, Any],
    ) -> VerifyPostbackResp:
        """Verify a Kite postback signature without exposing the
        api_secret to the main API process.

        Body: {order_id, order_timestamp, checksum}
        Returns: VerifyPostbackResp(ok: bool)

        Kite spec: HMAC-SHA256-ish (actually a plain SHA-256 of the
        concatenated string) = sha256(order_id + order_timestamp +
        api_secret). We mirror the existing main-API code path so
        the signature semantics are unchanged.
        """
        from backend.brokers.connections import Connections

        conn = Connections().conn.get(account)
        if conn is None:
            raise NotFoundException(detail=f"no account {account!r}")
        api_secret = getattr(conn, "api_secret", None)
        if not api_secret:
            # Dhan / Groww / non-Kite account — no postback signature
            # to verify. Treat as a programming error from the caller
            # since the route should only post here for Kite accounts.
            return VerifyPostbackResp(ok=False, error="no api_secret on this account")

        order_id = str(data.get("order_id", ""))
        ts = str(data.get("order_timestamp", ""))
        checksum = str(data.get("checksum", ""))
        msg = (order_id + ts + api_secret).encode()
        expected = hashlib.sha256(msg).hexdigest()
        return VerifyPostbackResp(ok=hmac.compare_digest(expected, checksum))

    @get("/{account:str}/access_token")
    async def access_token(self, account: str) -> dict[str, Any]:
        """Return the live Kite access_token for `account`.

        Used by the main API's KiteTicker initialiser — the WebSocket
        construction needs the token, but main API doesn't load
        Connections when the flag is on. Once slice 4 moves ticker
        ownership into conn_service, this endpoint goes away.

        Returns {api_key, access_token} for Kite accounts; 404 for
        non-Kite. Treat the token as sensitive: it grants full broker
        access. UDS file-mode is the auth boundary.
        """
        from backend.brokers.connections import Connections

        conn = Connections().conn.get(account)
        if conn is None:
            raise NotFoundException(detail=f"no account {account!r}")
        # Kite-only — Dhan + Groww have different token shapes and don't
        # feed KiteTicker.
        api_key = getattr(conn, "api_key", None)
        token_getter = getattr(conn, "get_access_token", None)
        if api_key is None or token_getter is None:
            raise NotFoundException(
                detail=f"account {account!r} is not Kite-shaped",
            )
        try:
            token = await asyncio.to_thread(token_getter)
        except Exception as e:
            logger.warning("conn_service: access_token fetch failed: %s", e)
            return {"api_key": api_key, "access_token": None}
        return {"api_key": api_key, "access_token": token}
