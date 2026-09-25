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

# Module-level decoder — reuse across calls for maximum efficiency.
_per_account_decoder = msgspec.json.Decoder(InternalPerAccountResp)

_client = httpx.Client(
    base_url="http://conn",
    transport=httpx.HTTPTransport(uds=CONN_SOCK),
    timeout=_TIMEOUT,
)


def _get_client() -> httpx.Client:
    return _client


def _failed_sentinel() -> list[pd.DataFrame]:
    """Single-frame `fetch_failed` sentinel — the same shape used on
    transport/HTTP failure and on a conn_service-side degraded response.
    Kept as one helper so the two failure paths below can't drift apart."""
    sentinel = pd.DataFrame()
    sentinel.attrs["fetch_failed"] = True
    return [sentinel]


def _fetch_per_account(path: str) -> list[pd.DataFrame]:
    """Sync version of the per-account fetch. Uses msgspec decoder on
    resp.content for ~3× faster decode vs resp.json() + dict access.

    conn_service's /internal/holdings|positions|margins handlers catch
    every exception raised during the per-account fetch loop (e.g. one
    account's post-processing step raising, which aborts the WHOLE
    @for_all_accounts batch — not just that account) and return HTTP 200
    with `accounts: []` + `errors: [...]`. Without the check below that
    200 looks identical to a genuine "zero rows" result, so callers'
    outage/stale-substitute detectors never fire and a real fetch failure
    silently renders as an empty-but-fresh book (NavStrip / Payoff chart
    showing 0 instead of the last-known-good value).

    A conn_service process with zero loaded broker accounts also returns
    `accounts: []` with `errors: []` — that combination is NOT treated as
    a failure (genuinely nothing to report), so a fresh box with no
    broker accounts configured doesn't get stuck permanently "failed"."""
    try:
        resp = _get_client().get(path)
        resp.raise_for_status()
        payload = _per_account_decoder.decode(resp.content)
    except Exception as e:
        logger.warning("conn_client.sync: %s failed: %s", path, e)
        return _failed_sentinel()

    if payload.errors:
        logger.warning(
            "conn_client.sync: %s returned degraded response: %s",
            path, "; ".join(payload.errors)[:300],
        )
        return _failed_sentinel()

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
