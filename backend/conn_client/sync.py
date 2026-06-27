"""Sync facade over the conn_service `/internal/*` endpoints.

Mirrors `backend.conn_client.api` shape but uses httpx's sync
client. Needed because a handful of callers (sim/driver.seed_live,
expiry.OptionPosition._fetch_* class methods) live deep inside
sync call chains that would be expensive to flip to async.

Both facades hit the same UDS. The sync client has its own
connection pool — that's fine, the conn_service can handle both.
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx
import pandas as pd

from backend.conn_client.transport import CONN_SOCK

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(30.0, connect=5.0)

_client: Optional[httpx.Client] = None


def _get_client() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(
            transport=httpx.HTTPTransport(uds=CONN_SOCK),
            base_url="http://conn",
            timeout=_TIMEOUT,
        )
    return _client


def _fetch_per_account(path: str) -> list[pd.DataFrame]:
    try:
        resp = _get_client().get(path)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as e:
        logger.warning("conn_client.sync: %s failed: %s", path, e)
        sentinel = pd.DataFrame()
        sentinel.attrs["fetch_failed"] = True
        return [sentinel]

    out: list[pd.DataFrame] = []
    for entry in payload.get("accounts", []) or []:
        rows = entry.get("rows") or []
        df = pd.DataFrame(rows)
        if not entry.get("ok", True):
            df.attrs["fetch_failed"] = True
        out.append(df)
    return out


def fetch_holdings() -> list[pd.DataFrame]:
    return _fetch_per_account("/internal/holdings")


def fetch_positions() -> list[pd.DataFrame]:
    return _fetch_per_account("/internal/positions")


def fetch_margins() -> list[pd.DataFrame]:
    return _fetch_per_account("/internal/margins")
