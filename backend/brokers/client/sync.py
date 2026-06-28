"""Sync facade over the conn_service `/internal/*` endpoints.

Mirrors `backend.brokers.client.api` shape but uses httpx's sync
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
import msgspec
import pandas as pd

from backend.brokers.client.transport import CONN_SOCK
from backend.brokers.service.schemas import InternalPerAccountResp

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(30.0, connect=5.0)

_client: Optional[httpx.Client] = None

# Module-level decoder — reuse across calls for maximum efficiency.
_per_account_decoder = msgspec.json.Decoder(InternalPerAccountResp)


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
    """Sync version of the per-account fetch. Uses msgspec decoder on
    resp.content for ~3× faster decode vs resp.json() + dict access."""
    try:
        resp = _get_client().get(path)
        resp.raise_for_status()
        payload = _per_account_decoder.decode(resp.content)
    except Exception as e:
        logger.warning("conn_client.sync: %s failed: %s", path, e)
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


def fetch_holdings() -> list[pd.DataFrame]:
    return _fetch_per_account("/internal/holdings")


def fetch_positions() -> list[pd.DataFrame]:
    return _fetch_per_account("/internal/positions")


def fetch_margins() -> list[pd.DataFrame]:
    return _fetch_per_account("/internal/margins")
