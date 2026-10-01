"""
Standalone broker GTT (Good-Till-Triggered) listing + cancel endpoints.

GET  /api/orders/gtts/                — list broker-resting GTTs across all
                                         configured accounts (optionally
                                         scoped via ?accounts=).
POST /api/orders/gtts/{gtt_id}/cancel — cancel one GTT at the broker
                                         (?account=... required, ?exchange=
                                         optional — Groww requires it to
                                         resolve its segment).

This is a DIFFERENT concept from the per-filled-order attached-exit-GTT
bookkeeping surfaced via `AlgoOrder.attached_gtts_json` (see OrderCard.svelte)
— that tracks GTTs this app itself placed as take-profit/stop legs after a
fill. This module is a raw, read-mostly mirror of whatever stands resting at
the broker right now, regardless of how it got there (operator-placed via
Kite's own app, a template attach, a manual GTT, etc.).

Broker-layer support: `Broker.get_gtts()` / `Broker.cancel_gtt()` (base.py),
implemented by kite.py, dhan.py, groww.py. Each broker's `get_gtts()` row
shape differs slightly (Kite's raw SDK response nests fields under a
`condition` dict; Dhan/Groww pre-flatten to top-level keys) — `_normalize_gtt_row`
below reconciles both shapes into one `GttRow`. The `g.get("id") or
g.get("gtt_id")` id-resolution idiom mirrors the one already established in
`background.py:_oco_build_gtts_map`.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import msgspec
from litestar import Controller, Request, get, post
from litestar.exceptions import HTTPException
from litestar.params import Parameter

from backend.api.auth_guard import auth_or_demo_guard, admin_guard, is_admin_request
from backend.api.cache import get_or_fetch, invalidate
from backend.api.schemas import GttCancelResponse, GttListResponse, GttRow
from backend.shared.helpers.ramboq_logger import get_logger
from backend.shared.helpers.utils import mask_account

logger = get_logger(__name__)

_GTT_FETCH_TIMEOUT = 8   # seconds, per-broker — mirrors _BROKER_ORDERS_TIMEOUT
_GTT_TTL = 10            # seconds — short-lived cache, same spirit as _ORDERS_TTL


# ── Row normalisation ──────────────────────────────────────────────────────

def _normalize_gtt_row(raw: dict, account: str, broker_id: str) -> GttRow:
    """Reconcile Kite's raw-SDK shape (fields nested under `condition`)
    with Dhan/Groww's pre-flattened "Kite GTT shape" normalisers, and tag
    the row with the account + broker it came from."""
    condition = raw.get("condition") or {}
    gtt_id = str(raw.get("id") if raw.get("id") is not None else raw.get("gtt_id") or "")
    tradingsymbol = raw.get("tradingsymbol") or condition.get("tradingsymbol") or ""
    exchange = raw.get("exchange") or condition.get("exchange") or ""
    trigger_values_raw = raw.get("trigger_values") or condition.get("trigger_values") or []
    try:
        trigger_values = [float(v) for v in trigger_values_raw]
    except (TypeError, ValueError):
        trigger_values = []
    last_price_raw = raw.get("last_price")
    if last_price_raw is None:
        last_price_raw = condition.get("last_price")
    try:
        last_price = float(last_price_raw or 0)
    except (TypeError, ValueError):
        last_price = 0.0
    status = str(raw.get("status") or "").lower()
    trigger_type = str(raw.get("trigger_type") or raw.get("type") or "")
    orders = raw.get("orders")
    if not isinstance(orders, list):
        orders = []
    created_at = str(raw.get("created_at") or "")
    return GttRow(
        gtt_id=gtt_id,
        account=account,
        broker_id=broker_id,
        status=status,
        trigger_type=trigger_type,
        tradingsymbol=tradingsymbol,
        exchange=exchange,
        trigger_values=trigger_values,
        last_price=last_price,
        orders=orders,
        created_at=created_at,
    )


# ── Fetch (all accounts, per-account isolation) ─────────────────────────────

def _fetch_gtts() -> list[GttRow]:
    """Fetch every account's broker GTTs in parallel. One account/broker
    failure (timeout or exception) contributes an empty list for that
    account only — never blanks the combined response (staleness-freeze
    convention: a partial failure degrades gracefully, it doesn't collapse
    everything to empty)."""
    import concurrent.futures as _cf
    from backend.brokers.registry import all_brokers

    brokers = list(all_brokers())
    if not brokers:
        return []

    def _one_account(broker) -> list[GttRow]:  # type: ignore[no-untyped-def]
        account = broker.account
        broker_id = getattr(broker, "broker_id", "")
        try:
            raw_rows = broker.get_gtts() or []
            return [
                _normalize_gtt_row(r, account, broker_id)
                for r in raw_rows
                if isinstance(r, dict)
            ]
        except Exception as e:
            logger.error(f"GTT list failed for {account}: {e}")
            return []

    results: list[list[GttRow]] = []
    pool = ThreadPoolExecutor(max_workers=min(len(brokers), 4))
    futs = [(pool.submit(_one_account, b), b.account) for b in brokers]
    for fut, account in futs:
        try:
            results.append(fut.result(timeout=_GTT_FETCH_TIMEOUT))
        except _cf.TimeoutError:
            logger.warning(
                f"GTT list timed out for {account} after {_GTT_FETCH_TIMEOUT}s"
            )
            results.append([])
        except Exception as exc:
            logger.error(f"GTT list failed for {account}: {exc}")
            results.append([])
    pool.shutdown(wait=False, cancel_futures=True)

    return [row for chunk in results for row in chunk]


def _parse_accounts_filter(s: Optional[str]) -> list[str]:
    """Comma-separated account filter — same parsing convention as
    history.py's `_accounts_filter` / logs.py's `_parse_csv_set`."""
    if not s:
        return []
    return [a.strip() for a in s.split(",") if a.strip()]


def _filter_gtts_by_accounts(rows: list[GttRow], accounts: Optional[str]) -> list[GttRow]:
    allow = _parse_accounts_filter(accounts)
    if not allow:
        return rows
    allow_set = set(allow)
    return [r for r in rows if r.account in allow_set]


# ── Cancel (single account, single gtt) ─────────────────────────────────────

async def _cancel_gtt(account: str, gtt_id: str, exchange: Optional[str]) -> str:
    """Cancel one GTT at the broker for `account`. Raises HTTPException on
    any failure — 404 for an unknown account, 501 when the broker adapter
    hasn't implemented cancel_gtt at all, 400 for anything else (bad
    exchange hint, broker rejection, etc.)."""
    from backend.brokers.registry import get_broker

    try:
        broker = get_broker(account)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Account '{account}' not found")

    broker_id = getattr(broker, "broker_id", "")
    try:
        result = broker.cancel_gtt(gtt_id, exchange=exchange)
    except NotImplementedError as e:
        raise HTTPException(
            status_code=501,
            detail=f"{broker_id or account} does not support GTT cancel: {e}",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"GTT cancel failed [{mask_account(account)}] {gtt_id}: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    return str(result)


# ── Controller ───────────────────────────────────────────────────────────────

class GttController(Controller):
    path = "/api/orders/gtts"
    guards = [auth_or_demo_guard]

    @get("/")
    async def list_gtts(
        self,
        request: Request,
        accounts: Optional[str] = None,
    ) -> GttListResponse:
        try:
            rows: list[GttRow] = await get_or_fetch("gtts", _fetch_gtts, ttl_seconds=_GTT_TTL)
        except Exception as e:
            logger.error(f"GTT list API error: {e}")
            raise HTTPException(status_code=500, detail=str(e))

        rows = _filter_gtts_by_accounts(rows, accounts)

        # Mask account codes for non-admin callers — symmetric with
        # OrdersController.list_orders.
        if not is_admin_request(request):
            rows = [msgspec.structs.replace(r, account=mask_account(r.account)) for r in rows]

        return GttListResponse(gtts=rows, count=len(rows))

    @post("/{gtt_id:str}/cancel", guards=[admin_guard])
    async def cancel_gtt_handler(
        self,
        gtt_id: str,
        account: str = Parameter(query="account"),
        exchange: Optional[str] = Parameter(query="exchange", default=None),
    ) -> GttCancelResponse:
        cancelled_id = await _cancel_gtt(account, gtt_id, exchange)
        invalidate("gtts")
        logger.info(f"GTT cancelled: {cancelled_id} [{mask_account(account)}]")
        return GttCancelResponse(gtt_id=cancelled_id)
