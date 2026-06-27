"""Internal endpoints exposed by conn_service.

The main API speaks to this service via Unix domain socket. Every
endpoint here delegates to the existing `broker_apis` module, which
in turn reads from the in-process Connections singleton owned by
this process. All three brokers (Kite + Dhan + Groww) flow through
the same path because broker_apis.fetch_holdings is decorated with
@for_all_accounts which iterates Connections.conn.keys() — operator
explicitly asked for Groww to be in scope here.

These endpoints are NOT meant to be public — they're consumed by
the main API service over the UDS. No auth required at the HTTP
layer because the socket itself is the auth boundary (file mode
0660 owned by www-data:www-data restricts access to the same OS
user that runs ramboq_api.service).

Endpoint shape:
  GET  /health              — readiness probe + broker count
  GET  /internal/holdings   — every account's holdings (one DataFrame
                              per account, returned as JSON)
  GET  /internal/positions  — same shape for positions
  GET  /internal/margins    — same shape for margins (funds)
  GET  /internal/health/brokers — per-account fetch-health snapshot
  GET  /internal/accounts   — currently-loaded broker accounts
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from litestar import Controller, get

logger = logging.getLogger(__name__)


def _df_list_to_records(dfs: list) -> list[dict[str, Any]]:
    """Convert a list of pandas DataFrames to a single JSON-able
    list of records, preserving each row's `account` column.

    Used by every internal endpoint so the wire format is uniform:
    a JSON array of row dicts, regardless of how many accounts
    contributed. Returns [] when there's no data.
    """
    out: list[dict[str, Any]] = []
    for df in dfs or []:
        if df is None or getattr(df, "empty", True):
            continue
        # to_dict('records') is the cheapest path that preserves
        # column dtypes-as-Python via msgspec/json-friendly types.
        out.extend(df.to_dict(orient="records"))
    return out


class HealthController(Controller):
    """Cheap probe — used by systemd ExecStartPost + deploy.sh
    health-check loop to verify the service is up. Does NOT exercise
    broker calls; only confirms the Litestar app is responsive."""

    path = "/"

    @get("/health")
    async def health(self) -> dict[str, Any]:
        from backend.shared.helpers.connections import Connections

        accts = sorted(Connections().conn.keys())
        return {
            "ok": True,
            "service": "ramboq_conn",
            "accounts_loaded": len(accts),
            "accounts": accts,
        }


class InternalBrokerController(Controller):
    """Broker-data endpoints — consumed by the main API service over
    UDS. Each endpoint wraps the existing broker_apis.fetch_* helpers
    so the same path that already runs in production today is now
    just hosted in a different process."""

    path = "/internal"

    @get("/accounts")
    async def accounts(self) -> dict[str, Any]:
        """Return the currently-loaded broker accounts, including
        which broker each one is bound to. Useful for the main API
        to surface 'who's connected' without needing to query each
        endpoint separately."""
        from backend.shared.helpers.connections import Connections

        c = Connections()
        out: list[dict[str, str]] = []
        for acct, conn in c.conn.items():
            broker_cls = type(conn).__name__  # KiteConnection / DhanConnection / GrowwConnection
            out.append({"account": acct, "broker": broker_cls})
        return {"accounts": out}

    @get("/holdings")
    async def holdings(self) -> dict[str, Any]:
        """Multi-broker holdings fetch (Kite + Dhan + Groww). Wraps
        broker_apis.fetch_holdings() which is decorated with
        @for_all_accounts — iterates EVERY account in the singleton
        and returns the union of rows.

        Wire shape: { rows: [...], errors: [...] }
        """
        from backend.shared.helpers.broker_apis import fetch_holdings

        try:
            dfs = await asyncio.to_thread(fetch_holdings)
            return {"rows": _df_list_to_records(dfs), "errors": []}
        except Exception as e:
            logger.exception("conn_service: fetch_holdings failed")
            return {"rows": [], "errors": [str(e)[:300]]}

    @get("/positions")
    async def positions(self) -> dict[str, Any]:
        """Multi-broker positions fetch (Kite + Dhan + Groww)."""
        from backend.shared.helpers.broker_apis import fetch_positions

        try:
            dfs = await asyncio.to_thread(fetch_positions)
            return {"rows": _df_list_to_records(dfs), "errors": []}
        except Exception as e:
            logger.exception("conn_service: fetch_positions failed")
            return {"rows": [], "errors": [str(e)[:300]]}

    @get("/margins")
    async def margins(self) -> dict[str, Any]:
        """Multi-broker margins / funds fetch (Kite + Dhan + Groww).
        Returns the flattened payload with broker-native column names
        (e.g. 'avail cash', 'util debits' with spaces) — the main API
        applies its _COL_MAP rename downstream."""
        from backend.shared.helpers.broker_apis import fetch_margins

        try:
            dfs = await asyncio.to_thread(fetch_margins)
            return {"rows": _df_list_to_records(dfs), "errors": []}
        except Exception as e:
            logger.exception("conn_service: fetch_margins failed")
            return {"rows": [], "errors": [str(e)[:300]]}

    @get("/health/brokers")
    async def broker_health(self) -> dict[str, Any]:
        """Per-account fetch-health snapshot. Used by the navbar
        'N of M brokers connected' badge in main API."""
        from backend.shared.helpers.broker_apis import fetch_health_snapshot

        return {"health": fetch_health_snapshot()}
