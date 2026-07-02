"""Typed wrappers over the conn_service `/internal/*` endpoints.

Each function here returns the SAME shape as the corresponding
`broker_apis.fetch_*` so caller code (background.py, routes/*.py,
sim/driver.py, expiry.py) can swap

    from backend.brokers import broker_apis
    dfs = broker_apis.fetch_holdings()

for

    from backend import conn_client
    dfs = await conn_client.fetch_holdings()

without rewriting the `pd.concat(dfs, ignore_index=True)` /
`df.attrs.get('fetch_failed')` patterns that surround the call.

Three return-shape contracts to preserve (matching broker_apis):

  • `list[DataFrame]` — one entry per loaded account, in
    Connections.conn.keys() order. Empty list if conn_service
    is unreachable (logged, no exception — callers already
    handle empty).
  • `df.attrs['fetch_failed'] = True` — set on the per-account
    frame when that account's broker call failed inside
    conn_service. Callers use this for the "all accounts failed
    = outage" detector.
  • Column names — unchanged. conn_service ships raw broker-
    native column names (e.g. 'avail cash' with the space); the
    main API still owns the _COL_MAP rename in its routes layer.
"""

from __future__ import annotations

import logging
from typing import Any

import msgspec
import pandas as pd

from backend.brokers.client.transport import get_client
from backend.brokers.service.schemas import InternalPerAccountResp

logger = logging.getLogger(__name__)

# Module-level decoder — msgspec Decoder is reusable and cheaper to call
# than msgspec.json.decode(..., type=...) which constructs a new decoder
# each time. Decoder construction is O(1) amortized but the reuse is
# explicit here to make the intent clear.
_per_account_decoder = msgspec.json.Decoder(InternalPerAccountResp)


async def _fetch_per_account(path: str) -> list[pd.DataFrame]:
    """GET `path` on conn_service, rebuild `list[DataFrame]` from
    the per-account wire envelope. On transport / HTTP error returns
    a single fetch_failed frame so the caller's outage detector fires.

    Decode path: msgspec.json.Decoder(InternalPerAccountResp) on
    resp.content — ~3× faster than resp.json() + dict access for the
    typical 5-account × 50-row holdings/positions/margins payload.
    """
    try:
        client = get_client()
        resp = await client.get(path)
        resp.raise_for_status()
        payload = _per_account_decoder.decode(resp.content)
    except Exception as e:
        # Connection refused / timeout / 5xx — surface as one
        # failed-frame so callers like /api/holdings raise their
        # outage banner instead of returning an empty success.
        logger.warning("conn_client: %s failed: %s", path, e)
        sentinel = pd.DataFrame()
        sentinel.attrs["fetch_failed"] = True
        return [sentinel]

    out: list[pd.DataFrame] = []
    for entry in payload.accounts or []:
        rows = entry.rows or []
        df = pd.DataFrame(rows)
        if not entry.ok:
            df.attrs["fetch_failed"] = True
        out.append(df)
    return out


async def fetch_holdings() -> list[pd.DataFrame]:
    """Drop-in replacement for `broker_apis.fetch_holdings()`."""
    return await _fetch_per_account("/internal/holdings")


async def fetch_positions() -> list[pd.DataFrame]:
    """Drop-in replacement for `broker_apis.fetch_positions()`."""
    return await _fetch_per_account("/internal/positions")


async def fetch_margins() -> list[pd.DataFrame]:
    """Drop-in replacement for `broker_apis.fetch_margins()`."""
    return await _fetch_per_account("/internal/margins")


async def fetch_health_snapshot() -> dict[str, dict]:
    """Drop-in replacement for `broker_apis.fetch_health_snapshot()`."""
    try:
        client = get_client()
        resp = await client.get("/internal/health/brokers")
        resp.raise_for_status()
        return (resp.json() or {}).get("health", {}) or {}
    except Exception as e:
        logger.warning("conn_client: /internal/health/brokers failed: %s", e)
        return {}


async def list_accounts() -> list[dict[str, str]]:
    """Return [{account, broker}, ...] for the currently-loaded
    broker accounts. Used by /admin/brokers status surface."""
    try:
        client = get_client()
        resp = await client.get("/internal/accounts")
        resp.raise_for_status()
        return (resp.json() or {}).get("accounts", []) or []
    except Exception as e:
        logger.warning("conn_client: /internal/accounts failed: %s", e)
        return []


async def dhan_poll_reset_remote(accounts: list[str] | None = None) -> None:
    """Reset the Dhan next-poll interval gate inside conn_service.

    When ``RAMBOQ_USE_CONN_SERVICE=1`` the _dhan_next_poll dict lives in
    conn_service (not the main API process).  Route handlers call this on
    ``?fresh=1`` instead of the local ``dhan_next_poll_clear()``.

    Best-effort: logs a warning on failure but never raises, so the caller
    always proceeds to the fresh broker fetch regardless.

    ``accounts`` — list of Dhan account codes to reset; None = clear all.
    """
    try:
        client = get_client()
        body: dict = {"accounts": accounts} if accounts is not None else {}
        resp = await client.post("/internal/dhan/poll_reset", json=body)
        resp.raise_for_status()
    except Exception as e:
        logger.warning("conn_client: dhan_poll_reset_remote failed: %s", e)
