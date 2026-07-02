"""msgspec.Struct schemas for the conn_service /internal/* UDS wire format.

Using Struct instead of plain dicts gives ~3× faster JSON encode on the
service side (Litestar auto-encodes Struct returns) and ~3× faster decode
on the client side (msgspec.json.decode vs json.loads + dict access).

The wire format is JSON (same as before) — only the encode/decode path
changes. Existing callers that use resp.json() dict access keep working
because the data shape on the wire is identical.

For the broker.call dispatch result (heterogeneous Any) we keep dict
decoding — Struct schema isn't worth it for that one endpoint.
"""

from __future__ import annotations

from typing import Any

import msgspec


# ── Health / accounts ─────────────────────────────────────────────────


class InternalHealthResp(msgspec.Struct):
    """Response shape for GET /internal/health.

    `ticker` is populated when the ticker singleton has been touched
    at least once (i.e., always in normal operation). Fields:
      * active_account       — Kite account currently bound to the WS
      * failover_list        — priority-ordered eligible Kite accounts
      * consecutive_unhealthy — bad watchdog cycles on active account
      * swaps_last_hour      — auto-failover swaps within the last 3600s
      * last_swap_at         — unix ts of most recent swap (0 if none)
      * started / connected / subscribed_count — legacy fields
    """

    ok: bool
    service: str
    accounts_loaded: int
    accounts: list[str]
    ticker: dict | None = None


class InternalAccountRow(msgspec.Struct):
    """One entry in the /internal/accounts list."""

    account: str
    conn_cls: str
    broker_id: str


class InternalAccountsResp(msgspec.Struct):
    """Response shape for GET /internal/accounts."""

    accounts: list[InternalAccountRow]


# ── Per-account fetch responses ──────────────────────────────────────


class InternalAccountEnvelope(msgspec.Struct):
    """One per-account block inside holdings/positions/margins responses.

    ``rows`` is a list of plain dicts — the row data is heterogeneous
    (broker-native column names, mixed types) so we keep dict for the
    inner payload and only Struct the outer envelope.
    """

    account: str | None
    ok: bool
    rows: list[dict]


class InternalPerAccountResp(msgspec.Struct):
    """Response shape for GET /internal/holdings|positions|margins."""

    accounts: list[InternalAccountEnvelope]
    errors: list[str]


# ── Broker dispatch ───────────────────────────────────────────────────


class BrokerCallResp(msgspec.Struct):
    """Response shape for POST /internal/broker/{account}/call/{method}."""

    ok: bool
    result: Any | None = None
    error: str | None = None


# ── Postback verification ─────────────────────────────────────────────


class VerifyPostbackReq(msgspec.Struct):
    """Request body for POST /internal/broker/{account}/verify_postback."""

    order_id: str
    order_timestamp: str
    checksum: str


class VerifyPostbackResp(msgspec.Struct):
    """Response shape for POST /internal/broker/{account}/verify_postback."""

    ok: bool
    error: str | None = None


# ── Ticker subscribe ──────────────────────────────────────────────────


class TickerSubscribeResp(msgspec.Struct):
    """Response shape for POST /internal/ticker/subscribe."""

    ok: bool
    subscribed: int = 0
    total: int = 0
    error: str | None = None


# ── Dhan poll reset ───────────────────────────────────────────────────


class PollResetRequest(msgspec.Struct):
    """Request body for POST /internal/dhan/poll_reset.

    ``accounts`` — list of Dhan account codes to reset; None = clear all.
    Mirrors the ``dhan_next_poll_clear(accounts)`` signature in broker_apis.
    """

    accounts: list[str] | None = None


class PollResetResp(msgspec.Struct):
    """Response shape for POST /internal/dhan/poll_reset."""

    ok: bool
    cleared: str  # account code or "all"
    error: str | None = None
